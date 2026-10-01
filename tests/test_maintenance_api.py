from __future__ import annotations

import json
import sqlite3
import unittest

from maintenance_planning.api import JsonApplication
from maintenance_planning.service import MaintenanceService


class MaintenanceApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.app = JsonApplication(MaintenanceService(self.connection))
        self.post("/users", {"user_id": "plan", "display_name": "计划", "role": "planner"})
        self.post("/users", {"user_id": "boss", "display_name": "审批", "role": "approver"})

    def tearDown(self) -> None:
        self.connection.close()

    def post(self, path: str, payload: dict, actor: str = "plan"):
        body = json.dumps(payload, ensure_ascii=False).encode()
        return self.app.handle("POST", path, {"X-Actor-Id": actor}, body)

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")

    def test_missing_actor_is_rejected(self) -> None:
        response = self.app.handle("POST", "/rulebooks", {}, b"{}")
        self.assertEqual(response.status, 422)

    def test_full_plan_lifecycle_over_http(self) -> None:
        rulebook = {
            "rulebook_id": "rb", "version": 1,
            "degradation_weight": "0.4", "alert_weight": "0.3",
            "recall_weight": "0.2", "cycle_weight": "0.1",
            "degradation_threshold_percent": "20", "high_risk_score": 70, "horizon_days": 30,
        }
        self.assertEqual(self.post("/rulebooks", rulebook).status, 201)
        activity = {
            "activity_id": "act-1", "title": "检修",
            "required_certifications": ["hv"], "estimated_hours": 4,
            "required_spare_kinds": {"pack": 1}, "isolation_required": True, "minimum_crew": 2,
        }
        self.assertEqual(self.post("/activities", activity).status, 201)
        self.assertEqual(self.post("/crews", {
            "crew_id": "c1", "display_name": "一班", "size": 2, "certifications": ["hv"]}).status, 201)
        self.assertEqual(self.post("/spares", {"spare_kind": "pack", "available_quantity": 1}).status, 200)
        self.assertEqual(self.post("/facilities/f1/quota", {
            "service_date": "2026-06-08", "shutdown_quota_kwh": 2500}).status, 200)
        evidence = {
            "battery_id": "bat-1", "facility_id": "f1", "evidence_revision": "ev-1",
            "observed_at": "2026-06-01T00:00:00Z", "rated_capacity_kwh": "1000",
            "usable_capacity_kwh": "700", "cycle_count": 1000, "alerts": [],
            "recall_level": "none",
        }
        self.assertEqual(self.post("/evidence", evidence).status, 201)
        self.assertEqual(self.post("/batteries/bat-1/activity", {"activity_id": "act-1"}).status, 200)
        generated = self.post("/plans/generate", {
            "plan_id": "p1", "title": "计划", "rulebook_id": "rb", "rulebook_version": 1,
            "horizon_start": "2026-06-08", "horizon_end": "2026-06-08"})
        self.assertEqual(generated.status, 200)
        self.assertEqual(len(generated.body["windows"]), 1)
        approved = self.post("/plans/p1/approve", {"expected_revision": 1}, actor="boss")
        self.assertEqual(approved.status, 200)
        detail = self.app.handle("GET", "/windows/1", {"X-Actor-Id": "plan"})
        self.assertEqual(detail.body["state"], "scheduled")
        self.assertTrue(detail.body["selection_reasons"])

    def test_unknown_route(self) -> None:
        response = self.app.handle("GET", "/nope", {"X-Actor-Id": "plan"})
        self.assertEqual(response.status, 404)


if __name__ == "__main__":
    unittest.main()
