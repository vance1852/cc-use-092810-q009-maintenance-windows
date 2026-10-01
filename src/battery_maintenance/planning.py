"""确定性的健康风险评分与维修窗口排程。

评分与排程不访问数据库、不读取当前时间以外的外部状态：同样的证据版本、规则版本、
资源快照必须产生同样的窗口安排，便于批准时冻结输入并在事后解释“为何这样安排”。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Mapping, Sequence

from .models import Device, HealthEvidence, RiskRuleSet


ZERO = Decimal("0")
HUNDRED = Decimal("100")
DAY = timedelta(days=1)


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def iso_date(value: date) -> str:
    return value.isoformat()


@dataclass(frozen=True, slots=True)
class RiskScore:
    device_id: str
    evidence_id: str
    level: str
    points: int
    factors: Mapping[str, object]
    latest_date: str

    def as_dict(self) -> dict[str, object]:
        return {
            "device_id": self.device_id,
            "evidence_id": self.evidence_id,
            "level": self.level,
            "points": self.points,
            "factors": dict(self.factors),
            "latest_date": self.latest_date,
        }


def _level_for_retention(retention: Decimal, rules: RiskRuleSet) -> str | None:
    """容量保持率触发的风险级别；保持率越低风险越高。"""

    if retention < rules.retention_thresholds["critical"]:
        return "critical"
    if retention < rules.retention_thresholds["high"]:
        return "high"
    return None


def score_evidence(
    evidence: HealthEvidence,
    rules: RiskRuleSet,
    *,
    anchor_date: date,
) -> RiskScore:
    """把一份确定版本的健康证据按确定版本的规则评分为待办优先级。"""

    retention_level = _level_for_retention(evidence.capacity_retention_percent, rules)
    retention_penalty = 0
    if retention_level == "critical":
        retention_penalty = 60
    elif retention_level == "high":
        retention_penalty = 35
    severity_penalty = rules.severity_weights[evidence.alarm_severity]
    recall_penalty = rules.recall_scores.get(evidence.recall_level, 0)
    alarm_count_penalty = rules.alarm_count_weight * evidence.alarm_count_30d
    cycle_penalty = rules.cycle_weight * evidence.cycle_count
    points = retention_penalty + severity_penalty + recall_penalty + alarm_count_penalty + cycle_penalty

    if points >= 80 or evidence.recall_level == "mandatory" or retention_level == "critical":
        level = "critical"
    elif points >= 50 or evidence.recall_level == "hold" or retention_level == "high":
        level = "high"
    elif points >= 20 or evidence.recall_level == "monitor":
        level = "medium"
    else:
        level = "low"

    latest = anchor_date + timedelta(days=rules.deadline_days[level])
    factors = {
        "capacity_retention_percent": decimal_text(evidence.capacity_retention_percent),
        "retention_level": retention_level,
        "retention_penalty": retention_penalty,
        "alarm_severity": evidence.alarm_severity,
        "alarm_severity_penalty": severity_penalty,
        "alarm_count_30d": evidence.alarm_count_30d,
        "alarm_count_penalty": alarm_count_penalty,
        "recall_level": evidence.recall_level,
        "recall_penalty": recall_penalty,
        "cycle_count": evidence.cycle_count,
        "cycle_penalty": cycle_penalty,
        "total_points": points,
        "rule_deadline_days": rules.deadline_days[level],
    }
    return RiskScore(
        device_id=evidence.device_id,
        evidence_id=evidence.evidence_id,
        level=level,
        points=points,
        factors=factors,
        latest_date=iso_date(latest),
    )


@dataclass(frozen=True, slots=True)
class ResourceSnapshot:
    """排程时刻的资源可用性快照，批准计划时整体冻结。

    station_quota_by_date 已扣除其他已批准窗口占用的当日停机额度；
    unavailable 包含人员请假与已批准窗口造成的整日占用；
    busy_bays 是已被其他窗口占用的（隔离位, 日期）；
    spare_availability 已扣除 held 状态的备件预留。
    """

    station_quota_by_date: Mapping[str, Mapping[str, Decimal]]
    technicians: Sequence[Mapping[str, object]]
    unavailable: Mapping[str, Sequence[tuple[str, str]]]
    bays: Sequence[Mapping[str, object]]
    busy_bays: frozenset[tuple[str, str]]
    spare_availability: Mapping[tuple[str, str], int]


LEVEL_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


@dataclass(slots=True)
class _DayState:
    used_kwh: dict[str, Decimal] = field(default_factory=dict)
    technician_days: set[tuple[str, str]] = field(default_factory=set)
    bay_days: set[tuple[str, str]] = field(default_factory=set)
    spare_used: dict[tuple[str, str, str], int] = field(default_factory=dict)


def _technician_available(technician_id: str, day: str, unavailable: Mapping[str, Sequence[tuple[str, str]]]) -> bool:
    for starts_at, ends_at in unavailable.get(technician_id, ()):  # type: ignore[union-attr]
        if starts_at <= f"{day}T23:59:59Z" and ends_at > f"{day}T00:00:00Z":
            return False
    return True


def schedule_windows(
    *,
    scores: Sequence[RiskScore],
    devices: Mapping[str, Device],
    rules: RiskRuleSet,
    resources: ResourceSnapshot,
    anchor_date: date,
    horizon_days: int,
) -> dict[str, object]:
    """按风险优先级在容量、人员、备件与隔离约束内逐日排程。

    冲突裁决顺序固定为：风险级别、评分、最迟日期、设备编号（全部升序/降序确定）。
    排不进 horizon 的设备进入 unscheduled，不静默丢弃。
    """

    horizon_end = anchor_date + timedelta(days=horizon_days)
    ordered = sorted(
        scores,
        key=lambda item: (LEVEL_ORDER[item.level], -item.points, item.latest_date, item.device_id),
    )
    technicians = sorted(resources.technicians, key=lambda item: str(item["technician_id"]))
    bays = sorted(resources.bays, key=lambda item: str(item["bay_id"]))

    days: list[str] = []
    cursor = anchor_date
    while cursor < horizon_end:
        days.append(iso_date(cursor))
        cursor += DAY

    state = _DayState()
    windows: list[dict[str, object]] = []
    unscheduled: list[dict[str, object]] = []
    decisions: list[dict[str, object]] = []

    def spare_key(station_id: str, sku: str) -> tuple[str, str]:
        return (station_id, sku)

    for score in ordered:
        device = devices[score.device_id]
        capacity = device.rated_capacity_kwh
        station_quota = resources.station_quota_by_date.get(device.station_id, {})
        candidate_days = [day for day in days if day <= score.latest_date]
        placed = False
        rejections: list[dict[str, object]] = []
        if not candidate_days:
            rejections.append({"service_date": None, "reasons": ["latest_date_before_horizon"]})
        for day in candidate_days:
            day_rejections: list[str] = []
            load_key = f"{device.station_id}|{day}"
            quota = station_quota.get(day, ZERO)
            used = state.used_kwh.get(load_key, ZERO)
            if used + capacity > quota:
                day_rejections.append("station_outage_quota")
            chosen_technician = None
            for technician in technicians:
                technician_id = str(technician["technician_id"])
                qualifications = technician["qualifications"]  # type: ignore[index]
                if device.required_qualification not in qualifications:  # type: ignore[operator]
                    continue
                if (technician_id, day) in state.technician_days:
                    continue
                if not _technician_available(technician_id, day, resources.unavailable):
                    continue
                chosen_technician = technician_id
                break
            if chosen_technician is None:
                day_rejections.append("qualified_technician")
            chosen_bay = None
            if device.required_isolation:
                for bay in bays:
                    bay_id = str(bay["bay_id"])
                    if bay["station_id"] != device.station_id:  # type: ignore[index]
                        continue
                    if not device.required_isolation <= bay["isolation_kinds"]:  # type: ignore[operator]
                        continue
                    if (bay_id, day) in state.bay_days or (bay_id, day) in resources.busy_bays:
                        continue
                    chosen_bay = bay_id
                    break
                if chosen_bay is None:
                    day_rejections.append("isolation_bay")
            spare_short: list[str] = []
            for sku in sorted(device.required_spare_skus):
                available = resources.spare_availability.get(spare_key(device.station_id, sku), 0)
                consumed = state.spare_used.get((device.station_id, sku, day), 0)
                if consumed + 1 > available:
                    spare_short.append(sku)
            if spare_short:
                day_rejections.append("spare_part:" + ",".join(spare_short))
            if day_rejections:
                rejections.append({"service_date": day, "reasons": day_rejections})
                continue
            state.used_kwh[load_key] = used + capacity
            assert chosen_technician is not None
            state.technician_days.add((chosen_technician, day))
            if chosen_bay is not None:
                state.bay_days.add((chosen_bay, day))
            for sku in device.required_spare_skus:
                state.spare_used[(device.station_id, sku, day)] = (
                    state.spare_used.get((device.station_id, sku, day), 0) + 1
                )
            windows.append({
                "device_id": device.device_id,
                "station_id": device.station_id,
                "service_date": day,
                "risk_level": score.level,
                "risk_points": score.points,
                "evidence_id": score.evidence_id,
                "latest_date": score.latest_date,
                "rated_capacity_kwh": decimal_text(capacity),
                "technician_id": chosen_technician,
                "bay_id": chosen_bay,
                "required_spare_skus": sorted(device.required_spare_skus),
                "required_isolation": sorted(device.required_isolation),
                "rationale": score.as_dict(),
            })
            decisions.append({
                "device_id": device.device_id,
                "service_date": day,
                "rank": (LEVEL_ORDER[score.level], -score.points, score.latest_date),
                "considered": rejections,
            })
            placed = True
            break
        if not placed:
            unscheduled.append({
                "device_id": device.device_id,
                "station_id": device.station_id,
                "risk_level": score.level,
                "risk_points": score.points,
                "evidence_id": score.evidence_id,
                "latest_date": score.latest_date,
                "rejected_slots": rejections,
            })

    windows.sort(key=lambda item: (str(item["service_date"]), LEVEL_ORDER[str(item["risk_level"])], str(item["device_id"])))
    daily_load: dict[str, dict[str, object]] = {}
    for key, value in state.used_kwh.items():
        station_id, day = key.split("|", 1)
        bucket = daily_load.setdefault(day, {"service_date": day, "stations": {}})
        bucket["stations"][station_id] = {  # type: ignore[index]
            "outage_kwh": decimal_text(value.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)),
            "quota_kwh": decimal_text(
                resources.station_quota_by_date.get(station_id, {}).get(day, ZERO)
            ),
        }
    return {
        "anchor_date": iso_date(anchor_date),
        "horizon_days": horizon_days,
        "windows": windows,
        "unscheduled": unscheduled,
        "arbitration": [
            {
                "device_id": item["device_id"],
                "service_date": item["service_date"],
                "priority_rank": list(item["rank"]),
                "rejected_alternatives": item["considered"],
            }
            for item in decisions
        ],
        "daily_load": [daily_load[day] for day in sorted(daily_load)],
    }
