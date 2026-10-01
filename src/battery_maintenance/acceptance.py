"""贯通健康证据、风险排程、批准冻结、信号失效、现场回执与延期的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .errors import InvalidState
from .service import MaintenancePlanService
from .storage import inspect_schema


RULE_SET = {
    "rule_set_id": "summer-peak-rules",
    "version": "2026.1",
    "retention_thresholds": {"high": "85", "critical": "75"},
    "severity_weights": {"info": 0, "warning": 15, "critical": 40},
    "recall_scores": {"none": 0, "monitor": 10, "hold": 45, "mandatory": 80},
    "alarm_count_weight": 2,
    "cycle_weight": 0,
    "deadline_days": {"critical": 3, "high": 7, "medium": 14, "low": 30},
}


def _evidence(evidence_id: str, device_id: str, version: str, retention: str,
              severity: str = "info", alarm_count: int = 0, recall: str = "none",
              recall_code: str | None = None) -> dict[str, object]:
    return {
        "evidence_id": evidence_id,
        "device_id": device_id,
        "version": version,
        "observed_at": "2026-05-30T08:00:00Z",
        "capacity_retention_percent": retention,
        "cycle_count": 1200,
        "alarm_severity": severity,
        "alarm_count_30d": alarm_count,
        "recall_level": recall,
        "recall_code": recall_code,
        "note": "迎峰度夏前专项检测",
    }


def _device(device_id: str, station_id: str, capacity: str, *, qualification: str = "hv",
            isolation: list[str] | None = None, spares: list[str] | None = None) -> dict[str, object]:
    return {
        "device_id": device_id,
        "station_id": station_id,
        "device_kind": "pack",
        "model_name": "LFP-280Ah",
        "rated_capacity_kwh": capacity,
        "required_qualification": qualification,
        "required_isolation": isolation or [],
        "required_spare_skus": spares or [],
    }


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    clock = FrozenClock(datetime(2026, 6, 1, 6, 0, tzinfo=timezone.utc))
    service = MaintenancePlanService(connection, clock)

    for user_id, role in (
        ("planner", "planner"), ("risk", "risk"), ("auditor", "auditor"),
        ("tech-1", "technician"), ("tech-2", "technician"),
    ):
        service.create_user(user_id, user_id, role)

    service.register_station("planner", {
        "station_id": "station-a", "name": "北部迎峰度夏场站",
        "timezone": "Asia/Shanghai", "daily_outage_kwh": "1000",
    })
    devices = (
        _device("pack-1", "station-a", "500", isolation=["electrical"]),
        _device("pack-2", "station-a", "500"),
        _device("pack-3", "station-a", "500"),
        _device("pack-4", "station-a", "500"),
        _device("pack-5", "station-a", "400", spares=["sku-bms-board"]),
    )
    for device in devices:
        service.register_device("planner", device)

    evidence_rows = (
        _evidence("ev-1", "pack-1", "v1", "72", "critical", 9, "mandatory", "recall-2026-017"),
        _evidence("ev-2", "pack-2", "v1", "82", "warning", 4),
        _evidence("ev-3", "pack-3", "v1", "95", "info", 0),
        _evidence("ev-4", "pack-4", "v1", "88", "warning", 2, "monitor"),
        _evidence("ev-5", "pack-5", "v1", "70", "critical", 12, "hold"),
    )
    for row in evidence_rows:
        service.record_evidence("planner", row)

    service.publish_rule_set("risk", RULE_SET)

    service.register_technician("planner", {
        "technician_id": "tech-1", "display_name": "技师一", "qualifications": ["hv"],
    })
    service.register_technician("planner", {
        "technician_id": "tech-2", "display_name": "技师二", "qualifications": ["hv"],
    })
    service.add_unavailability("planner", "tech-2", {
        "starts_at": "2026-06-01T00:00:00Z", "ends_at": "2026-06-02T00:00:00Z",
        "reason": "培训",
    })
    service.register_bay("planner", {
        "bay_id": "bay-a", "station_id": "station-a", "isolation_kinds": ["electrical", "thermal"],
    })
    service.restock_part("planner", {
        "station_id": "station-a", "sku": "sku-bms-board", "quantity_on_hand": 0,
    })

    # 1) 生成待办优先级与维修窗口
    plan = service.generate_plan("planner", {
        "plan_id": "plan-summer-1", "rule_set_id": "summer-peak-rules",
        "horizon_days": 14, "idempotency_key": "plan-key-1",
    })
    order = [item["device_id"] for item in plan["windows"]]

    # 2) 批准：冻结证据版本与资源占用
    approved = service.approve_plan("risk", "plan-summer-1", 1)

    # 批准后证据变化导致新草稿无法通过冻结校验
    plan_b = service.generate_plan("planner", {
        "plan_id": "plan-summer-2", "rule_set_id": "summer-peak-rules",
        "horizon_days": 14, "idempotency_key": "plan-key-2",
    })
    service.record_evidence("planner", _evidence("ev-5b", "pack-5", "v2", "68", "critical", 15, "mandatory"))
    freeze_rejected = None
    try:
        service.approve_plan("risk", "plan-summer-2", 1)
    except InvalidState as exc:
        freeze_rejected = str(exc)

    # 3) 现场回执：开工、重复/乱序去重、完工与复测
    plan_detail = service.plan_detail("auditor", "plan-summer-1")
    w1 = "plan-summer-1:pack-1"
    w4 = "plan-summer-1:pack-4"
    started = service.receive_receipt("tech-1", w1, "started", "rcpt-1", {"note": "到场"})
    started_replay = service.receive_receipt("tech-1", w1, "started", "rcpt-1", {"note": "到场"})
    paused = service.receive_receipt("tech-1", w1, "paused", "rcpt-2", {"reason": "等待高空车"})
    out_of_order = None
    try:
        service.receive_receipt("tech-1", w1, "completed", "rcpt-x", {})
    except InvalidState as exc:
        out_of_order = str(exc)
    service.receive_receipt("tech-1", w1, "resumed", "rcpt-3", {})
    completed = service.receive_receipt("tech-1", w1, "completed", "rcpt-4", {"result": "更换模组"})
    retest = service.receive_receipt("tech-1", w1, "retest", "rcpt-5", {"retention": "92"})
    late_start_rejected = None
    try:
        service.receive_receipt("tech-1", w1, "started", "rcpt-late", {})
    except InvalidState as exc:
        late_start_rejected = str(exc)

    # 4) 证据变化 / 紧急告警只失效受影响的未执行窗口；已开工窗口不动
    service.record_evidence("planner", _evidence("ev-2b", "pack-2", "v2", "78", "critical", 8, "hold"))
    signal_evidence = service.record_signal("planner", "pack-2", "evidence_changed",
                                            {"new_evidence_id": "ev-2b"})
    signal_alarm = service.record_signal("planner", "pack-3", "urgent_alarm",
                                         {"alarm": "thermal_runaway_precursor"})

    # 5) 延期必须记录风险接受人与新的最迟日期
    extension = service.extend_window(
        "planner", w4, "2026-06-20", "备件到货延迟，风险可控", "risk",
    )

    view = service.operations_view("planner")
    audit = service.audit_chain("auditor")
    schema = inspect_schema(connection)
    result = {
        "status": "ok",
        "workspace": workspace.name,
        "plan_id": plan["plan_id"],
        "input_sha256": plan["input_sha256"],
        "window_order": order,
        "window_count": approved["windows"],
        "unscheduled": [
            {"device_id": item["device_id"], "reasons": item["rejected_slots"][-1]["reasons"]}
            for item in plan["unscheduled"]
        ],
        "arbitration_sample": plan_detail["arbitration"][0],
        "daily_load": plan_detail["daily_load"],
        "frozen_evidence_count": len(plan_detail["frozen_evidence"]),
        "freeze_rejected_after_change": freeze_rejected is not None,
        "receipts": {
            "started": started["state_after"],
            "duplicate_replayed_same": started_replay["receipt_id"] == started["receipt_id"],
            "paused": paused["state_after"],
            "out_of_order_completed_rejected": out_of_order is not None,
            "completed": completed["state_after"],
            "retest": retest["state_after"],
            "late_start_after_close_rejected": late_start_rejected is not None,
        },
        "signals": {
            "evidence_changed": signal_evidence["invalidated_windows"],
            "urgent_alarm": signal_alarm["invalidated_windows"],
            "started_window_survives": w1 not in signal_evidence["invalidated_windows"]
            and w1 not in signal_alarm["invalidated_windows"],
        },
        "extension": extension,
        "operations": {
            "pending_actions": view["pending_actions"],
            "overdue": view["overdue_windows"],
            "station_daily_load": view["station_daily_load"],
            "extension_exposure_total_kwh_days": view["extension_exposure_total_kwh_days"],
        },
        "audit": audit,
        "schema": schema,
    }
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行储能电池检修计划服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
