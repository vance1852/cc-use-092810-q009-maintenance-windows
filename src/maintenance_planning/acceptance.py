"""贯通风险待办、约束排程、批准冻结、失效隔离、回执乱序、延期与运营看板的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import MaintenanceService


RULEBOOK = {
    "rulebook_id": "summer-peak",
    "version": 1,
    "degradation_weight": "0.4",
    "alert_weight": "0.3",
    "recall_weight": "0.2",
    "cycle_weight": "0.1",
    "degradation_threshold_percent": "20",
    "high_risk_score": 70,
    "horizon_days": 30,
}

ACTIVITY = {
    "activity_id": "act-pack-replace",
    "title": "电池包更换与隔离复检",
    "required_certifications": ["hv", "bms"],
    "estimated_hours": 6,
    "required_spare_kinds": {"pack-280ah": 1},
    "isolation_required": True,
    "minimum_crew": 2,
}


def evidence(battery: str, facility: str, revision: str, usable: str, alerts=None,
             recall="none", blocked=None, cycles=2000, rated="1000") -> dict:
    return {
        "battery_id": battery,
        "facility_id": facility,
        "evidence_revision": revision,
        "observed_at": "2026-06-01T00:00:00Z",
        "rated_capacity_kwh": rated,
        "usable_capacity_kwh": usable,
        "cycle_count": cycles,
        "alerts": alerts or [],
        "recall_level": recall,
        **({"blocked_recall": blocked} if blocked is not None else {}),
        **({"recall_reference": f"recall-{battery}"} if recall != "none" else {}),
    }


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    service = MaintenanceService(connection, FrozenClock(datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc)))
    for user_id, role in (
        ("plan", "planner"), ("boss", "approver"), ("risk", "risk"),
        ("tech", "technician"), ("audit", "auditor"),
    ):
        service.create_user(user_id, user_id, role)

    # 规则、活动、班组、备件与场站可停机额度。
    service.publish_rulebook("plan", RULEBOOK)
    service.register_activity("plan", ACTIVITY)
    service.register_crew("plan", "crew-north", "北班", 2, ["hv", "bms"])
    service.register_crew("plan", "crew-south", "南班", 2, ["hv", "bms"])
    service.upsert_spare_stock("plan", "pack-280ah", 3)
    for day in ("2026-06-08", "2026-06-09", "2026-06-10"):
        service.set_facility_quota("plan", "station-a", day, 2500)

    # 三套设备：高风险（严重告警）、中风险（容量衰减）、被召回禁排。
    service.record_evidence("plan", evidence("bat-001", "station-a", "ev-1", "780",
                                             alerts=[{"alert_id": "a1", "severity": "critical",
                                                      "code": "thermal", "observed_at": "2026-05-30T08:00:00Z"}]))
    service.record_evidence("plan", evidence("bat-002", "station-a", "ev-1", "830", cycles=5200))
    service.record_evidence("plan", evidence("bat-003", "station-a", "ev-1", "950",
                                             recall="mandatory", blocked=True))
    for battery in ("bat-001", "bat-002", "bat-003"):
        service.assign_activity("plan", battery, "act-pack-replace")

    backlog = service.risk_backlog("plan", "summer-peak", 1)
    draft = service.generate_plan("plan", {
        "plan_id": "plan-summer-1", "title": "迎峰度夏前检修",
        "rulebook_id": "summer-peak", "rulebook_version": 1,
        "horizon_start": "2026-06-08", "horizon_end": "2026-06-10",
    })
    approved = service.approve_plan("boss", "plan-summer-1", 1)

    approved_windows = connection.execute(
        "SELECT battery_id,window_id,state,service_date FROM maintenance_windows ORDER BY window_id"
    ).fetchall()
    id_001 = next(r["window_id"] for r in approved_windows if r["battery_id"] == "bat-001")
    id_002 = next(r["window_id"] for r in approved_windows if r["battery_id"] == "bat-002")

    # 召回升级只使受影响电池（bat-002）的未执行窗口失效，bat-001 不受影响。
    service.record_health_event("risk", {
        "battery_id": "bat-002", "kind": "recall", "severity": "restricted",
        "reference": "recall-2026-07", "observed_at": "2026-06-02T09:00:00Z",
    })
    invalidated_002 = connection.execute(
        "SELECT state FROM maintenance_windows WHERE window_id=?", (id_002,)
    ).fetchone()["state"]

    # 延期：计划员申请，风险负责人接受暴露容量与新最迟日期，窗口日期随之移动。
    postponement = service.request_postponement("plan", id_001, "2026-06-10", "2026-06-12", "备件到货晚两天")
    accepted = service.resolve_postponement("risk", postponement["postponement_id"], True, "迎峰前可接受该容量敞口")
    moved_date = connection.execute(
        "SELECT service_date FROM maintenance_windows WHERE window_id=?", (id_001,)
    ).fetchone()["service_date"]

    # 现场回执乱序与重复：对已失效窗口的迟到完工被拒绝；重复开始不二次生效；关闭后不能复活。
    service.record_receipt("tech", id_001, "start", "key-start-1")
    duplicate_start = service.record_receipt("tech", id_001, "start", "key-start-1")
    late_complete = service.record_receipt("tech", id_002, "complete", "key-complete-late")
    service.record_receipt("tech", id_001, "pause", "key-pause-1")
    service.record_receipt("tech", id_001, "resume", "key-resume-1")
    service.record_receipt("tech", id_001, "complete", "key-complete-1")
    service.record_receipt("tech", id_001, "retest", "key-retest-1", {"passed": True})
    closed_001 = connection.execute(
        "SELECT state FROM maintenance_windows WHERE window_id=?", (id_001,)
    ).fetchone()["state"]
    zombie_start = service.record_receipt("tech", id_001, "start", "key-start-zombie")

    dashboard = service.operations_dashboard("plan", "plan-summer-1")
    window_001_detail = service.window_detail("audit", id_001)

    result = {
        "status": "ok",
        "backlog_order": [(row["battery_id"], row["risk_score"], row["risk_band"]) for row in backlog["backlog"]],
        "draft_windows": [(w["battery_id"], w["service_date"], w["crew_id"]) for w in draft["windows"]],
        "unscheduled": [(u["battery_id"], u["reasons"]) for u in draft["unscheduled"]],
        "arbitrations": len(draft["arbitrations"]),
        "approved": approved,
        "recall_invalidates_only_affected_window": invalidated_002 == "invalidated",
        "postponement": {"request": postponement, "accepted": accepted, "moved_date": moved_date},
        "duplicate_start_is_idempotent": duplicate_start["duplicate"] is True,
        "late_complete_rejected": late_complete["accepted"] is False,
        "closed_state": closed_001,
        "retest_passed": closed_001 == "retest_passed",
        "closed_window_not_revived": zombie_start["accepted"] is False,
        "window_001_reasons": window_001_detail["selection_reasons"],
        "window_counts": dashboard["window_counts"],
        "pending_actions": [a["action"] for a in dashboard["pending_actions"]],
        "postponed_capacity_risk": dashboard["postponed_capacity_risk"],
        "unscheduled_backlog": [(u["battery_id"], u["reasons"]) for u in dashboard["unscheduled_backlog"]],
        "audit": service.audit_chain("audit"),
        "workspace": workspace.name,
    }
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行维修计划服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
