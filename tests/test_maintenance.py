from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from maintenance_planning.clock import FrozenClock
from maintenance_planning.contracts import HealthEvidence, RiskRulebook
from maintenance_planning.errors import Conflict, Forbidden, InvalidState
from maintenance_planning.risk import rank, score_evidence
from maintenance_planning.scheduler import build_schedule
from maintenance_planning.service import MaintenanceService


def evidence_dict(battery="bat-1", facility="f1", revision="ev-1", usable="800",
                  rated="1000", alerts=(), recall="none", blocked=None, cycles=1000):
    data = {
        "battery_id": battery, "facility_id": facility, "evidence_revision": revision,
        "observed_at": "2026-06-01T00:00:00Z", "rated_capacity_kwh": rated,
        "usable_capacity_kwh": usable, "cycle_count": cycles, "alerts": list(alerts),
        "recall_level": recall,
    }
    if blocked is not None:
        data["blocked_recall"] = blocked
    if recall != "none":
        data["recall_reference"] = "r-1"
    return data


RULEBOOK = {
    "rulebook_id": "rb", "version": 1,
    "degradation_weight": "0.4", "alert_weight": "0.3",
    "recall_weight": "0.2", "cycle_weight": "0.1",
    "degradation_threshold_percent": "20", "high_risk_score": 70, "horizon_days": 30,
}

ACTIVITY = {
    "activity_id": "act-1", "title": "检修", "required_certifications": ["hv"],
    "estimated_hours": 4, "required_spare_kinds": {"pack": 1},
    "isolation_required": True, "minimum_crew": 2,
}


class RiskTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rulebook = RiskRulebook.from_dict(RULEBOOK)

    def test_degradation_over_threshold_drives_high_score(self) -> None:
        ev = HealthEvidence.from_dict(evidence_dict(usable="700"))  # 30% 衰减 -> 分项 150 截顶 100
        scored = score_evidence(ev, self.rulebook)
        self.assertEqual(scored["components"]["degradation"], "100.0")
        self.assertGreaterEqual(scored["risk_score"], 40)

    def test_recall_mandatory_is_critical_band_even_with_low_score(self) -> None:
        ev = HealthEvidence.from_dict(evidence_dict(usable="990", recall="mandatory", blocked=True))
        scored = score_evidence(ev, self.rulebook)
        self.assertEqual(scored["risk_band"], "critical")
        self.assertIn("recall_block", scored["drivers"])
        self.assertTrue(scored["blocked"])

    def test_rank_is_deterministic_and_descending(self) -> None:
        rows = [
            HealthEvidence.from_dict(evidence_dict(battery="ba", usable="600")),
            HealthEvidence.from_dict(evidence_dict(battery="aa", usable="600")),
            HealthEvidence.from_dict(evidence_dict(battery="ca", usable="900")),
        ]
        first = rank(rows, self.rulebook)
        second = rank(list(reversed(rows)), self.rulebook)
        self.assertEqual([r["battery_id"] for r in first], [r["battery_id"] for r in second])
        self.assertEqual([r["battery_id"] for r in first[:2]], ["aa", "ba"])  # 同分按编号
        self.assertEqual([r["ranking"] for r in first], [1, 2, 3])


