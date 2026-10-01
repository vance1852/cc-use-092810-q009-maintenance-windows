"""维修风险评分与排程引擎的确定性规则测试。"""

from __future__ import annotations

import unittest
from datetime import date, timedelta
from decimal import Decimal

from battery_maintenance.models import Device, HealthEvidence, RiskRuleSet
from battery_maintenance.planning import ResourceSnapshot, schedule_windows, score_evidence


RULES = RiskRuleSet.from_dict({
    "rule_set_id": "rules",
    "version": "1",
    "retention_thresholds": {"high": "85", "critical": "75"},
    "severity_weights": {"info": 0, "warning": 15, "critical": 40},
    "recall_scores": {"none": 0, "monitor": 10, "hold": 45, "mandatory": 80},
    "alarm_count_weight": 2,
    "cycle_weight": 0,
    "deadline_days": {"critical": 3, "high": 7, "medium": 14, "low": 30},
})

ANCHOR = date(2026, 6, 1)


def evidence(evidence_id: str, device_id: str, **overrides) -> HealthEvidence:
    raw = {
        "evidence_id": evidence_id,
        "device_id": device_id,
        "version": "v1",
        "observed_at": "2026-05-30T08:00:00Z",
        "capacity_retention_percent": "95",
        "cycle_count": 100,
        "alarm_severity": "info",
        "alarm_count_30d": 0,
        "recall_level": "none",
    }
    raw.update(overrides)
    return HealthEvidence.from_dict(raw)


def device(device_id: str, *, capacity: str = "500", qualification: str = "hv",
           isolation=frozenset(), spares=frozenset()) -> Device:
    return Device(
        device_id=device_id, station_id="s1", device_kind="pack", model_name="m",
        rated_capacity_kwh=Decimal(capacity), required_qualification=qualification,
        required_isolation=isolation, required_spare_skus=spares,
    )


def resources(*, technicians=("t1",), unavailable=None, bays=(),
              busy_bays=frozenset(), spares=None) -> ResourceSnapshot:
    return ResourceSnapshot(
        station_quota_by_date={"s1": {}},
        technicians=[{"technician_id": tid, "qualifications": frozenset({"hv"})} for tid in technicians],
        unavailable=unavailable or {},
        bays=[
            {"bay_id": bid, "station_id": "s1", "isolation_kinds": frozenset({"electrical"})}
            for bid in bays
        ],
        busy_bays=busy_bays,
        spare_availability=spares or {},
    )


def quota_for(resources_obj: ResourceSnapshot, dates: list[str], quota: str) -> ResourceSnapshot:
    return ResourceSnapshot(
        station_quota_by_date={"s1": {day: Decimal(quota) for day in dates}},
        technicians=resources_obj.technicians,
        unavailable=resources_obj.unavailable,
        bays=resources_obj.bays,
        busy_bays=resources_obj.busy_bays,
        spare_availability=resources_obj.spare_availability,
    )


class RiskScoreTests(unittest.TestCase):
    def test_capacity_retention_drives_critical_level(self) -> None:
        score = score_evidence(evidence("ev", "dev", capacity_retention_percent="70"), RULES, anchor_date=ANCHOR)
        self.assertEqual(score.level, "critical")
        self.assertEqual(score.factors["retention_level"], "critical")
        self.assertEqual(score.latest_date, "2026-06-04")

    def test_mandatory_recall_is_critical_regardless_of_retention(self) -> None:
        score = score_evidence(
            evidence("ev", "dev", capacity_retention_percent="99", recall_level="mandatory"),
            RULES, anchor_date=ANCHOR,
        )
        self.assertEqual(score.level, "critical")

    def test_warning_and_alarm_count_reach_high(self) -> None:
        score = score_evidence(
            evidence("ev", "dev", capacity_retention_percent="90", alarm_severity="warning",
                     alarm_count_30d=20),
            RULES, anchor_date=ANCHOR,
        )
        self.assertEqual(score.level, "high")
        self.assertEqual(score.factors["alarm_count_penalty"], 40)

    def test_scoring_is_deterministic(self) -> None:
        ev = evidence("ev", "dev", capacity_retention_percent="80", alarm_severity="warning")
        first = score_evidence(ev, RULES, anchor_date=ANCHOR)
        second = score_evidence(ev, RULES, anchor_date=ANCHOR)
        self.assertEqual(first.as_dict(), second.as_dict())


