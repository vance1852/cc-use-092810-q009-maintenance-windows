"""维修计划 HTTP JSON 接口测试。"""

from __future__ import annotations

import json
import sqlite3
import unittest

from battery_maintenance.api import JsonApplication
from battery_maintenance.service import MaintenancePlanService


class MaintenanceApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.app = JsonApplication(MaintenancePlanService(self.connection))

    def tearDown(self) -> None:
        self.connection.close()

    def _post(self, path: str, payload: dict, actor: str = "planner"):
        return self.app.handle(
            "POST", path,
            headers={"X-Actor-Id": actor, "Content-Type": "application/json"},
            body=json.dumps(payload).encode("utf-8"),
        )

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")

    def test_requires_actor_header(self) -> None:
        response = self.app.handle("POST", "/stations", body=b"{}")
        self.assertEqual(response.status, 422)

    def test_station_and_device_flow(self) -> None:
        self._post("/users", {"user_id": "planner", "display_name": "p", "role": "planner"})
        response = self._post("/stations", {
            "station_id": "s1", "name": "场站", "timezone": "Asia/Shanghai",
            "daily_outage_kwh": "1000",
        })
        self.assertEqual(response.status, 201)
        missing = self._post("/devices", {
            "device_id": "d1", "station_id": "missing", "device_kind": "pack",
            "model_name": "m", "rated_capacity_kwh": "500",
            "required_qualification": "hv",
        })
        self.assertEqual(missing.status, 404)

    def test_unknown_route(self) -> None:
        self._post("/users", {"user_id": "planner", "display_name": "p", "role": "planner"})
        response = self.app.handle("GET", "/nope", headers={"X-Actor-Id": "planner"})
        self.assertEqual(response.status, 404)


if __name__ == "__main__":
    unittest.main()