class SchedulerTests(unittest.TestCase):
    def _ranked(self, items):
        return [
            {"battery_id": bid, "facility_id": "f1", "risk_score": score, "risk_band": "high",
             "ranking": i + 1, "slack_days": 0, "blocked": False}
            for i, (bid, score) in enumerate(items)
        ]

    def _evidence(self, batteries):
        return {bid: {"rated_capacity_kwh": "1000"} for bid in batteries}

    def test_quota_caps_same_day_capacity_and_spreads_to_next_day(self) -> None:
        ranked = self._ranked([("b1", 90), ("b2", 80), ("b3", 70)])
        result = build_schedule(
            ranked=ranked,
            evidence_by_battery=self._evidence(["b1", "b2", "b3"]),
            activity_by_battery={bid: ACTIVITY for bid in ("b1", "b2", "b3")},
            crews=[{"crew_id": "c1", "size": 2, "certifications": ["hv"]},
                   {"crew_id": "c2", "size": 2, "certifications": ["hv"]}],
            spare_availability={"pack": 5},
            facility_calendars={"f1": {"2026-06-08": Decimal("2500"), "2026-06-09": Decimal("2500")}},
            service_dates=["2026-06-08", "2026-06-09"],
        )
        by_day: dict[str, list[str]] = {}
        for window in result["windows"]:
            by_day.setdefault(window["service_date"], []).append(window["battery_id"])
        self.assertEqual(set(by_day["2026-06-08"]), {"b1", "b2"})
        self.assertEqual(by_day["2026-06-09"], ["b3"])  # 同一天不能停掉超过 2500kWh
        self.assertTrue(result["arbitrations"])

    def test_missing_certification_leaves_unscheduled(self) -> None:
        ranked = self._ranked([("b1", 90)])
        result = build_schedule(
            ranked=ranked, evidence_by_battery=self._evidence(["b1"]),
            activity_by_battery={"b1": ACTIVITY},
            crews=[{"crew_id": "c1", "size": 2, "certifications": []}],
            spare_availability={"pack": 1},
            facility_calendars={"f1": {"2026-06-08": Decimal("2500")}},
            service_dates=["2026-06-08"],
        )
        self.assertEqual(result["windows"], [])
        self.assertEqual(result["unscheduled"][0]["reasons"], ["no_qualified_crew"])

    def test_missing_spares_leaves_unscheduled(self) -> None:
        ranked = self._ranked([("b1", 90)])
        result = build_schedule(
            ranked=ranked, evidence_by_battery=self._evidence(["b1"]),
            activity_by_battery={"b1": ACTIVITY},
            crews=[{"crew_id": "c1", "size": 2, "certifications": ["hv"]}],
            spare_availability={"pack": 0},
            facility_calendars={"f1": {"2026-06-08": Decimal("2500")}},
            service_dates=["2026-06-08"],
        )
        self.assertEqual(result["windows"], [])
        self.assertIn("insufficient_spares", result["unscheduled"][0]["reasons"])

    def test_blocked_recall_never_scheduled(self) -> None:
        ranked = [{
            "battery_id": "b1", "facility_id": "f1", "risk_score": 100, "risk_band": "critical",
            "ranking": 1, "slack_days": 0, "blocked": True,
        }]
        result = build_schedule(
            ranked=ranked, evidence_by_battery=self._evidence(["b1"]),
            activity_by_battery={"b1": ACTIVITY},
            crews=[{"crew_id": "c1", "size": 2, "certifications": ["hv"]}],
            spare_availability={"pack": 1},
            facility_calendars={"f1": {"2026-06-08": Decimal("2500")}},
            service_dates=["2026-06-08"],
        )
        self.assertEqual(result["windows"], [])
        self.assertEqual(result["unscheduled"][0]["reasons"], ["recall_blocked"])


class ServiceWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 6, 1, 8, tzinfo=timezone.utc))
        self.service = MaintenanceService(self.connection, self.clock)
        for uid, role in (("plan", "planner"), ("boss", "approver"), ("risk", "risk"),
                          ("tech", "technician"), ("audit", "auditor")):
            self.service.create_user(uid, uid, role)
        self.service.publish_rulebook("plan", RULEBOOK)
        self.service.register_activity("plan", ACTIVITY)
        self.service.register_crew("plan", "c1", "一班", 2, ["hv"])
        self.service.register_crew("plan", "c2", "二班", 2, ["hv"])
        self.service.upsert_spare_stock("plan", "pack", 2)
        for day in ("2026-06-08", "2026-06-09"):
            self.service.set_facility_quota("plan", "f1", day, 2500)
        self.service.record_evidence("plan", evidence_dict(battery="b1", usable="700"))
        self.service.record_evidence("plan", evidence_dict(battery="b2", usable="820"))
        self.service.assign_activity("plan", "b1", "act-1")
        self.service.assign_activity("plan", "b2", "act-1")
        self.service.generate_plan("plan", {
            "plan_id": "p1", "title": "计划", "rulebook_id": "rb", "rulebook_version": 1,
            "horizon_start": "2026-06-08", "horizon_end": "2026-06-09",
        })

    def _approve(self):
        return self.service.approve_plan("boss", "p1", 1)

    def _window(self, battery):
        return self.connection.execute(
            "SELECT window_id FROM maintenance_windows WHERE battery_id=?", (battery,)
        ).fetchone()["window_id"]

    def test_approve_freezes_holds_and_blocks_reapproval_with_stale_revision(self) -> None:
        self._approve()
        with self.assertRaises(InvalidState):
            self.service.approve_plan("boss", "p1", 1)
        holds = self.connection.execute(
            "SELECT resource_type,count(*) c FROM resource_holds WHERE state='held' GROUP BY resource_type"
        ).fetchall()
        counts = {row["resource_type"]: row["c"] for row in holds}
        self.assertEqual(counts["facility_quota"], 2)
        self.assertEqual(counts["crew"], 2)

    def test_evidence_change_only_invalidates_affected_scheduled_window(self) -> None:
        self._approve()
        w1 = self._window("b1")
        w2 = self._window("b2")
        self.service.record_receipt("tech", w1, "start", "k-start")
        # b2 出现新证据版本：只有 b2 仍 scheduled 的窗口失效；已开工的 b1 不动。
        self.service.record_evidence("plan", evidence_dict(battery="b2", revision="ev-2", usable="600"))
        self.assertEqual(
            self.connection.execute("SELECT state FROM maintenance_windows WHERE window_id=?", (w2,)).fetchone()["state"],
            "invalidated",
        )
        self.assertEqual(
            self.connection.execute("SELECT state FROM maintenance_windows WHERE window_id=?", (w1,)).fetchone()["state"],
            "in_progress",
        )
        released = self.connection.execute(
            "SELECT count(*) FROM resource_holds WHERE window_id=? AND state='released'", (w2,)
        ).fetchone()[0]
        self.assertGreater(released, 0)

    def test_critical_alert_invalidates_but_warning_does_not(self) -> None:
        self._approve()
        w1 = self._window("b1")
        self.service.record_health_event("risk", {
            "battery_id": "b1", "kind": "alert", "severity": "warning",
            "reference": "a-warn", "observed_at": "2026-06-02T00:00:00Z"})
        self.assertEqual(
            self.connection.execute("SELECT state FROM maintenance_windows WHERE window_id=?", (w1,)).fetchone()["state"],
            "scheduled",
        )
        self.service.record_health_event("risk", {
            "battery_id": "b1", "kind": "alert", "severity": "critical",
            "reference": "a-crit", "observed_at": "2026-06-02T01:00:00Z"})
        self.assertEqual(
            self.connection.execute("SELECT state FROM maintenance_windows WHERE window_id=?", (w1,)).fetchone()["state"],
            "invalidated",
        )

    def test_stale_approval_rejected_after_evidence_changes(self) -> None:
        self.service.record_evidence("plan", evidence_dict(battery="b1", revision="ev-2", usable="650"))
        with self.assertRaises(InvalidState):
            self._approve()

    def test_duplicate_and_out_of_order_receipts_never_revive_closed_window(self) -> None:
        self._approve()
        w1 = self._window("b1")
        first = self.service.record_receipt("tech", w1, "start", "k1")
        duplicate = self.service.record_receipt("tech", w1, "start", "k1")
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["state"], first["state"])
        # 未开始先完工：拒绝且状态不变。
        w2 = self._window("b2")
        early = self.service.record_receipt("tech", w2, "complete", "k-early")
        self.assertFalse(early["accepted"])
        self.assertEqual(early["state"], "scheduled")
        # 暂停未开始窗口：拒绝。
        self.assertFalse(self.service.record_receipt("tech", w2, "pause", "k-pause-early")["accepted"])
        # 正常流转到复测通过后，迟到开始/完工不能复活。
        self.service.record_receipt("tech", w1, "pause", "k2")
        self.service.record_receipt("tech", w1, "resume", "k3")
        self.service.record_receipt("tech", w1, "complete", "k4")
        self.service.record_receipt("tech", w1, "retest", "k5", {"passed": True})
        zombie = self.service.record_receipt("tech", w1, "start", "k-zombie")
        self.assertFalse(zombie["accepted"])
        final = self.connection.execute("SELECT state FROM maintenance_windows WHERE window_id=?", (w1,)).fetchone()["state"]
        self.assertEqual(final, "retest_passed")

    def test_retest_failure_requires_rework_and_rejects_late_retest(self) -> None:
        self._approve()
        w1 = self._window("b1")
        self.service.record_receipt("tech", w1, "start", "k1")
        self.service.record_receipt("tech", w1, "complete", "k2")
        self.service.record_receipt("tech", w1, "retest", "k3", {"passed": False})
        late = self.service.record_receipt("tech", w1, "retest", "k4", {"passed": True})
        self.assertFalse(late["accepted"])  # 终态不接受二次复测
        self.assertEqual(late["state"], "retest_failed")

    def test_postponement_requires_risk_acceptance_and_records_capacity_risk(self) -> None:
        self._approve()
        w1 = self._window("b1")
        request = self.service.request_postponement("plan", w1, "2026-06-09", "2026-06-11", "暴雨")
        self.assertEqual(request["state"], "pending")
        # 接受前日期不动。
        self.assertEqual(
            self.connection.execute("SELECT service_date FROM maintenance_windows WHERE window_id=?", (w1,)).fetchone()["service_date"],
            "2026-06-08",
        )
        # 技术员无权接受风险。
        with self.assertRaises(Forbidden):
            self.service.resolve_postponement("tech", request["postponement_id"], True, "")
        accepted = self.service.resolve_postponement("risk", request["postponement_id"], True, "可接受")
        self.assertEqual(accepted["service_date"], "2026-06-09")
        self.assertEqual(accepted["risk_accepter_id"], "risk")
        self.assertEqual(
            self.connection.execute("SELECT service_date FROM maintenance_windows WHERE window_id=?", (w1,)).fetchone()["service_date"],
            "2026-06-09",
        )

    def test_postponement_rejected_when_new_date_quota_taken(self) -> None:
        self._approve()
        w1 = self._window("b1")
        # 把 6 月 9 日额度收紧到 1，既有 b2 占用 1000，新日期已无余量。
        self.service.set_facility_quota("plan", "f1", "2026-06-09", 1)
        with self.assertRaises(Conflict):
            self.service.request_postponement("plan", w1, "2026-06-09", "2026-06-11", "暴雨")

    def test_dashboard_lists_pending_actions_and_explains_window(self) -> None:
        self._approve()
        w1 = self._window("b1")
        self.service.record_receipt("tech", w1, "start", "k1")
        self.service.record_receipt("tech", w1, "pause", "k2")
        dashboard = self.service.operations_dashboard("plan", "p1")
        actions = {(a["window_id"], a["action"]) for a in dashboard["pending_actions"]}
        self.assertIn((w1, "awaiting_resume"), actions)
        detail = self.service.window_detail("plan", w1)
        self.assertTrue(detail["selection_reasons"])
        self.assertTrue(detail["isolation_required"])

    def test_audit_chain_validates(self) -> None:
        self._approve()
        self.assertTrue(self.service.audit_chain("audit")["valid"])


if __name__ == "__main__":
    unittest.main()