class SchedulingTests(unittest.TestCase):
    def _dates(self, days: int = 14) -> list[str]:
        return [(date(2026, 6, 1) + timedelta(days=i)).isoformat() for i in range(days)]

    def test_station_daily_quota_spreads_windows(self) -> None:
        dates = self._dates()
        res = quota_for(resources(technicians=("t1", "t2")), dates, "600")
        devices = {"dev-a": device("dev-a"), "dev-b": device("dev-b")}
        scores = [
            score_evidence(evidence("ev-a", "dev-a", capacity_retention_percent="70"), RULES, anchor_date=ANCHOR),
            score_evidence(evidence("ev-b", "dev-b", capacity_retention_percent="80"), RULES, anchor_date=ANCHOR),
        ]
        result = schedule_windows(scores=scores, devices=devices, rules=RULES, resources=res,
                                  anchor_date=ANCHOR, horizon_days=14)
        days = {item["device_id"]: item["service_date"] for item in result["windows"]}
        self.assertEqual(days["dev-a"], "2026-06-01")
        self.assertEqual(days["dev-b"], "2026-06-02")

    def test_higher_risk_wins_contested_day(self) -> None:
        dates = self._dates()
        res = quota_for(resources(technicians=("t1", "t2")), dates, "500")
        devices = {"low": device("low"), "crit": device("crit")}
        scores = [
            score_evidence(evidence("ev-low", "low", capacity_retention_percent="99"), RULES, anchor_date=ANCHOR),
            score_evidence(evidence("ev-crit", "crit", capacity_retention_percent="70"), RULES, anchor_date=ANCHOR),
        ]
        result = schedule_windows(scores=scores, devices=devices, rules=RULES, resources=res,
                                  anchor_date=ANCHOR, horizon_days=14)
        first = result["windows"][0]
        self.assertEqual(first["device_id"], "crit")
        self.assertEqual(first["service_date"], "2026-06-01")
        self.assertEqual(result["windows"][1]["device_id"], "low")

    def test_unscheduled_when_resources_never_available(self) -> None:
        dates = self._dates(3)
        res = quota_for(resources(technicians=(), ), dates, "1000")
        devices = {"dev-a": device("dev-a")}
        scores = [score_evidence(evidence("ev-a", "dev-a", capacity_retention_percent="70"), RULES,
                                 anchor_date=ANCHOR)]
        result = schedule_windows(scores=scores, devices=devices, rules=RULES, resources=res,
                                  anchor_date=ANCHOR, horizon_days=3)
        self.assertEqual(result["windows"], [])
        self.assertEqual(len(result["unscheduled"]), 1)
        all_reasons = {reason for slot in result["unscheduled"][0]["rejected_slots"]
                       for reason in slot["reasons"]}
        self.assertIn("qualified_technician", all_reasons)

    def test_isolation_bay_and_spare_constraints(self) -> None:
        dates = self._dates()
        res = quota_for(resources(technicians=("t1",), bays=(), spares={}), dates, "1000")
        devices = {"dev-a": device("dev-a", isolation=frozenset({"electrical"}),
                               spares=frozenset({"sku-x"}))}
        scores = [score_evidence(evidence("ev-a", "dev-a", capacity_retention_percent="70"), RULES,
                                 anchor_date=ANCHOR)]
        result = schedule_windows(scores=scores, devices=devices, rules=RULES, resources=res,
                                  anchor_date=ANCHOR, horizon_days=14)
        self.assertEqual(len(result["unscheduled"]), 1)
        reasons = result["unscheduled"][0]["rejected_slots"][0]["reasons"]
        self.assertIn("isolation_bay", reasons)
        self.assertTrue(any(item.startswith("spare_part:") for item in reasons))

    def test_existing_held_bay_is_busy(self) -> None:
        dates = self._dates(2)
        res = ResourceSnapshot(
            station_quota_by_date={"s1": {d: Decimal("1000") for d in dates}},
            technicians=[{"technician_id": "t1", "qualifications": frozenset({"hv"})}],
            unavailable={},
            bays=[{"bay_id": "b1", "station_id": "s1",
                   "isolation_kinds": frozenset({"electrical"})}],
            busy_bays=frozenset({("b1", "2026-06-01")}),
            spare_availability={},
        )
        devices = {"dev-a": device("dev-a", isolation=frozenset({"electrical"}))}
        scores = [score_evidence(evidence("ev-a", "dev-a", capacity_retention_percent="70"), RULES,
                                 anchor_date=ANCHOR)]
        result = schedule_windows(scores=scores, devices=devices, rules=RULES, resources=res,
                                  anchor_date=ANCHOR, horizon_days=2)
        self.assertEqual(result["windows"][0]["service_date"], "2026-06-02")


if __name__ == "__main__":
    unittest.main()
