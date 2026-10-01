from __future__ import annotations

import unittest
from pathlib import Path

from battery_maintenance.acceptance import run


ROOT = Path(__file__).resolve().parents[1]


class MaintenanceAcceptanceTests(unittest.TestCase):
    def test_offline_acceptance(self) -> None:
        result = run(ROOT)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["schema"]["missing_tables"], [])
        self.assertEqual(len(result["input_sha256"]), 64)
        # 风险优先级：强制召回 + 严重衰减排最前
        self.assertEqual(result["window_order"][0], "pack-1")
        # 缺备件的设备进入不可排程清单而不是被静默丢弃
        self.assertEqual(result["unscheduled"][0]["device_id"], "pack-5")
        self.assertIn("spare_part:sku-bms-board", result["unscheduled"][0]["reasons"])
        # 批准冻结
        self.assertEqual(result["frozen_evidence_count"], 5)
        self.assertTrue(result["freeze_rejected_after_change"])
        # 回执去重、乱序、终态保护
        self.assertTrue(result["receipts"]["duplicate_replayed_same"])
        self.assertTrue(result["receipts"]["out_of_order_completed_rejected"])
        self.assertTrue(result["receipts"]["late_start_after_close_rejected"])
        # 信号只影响受影响的未执行窗口
        self.assertEqual(result["signals"]["evidence_changed"], ["plan-summer-1:pack-2"])
        self.assertEqual(result["signals"]["urgent_alarm"], ["plan-summer-1:pack-3"])
        self.assertTrue(result["signals"]["started_window_survives"])
        # 延期记录风险接受人
        self.assertEqual(result["extension"]["risk_acceptor_id"], "risk")
        self.assertEqual(result["extension"]["exposure_kwh_days"], "2500")
        # 审计哈希链完整
        self.assertTrue(result["audit"]["valid"])


if __name__ == "__main__":
    unittest.main()
