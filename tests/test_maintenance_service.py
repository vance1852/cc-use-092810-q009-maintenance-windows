"""维修计划服务的事务、冻结、失效、回执与延期规则测试。"""

from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone

from battery_maintenance.clock import FrozenClock
from battery_maintenance.errors import Conflict, Forbidden, InvalidState, ValidationFailed
from battery_maintenance.service import MaintenancePlanService


RULE_SET = {
    "rule_set_id": "rules",
    "version": "v1",
    "retention_thresholds": {"high": "85", "critical": "75"},
    "severity_weights": {"info": 0, "warning": 15, "critical": 40},
    "recall_scores": {"none": 0, "monitor": 10, "hold": 45, "mandatory": 80},
    "alarm_count_weight": 2,
    "cycle_weight": 0,
    "deadline_days": {"critical": 3, "high": 7, "medium": 14, "low": 30},
}


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 6, 1, 6, 0, tzinfo=timezone.utc))
        self.service = MaintenancePlanService(self.connection, self.clock)
        for user_id, role in (
            ("planner", "planner"), ("risk", "risk"), ("auditor", "auditor"),
            ("tech-1", "technician"), ("tech-2", "technician"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.service.register_station("planner", {
            "station_id": "s1", "name": "场站", "timezone": "Asia/Shanghai",
            "daily_outage_kwh": "1000",
        })
        self.service.register_device("planner", {
            "device_id": "d1", "station_id": "s1", "device_kind": "pack", "model_name": "m",
            "rated_capacity_kwh": "500", "required_qualification": "hv",
            "required_isolation": ["electrical"], "required_spare_skus": [],
        })
        self.service.register_device("planner", {
            "device_id": "d2", "station_id": "s1", "device_kind": "pack", "model_name": "m",
            "rated_capacity_kwh": "500", "required_qualification": "hv",
            "required_isolation": [], "required_spare_skus": [],
        })
        self.service.record_evidence("planner", {
            "evidence_id": "e1", "device_id": "d1", "version": "v1",
            "observed_at": "2026-05-30T08:00:00Z", "capacity_retention_percent": "70",
            "cycle_count": 100, "alarm_severity": "critical", "alarm_count_30d": 9,
            "recall_level": "mandatory", "recall_code": "rc-1", "note": "",
        })
        self.service.record_evidence("planner", {
            "evidence_id": "e2", "device_id": "d2", "version": "v1",
            "observed_at": "2026-05-30T08:00:00Z", "capacity_retention_percent": "90",
            "cycle_count": 100, "alarm_severity": "info", "alarm_count_30d": 0,
            "recall_level": "none", "note": "",
        })
        self.service.publish_rule_set("risk", RULE_SET)
        self.service.register_technician("planner", {
            "technician_id": "tech-1", "display_name": "一", "qualifications": ["hv"],
        })
        self.service.register_technician("planner", {
            "technician_id": "tech-2", "display_name": "二", "qualifications": ["hv"],
        })
        self.service.register_bay("planner", {
            "bay_id": "b1", "station_id": "s1", "isolation_kinds": ["electrical"],
        })

    def tearDown(self) -> None:
        self.connection.close()

    def _plan(self, plan_id: str = "plan-1", key: str = "key-1", horizon: int = 14) -> dict:
        return self.service.generate_plan("planner", {
            "plan_id": plan_id, "rule_set_id": "rules", "horizon_days": horizon,
            "idempotency_key": key,
        })

    def test_plan_prioritises_critical_device_first(self) -> None:
        plan = self._plan()
        self.assertEqual([item["device_id"] for item in plan["windows"]], ["d1", "d2"])
        self.assertEqual(plan["windows"][0]["risk_level"], "critical")
        self.assertEqual(plan["windows"][0]["technician_id"], "tech-1")
        self.assertEqual(plan["windows"][0]["bay_id"], "b1")

    def test_generate_plan_is_idempotent(self) -> None:
        first = self._plan()
        second = self._plan()
        self.assertEqual(first, second)

    def test_approve_freezes_windows_and_reservations(self) -> None:
        self._plan()
        result = self.service.approve_plan("risk", "plan-1", 1)
        self.assertEqual(result["state"], "approved")
        held = self.connection.execute(
            "SELECT resource_type,count(*) c FROM resource_reservations WHERE state='held' "
            "GROUP BY resource_type ORDER BY resource_type"
        ).fetchall()
        counts = {row["resource_type"]: row["c"] for row in held}
        self.assertEqual(counts["station_outage"], 2)
        self.assertEqual(counts["technician"], 2)
        self.assertEqual(counts["bay"], 1)
        snapshot = self.connection.execute(
            "SELECT count(*) c FROM plan_evidence_snapshot WHERE plan_id='plan-1'"
        ).fetchone()["c"]
        self.assertEqual(snapshot, 2)

    def test_approve_rejects_stale_revision(self) -> None:
        self._plan()
        with self.assertRaises(InvalidState):
            self.service.approve_plan("risk", "plan-1", 99)

    def test_approve_rejected_after_evidence_changes(self) -> None:
        self._plan()
        self.service.record_evidence("planner", {
            "evidence_id": "e1b", "device_id": "d1", "version": "v2",
            "observed_at": "2026-05-31T08:00:00Z", "capacity_retention_percent": "60",
            "cycle_count": 100, "alarm_severity": "critical", "alarm_count_30d": 20,
            "recall_level": "mandatory", "note": "",
        })
        with self.assertRaises(InvalidState):
            self.service.approve_plan("risk", "plan-1", 1)

    def test_second_plan_cannot_overbook_station_quota(self) -> None:
        self._plan()
        self.service.approve_plan("risk", "plan-1", 1)
        # 已批准计划之外新增高风险设备；排程引擎会避让已占用容量
        self.service.register_device("planner", {
            "device_id": "d3", "station_id": "s1", "device_kind": "pack", "model_name": "m",
            "rated_capacity_kwh": "600", "required_qualification": "hv",
            "required_isolation": [], "required_spare_skus": [],
        })
        self.service.record_evidence("planner", {
            "evidence_id": "e3", "device_id": "d3", "version": "v1",
            "observed_at": "2026-05-30T09:00:00Z", "capacity_retention_percent": "70",
            "cycle_count": 100, "alarm_severity": "critical", "alarm_count_30d": 9,
            "recall_level": "mandatory", "note": "",
        })
        plan_two = self.service.generate_plan("planner", {
            "plan_id": "plan-2", "rule_set_id": "rules", "horizon_days": 14,
            "idempotency_key": "key-2",
        })
        # 生成后指派技师被临时安排培训，批准时必须再次校验并拒绝
        assigned = plan_two["windows"][0]["technician_id"]
        assigned_day = plan_two["windows"][0]["service_date"]
        self.service.add_unavailability("planner", assigned, {
            "starts_at": f"{assigned_day}T00:00:00Z",
            "ends_at": f"{assigned_day}T23:59:59Z",
            "reason": "紧急借调",
        })
        with self.assertRaises(Conflict):
            self.service.approve_plan("risk", "plan-2", 1)

    def test_role_separation(self) -> None:
        self._plan()
        with self.assertRaises(Forbidden):
            self.service.approve_plan("planner", "plan-1", 1)
        with self.assertRaises(Forbidden):
            self.service.receive_receipt("planner", "plan-1:d1", "started", "k")

    def _approved_window(self, device_id: str) -> str:
        self._plan()
        self.service.approve_plan("risk", "plan-1", 1)
        return f"plan-1:{device_id}"

    def test_receipt_lifecycle_dedup_and_ordering(self) -> None:
        wid = self._approved_window("d1")
        self.assertEqual(self.service.receive_receipt("tech-1", wid, "started", "r1")["state_after"],
                         "in_progress")
        replay = self.service.receive_receipt("tech-1", wid, "started", "r1")
        self.assertEqual(replay["receipt_id"], 1)
        with self.assertRaises(InvalidState):
            self.service.receive_receipt("tech-1", wid, "started", "r-dup")
        self.service.receive_receipt("tech-1", wid, "paused", "r2")
        with self.assertRaises(InvalidState):
            self.service.receive_receipt("tech-1", wid, "completed", "r-x")
        self.service.receive_receipt("tech-1", wid, "resumed", "r3")
        self.service.receive_receipt("tech-1", wid, "completed", "r4")
        self.service.receive_receipt("tech-1", wid, "retest", "r5")
        # 已关闭工单不能被迟到消息恢复
        with self.assertRaises(InvalidState):
            self.service.receive_receipt("tech-1", wid, "started", "r-late")
        state = self.connection.execute(
            "SELECT state FROM maintenance_windows WHERE window_id=?", (wid,)
        ).fetchone()["state"]
        self.assertEqual(state, "completed")

    def test_only_assigned_technician_may_send_receipt(self) -> None:
        wid = self._approved_window("d1")
        with self.assertRaises(Forbidden):
            self.service.receive_receipt("tech-2", wid, "started", "r1")

    def test_invalidation_only_hits_affected_unexecuted_windows(self) -> None:
        wid_d1 = self._approved_window("d1")
        wid_d2 = "plan-1:d2"
        self.service.receive_receipt("tech-1", wid_d1, "started", "r1")
        signal = self.service.record_signal("planner", "d2", "urgent_alarm", {"code": "thermal"})
        self.assertEqual(signal["invalidated_windows"], [wid_d2])
        rows = {
            row["window_id"]: row["state"]
            for row in self.connection.execute("SELECT window_id,state FROM maintenance_windows")
        }
        self.assertEqual(rows[wid_d1], "in_progress")
        self.assertEqual(rows[wid_d2], "invalidated")
        released = self.connection.execute(
            "SELECT count(*) c FROM resource_reservations WHERE window_id=? AND state='released'",
            (wid_d2,),
        ).fetchone()["c"]
        self.assertGreater(released, 0)
        # 已开工窗口不受同设备新信号影响（此处对 d1 发信号）
        again = self.service.record_signal("planner", "d1", "urgent_alarm", {})
        self.assertEqual(again["invalidated_windows"], [])

    def test_evidence_changed_without_new_version_does_not_invalidate(self) -> None:
        wid_d2 = self._approved_window("d2")
        result = self.service.record_signal("planner", "d2", "evidence_changed", {})
        self.assertEqual(result["invalidated_windows"], [])
        state = self.connection.execute(
            "SELECT state FROM maintenance_windows WHERE window_id=?", (wid_d2,)
        ).fetchone()["state"]
        self.assertEqual(state, "approved")

    def test_extension_requires_risk_acceptor_and_records_exposure(self) -> None:
        wid = self._approved_window("d2")
        with self.assertRaises(ValidationFailed):
            self.service.extend_window("planner", wid, "2026-07-10", "原因", "planner")
        result = self.service.extend_window("planner", wid, "2026-07-10", "备件延迟", "risk")
        self.assertEqual(result["risk_acceptor_id"], "risk")
        self.assertEqual(result["exposure_kwh_days"], "4500")
        with self.assertRaises(ValidationFailed):
            self.service.extend_window("planner", wid, "2026-07-09", "倒退", "risk")
        extension = self.connection.execute(
            "SELECT * FROM window_extensions WHERE window_id=?", (wid,)
        ).fetchone()
        self.assertEqual(extension["previous_latest_date"], "2026-07-01")
        self.assertEqual(extension["new_latest_date"], "2026-07-10")
        self.assertEqual(extension["risk_acceptor_id"], "risk")

    def test_completed_window_cannot_be_extended(self) -> None:
        wid = self._approved_window("d2")
        self.service.receive_receipt("tech-2", wid, "started", "r1")
        self.service.receive_receipt("tech-2", wid, "completed", "r2")
        with self.assertRaises(InvalidState):
            self.service.extend_window("planner", wid, "2026-07-01", "x", "risk")

    def test_operations_view_reports_pending_actions_and_overdue(self) -> None:
        wid_d1 = self._approved_window("d1")
        self.service.receive_receipt("tech-1", wid_d1, "started", "r1")
        self.service.receive_receipt("tech-1", wid_d1, "paused", "r2")
        self.clock.advance(days=10)
        view = self.service.operations_view("auditor")
        actions = {item["window_id"]: item["action"] for item in view["pending_actions"]}
        self.assertEqual(actions[wid_d1], "resume_work")
        self.assertIn("plan-1:d2", actions)
        overdue_devices = {item["window_id"] for item in view["overdue_windows"]}
        self.assertIn(wid_d1, overdue_devices)
        item = next(window for window in view["windows"] if window["window_id"] == wid_d1)
        self.assertIn("total_points", item["why"]["factors"])
        self.assertEqual(item["receipts"][0]["event"], "started")

    def test_audit_chain_is_valid(self) -> None:
        self._plan()
        self.service.approve_plan("risk", "plan-1", 1)
        audit = self.service.audit_chain("auditor")
        self.assertTrue(audit["valid"])
        self.assertGreater(audit["events"], 0)


if __name__ == "__main__":
    unittest.main()
