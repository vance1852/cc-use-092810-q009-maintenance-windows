"""维修计划领域输入契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .clock import parse_utc
from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")

DEVICE_KINDS = {"pack", "rack", "cluster", "bms", "pcs", "auxiliary"}
ALARM_SEVERITIES = {"info", "warning", "critical"}
RECALL_LEVELS = {"none", "monitor", "hold", "mandatory"}
RISK_LEVELS = {"low", "medium", "high", "critical"}
WINDOW_STATES = {"proposed", "approved", "invalidated", "in_progress", "paused", "completed", "failed", "cancelled"}
TERMINAL_STATES = frozenset({"completed", "failed", "cancelled"})
RECEIPT_EVENTS = {"started", "paused", "resumed", "completed", "retest"}
RETURNABLE_EVENTS = frozenset({"started", "paused", "resumed", "retest"})
ISOLATION_KINDS = {"electrical", "thermal", "fire", "communicational"}


def required_text(value: object, field_name: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field_name} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field_name} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, field_name: str) -> str:
    result = required_text(value, field_name, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{field_name} 格式不正确")
    return result


def optional_identifier(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return identifier(value, field_name)


def decimal_value(
    value: object,
    field_name: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
    if isinstance(value, bool):
        raise ValidationFailed(f"{field_name} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationFailed(f"{field_name} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValidationFailed(f"{field_name} 必须是有限数值")
    if minimum is not None and result < minimum:
        raise ValidationFailed(f"{field_name} 不能小于 {minimum}")
    if maximum is not None and result > maximum:
        raise ValidationFailed(f"{field_name} 不能大于 {maximum}")
    return result


def positive_integer(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationFailed(f"{field_name} 必须是正整数")
    return value


def timestamp(value: object, field_name: str) -> str:
    text = required_text(value, field_name, 40)
    try:
        return parse_utc(text, field_name).isoformat().replace("+00:00", "Z")
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc


def choice(value: object, field_name: str, choices: set[str]) -> str:
    result = required_text(value, field_name, 24)
    if result not in choices:
        raise ValidationFailed(f"{field_name} 必须是 {sorted(choices)} 之一")
    return result


def identifier_set(value: object, field_name: str) -> frozenset[str]:
    if not isinstance(value, (list, tuple, set)):
        raise ValidationFailed(f"{field_name} 必须是字符串数组")
    result: set[str] = set()
    for item in value:
        result.add(identifier(item, f"{field_name}[]"))
    return frozenset(result)


@dataclass(frozen=True, slots=True)
class Station:
    station_id: str
    name: str
    timezone: str
    daily_outage_kwh: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Station":
        timezone_name = required_text(raw.get("timezone"), "timezone", 64)
        if "/" not in timezone_name and timezone_name != "UTC":
            raise ValidationFailed("timezone 必须是 IANA 时区或 UTC")
        return cls(
            station_id=identifier(raw.get("station_id"), "station_id"),
            name=required_text(raw.get("name"), "name"),
            timezone=timezone_name,
            daily_outage_kwh=decimal_value(
                raw.get("daily_outage_kwh"), "daily_outage_kwh", minimum=Decimal("0")
            ),
        )


@dataclass(frozen=True, slots=True)
class Device:
    device_id: str
    station_id: str
    device_kind: str
    model_name: str
    rated_capacity_kwh: Decimal
    required_qualification: str
    required_isolation: frozenset[str]
    required_spare_skus: frozenset[str]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Device":
        kind = required_text(raw.get("device_kind"), "device_kind", 24)
        if kind not in DEVICE_KINDS:
            raise ValidationFailed(f"device_kind 必须是 {sorted(DEVICE_KINDS)} 之一")
        isolation = raw.get("required_isolation", [])
        for item in isolation:
            choice(item, "required_isolation[]", ISOLATION_KINDS)
        return cls(
            device_id=identifier(raw.get("device_id"), "device_id"),
            station_id=identifier(raw.get("station_id"), "station_id"),
            device_kind=kind,
            model_name=required_text(raw.get("model_name"), "model_name"),
            rated_capacity_kwh=decimal_value(
                raw.get("rated_capacity_kwh"), "rated_capacity_kwh", minimum=Decimal("0.001")
            ),
            required_qualification=identifier(
                raw.get("required_qualification"), "required_qualification"
            ),
            required_isolation=identifier_set(isolation, "required_isolation")
            if isolation else frozenset(),
            required_spare_skus=identifier_set(
                raw.get("required_spare_skus", []), "required_spare_skus"
            ),
        )


@dataclass(frozen=True, slots=True)
class HealthEvidence:
    """确定版本的健康证据：同一证据版本下容量衰减、告警与召回限制不可变。"""

    evidence_id: str
    device_id: str
    version: str
    observed_at: str
    capacity_retention_percent: Decimal
    cycle_count: int
    alarm_severity: str
    alarm_count_30d: int
    recall_level: str
    recall_code: str | None
    note: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "HealthEvidence":
        device_id = identifier(raw.get("device_id"), "device_id")
        return cls(
            evidence_id=identifier(raw.get("evidence_id"), "evidence_id"),
            device_id=device_id,
            version=required_text(raw.get("version"), "version", 32),
            observed_at=timestamp(raw.get("observed_at"), "observed_at"),
            capacity_retention_percent=decimal_value(
                raw.get("capacity_retention_percent"),
                "capacity_retention_percent",
                minimum=Decimal("0"),
                maximum=Decimal("100"),
            ),
            cycle_count=positive_integer(raw.get("cycle_count"), "cycle_count"),
            alarm_severity=choice(raw.get("alarm_severity", "info"), "alarm_severity", ALARM_SEVERITIES),
            alarm_count_30d=_non_negative(raw.get("alarm_count_30d", 0), "alarm_count_30d"),
            recall_level=choice(raw.get("recall_level", "none"), "recall_level", RECALL_LEVELS),
            recall_code=optional_identifier(raw.get("recall_code"), "recall_code"),
            note=required_text(raw.get("note", ""), "note", 512) if raw.get("note") else "",
        )


def _non_negative(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationFailed(f"{field_name} 必须是非负整数")
    return value


@dataclass(frozen=True, slots=True)
class RiskRuleSet:
    """可版本化的风险评分规则；生成计划时冻结其版本与摘要。"""

    rule_set_id: str
    version: str
    retention_thresholds: Mapping[str, Decimal]
    severity_weights: Mapping[str, int]
    recall_scores: Mapping[str, int]
    alarm_count_weight: int
    cycle_weight: int
    deadline_days: Mapping[str, int]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RiskRuleSet":
        rule_set_id = identifier(raw.get("rule_set_id"), "rule_set_id")
        version = required_text(raw.get("version"), "version", 32)
        thresholds_raw = raw.get("retention_thresholds", {})
        if not isinstance(thresholds_raw, Mapping):
            raise ValidationFailed("retention_thresholds 必须是对象")
        thresholds: dict[str, Decimal] = {}
        for level in ("high", "critical"):
            thresholds[level] = decimal_value(
                thresholds_raw.get(level), f"retention_thresholds.{level}",
                minimum=Decimal("0"), maximum=Decimal("100"),
            )
        if thresholds["high"] <= thresholds["critical"]:
            raise ValidationFailed("retention_thresholds.high 必须大于 critical")
        severity_weights = _weight_map(raw.get("severity_weights", {}), "severity_weights", ALARM_SEVERITIES)
        recall_scores = _weight_map(raw.get("recall_scores", {}), "recall_scores", RECALL_LEVELS - {"none"})
        deadline_days_raw = raw.get("deadline_days", {})
        if not isinstance(deadline_days_raw, Mapping):
            raise ValidationFailed("deadline_days 必须是对象")
        deadline_days: dict[str, int] = {}
        for level in RISK_LEVELS:
            value = deadline_days_raw.get(level)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValidationFailed(f"deadline_days.{level} 必须是正整数")
            deadline_days[level] = value
        return cls(
            rule_set_id=rule_set_id,
            version=version,
            retention_thresholds=thresholds,
            severity_weights=severity_weights,
            recall_scores=recall_scores,
            alarm_count_weight=_non_negative(raw.get("alarm_count_weight", 0), "alarm_count_weight"),
            cycle_weight=_non_negative(raw.get("cycle_weight", 0), "cycle_weight"),
            deadline_days=deadline_days,
        )


def _weight_map(raw: object, field_name: str, keys: set[str]) -> dict[str, int]:
    if not isinstance(raw, Mapping):
        raise ValidationFailed(f"{field_name} 必须是对象")
    result: dict[str, int] = {}
    for key in keys:
        value = raw.get(key, 0)
        result[key] = _non_negative(value, f"{field_name}.{key}")
    return result


@dataclass(frozen=True, slots=True)
class Technician:
    technician_id: str
    display_name: str
    qualifications: frozenset[str]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Technician":
        return cls(
            technician_id=identifier(raw.get("technician_id"), "technician_id"),
            display_name=required_text(raw.get("display_name"), "display_name"),
            qualifications=identifier_set(raw.get("qualifications", []), "qualifications"),
        )


@dataclass(frozen=True, slots=True)
class IsolationBay:
    bay_id: str
    station_id: str
    isolation_kinds: frozenset[str]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "IsolationBay":
        kinds = identifier_set(raw.get("isolation_kinds", []), "isolation_kinds")
        for item in kinds:
            if item not in ISOLATION_KINDS:
                raise ValidationFailed(f"isolation_kinds 取值必须是 {sorted(ISOLATION_KINDS)} 之一")
        return cls(
            bay_id=identifier(raw.get("bay_id"), "bay_id"),
            station_id=identifier(raw.get("station_id"), "station_id"),
            isolation_kinds=kinds,
        )


@dataclass(frozen=True, slots=True)
class SparePart:
    sku: str
    station_id: str
    quantity_on_hand: int
    reserved: int = 0

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SparePart":
        return cls(
            sku=identifier(raw.get("sku"), "sku"),
            station_id=identifier(raw.get("station_id"), "station_id"),
            quantity_on_hand=_non_negative(raw.get("quantity_on_hand"), "quantity_on_hand"),
        )


@dataclass(frozen=True, slots=True)
class UnavailabilityWindow:
    """人员不可用时间段（半开区间 [starts_at, ends_at)）。"""

    starts_at: str
    ends_at: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "UnavailabilityWindow":
        starts_at = timestamp(raw.get("starts_at"), "starts_at")
        ends_at = timestamp(raw.get("ends_at"), "ends_at")
        if ends_at <= starts_at:
            raise ValidationFailed("ends_at 必须晚于 starts_at")
        return cls(starts_at=starts_at, ends_at=ends_at)


@dataclass(frozen=True, slots=True)
class PlanRequest:
    plan_id: str
    rule_set_id: str
    horizon_days: int
    station_ids: frozenset[str] = field(default_factory=frozenset)
    idempotency_key: str = ""

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "PlanRequest":
        horizon = positive_integer(raw.get("horizon_days", 14), "horizon_days")
        if horizon > 366:
            raise ValidationFailed("horizon_days 不能超过 366")
        stations_raw = raw.get("station_ids", [])
        stations = identifier_set(stations_raw, "station_ids") if stations_raw else frozenset()
        return cls(
            plan_id=identifier(raw.get("plan_id"), "plan_id"),
            rule_set_id=identifier(raw.get("rule_set_id"), "rule_set_id"),
            horizon_days=horizon,
            station_ids=stations,
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )
