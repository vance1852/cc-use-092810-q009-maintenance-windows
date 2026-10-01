"""维修计划领域的严格输入契约：健康证据、风险规则版本与资源目录。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
ALERT_SEVERITIES = {"info": 10, "warning": 30, "critical": 60}
RECALL_LEVELS = {"none": 0, "advisory": 25, "restricted": 55, "mandatory": 100}
RECORD_TYPES = {"evidence", "alert", "recall"}
TASK_STATUSES = {"pending", "scheduled", "in_progress", "paused", "completed", "failed", "cancelled"}


def required_text(value: object, field_name: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValueError(f"{field_name} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, field_name: str) -> str:
    result = required_text(value, field_name, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValueError(f"{field_name} 格式不正确")
    return result


def decimal_value(
    value: object,
    field_name: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{field_name} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValueError(f"{field_name} 必须是有限数值")
    if minimum is not None and result < minimum:
        raise ValueError(f"{field_name} 不能小于 {minimum}")
    if maximum is not None and result > maximum:
        raise ValueError(f"{field_name} 不能大于 {maximum}")
    return result


def integer_value(value: object, field_name: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} 必须是整数")
    if not minimum <= value <= maximum:
        raise ValueError(f"{field_name} 必须在 {minimum} 到 {maximum} 之间")
    return value


def _mapping(value: object, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} 必须是对象")
    return value


def _sequence(value: object, field_name: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError(f"{field_name} 必须是数组")
    return value


@dataclass(frozen=True, slots=True)
class HealthEvidence:
    """某套储能电池在某一确定版本上的健康证据快照。"""

    battery_id: str
    facility_id: str
    evidence_revision: str
    observed_at: str
    rated_capacity_kwh: Decimal
    usable_capacity_kwh: Decimal
    cycle_count: int
    alerts: tuple[Mapping[str, Any], ...]
    recall_level: str
    recall_reference: str | None
    blocked_recall: bool

    @property
    def degradation_percent(self) -> Decimal:
        if self.rated_capacity_kwh == 0:
            return Decimal(0)
        return (Decimal(1) - self.usable_capacity_kwh / self.rated_capacity_kwh) * Decimal(100)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "HealthEvidence":
        data = _mapping(raw, "evidence")
        alerts = tuple(
            _mapping(item, "evidence.alerts[]") for item in _sequence(data.get("alerts", ()), "evidence.alerts")
        )
        normalized_alerts: list[Mapping[str, Any]] = []
        for item in alerts:
            severity = required_text(item.get("severity"), "alert.severity", 16).lower()
            if severity not in ALERT_SEVERITIES:
                raise ValueError("alert.severity 必须是 info、warning 或 critical")
            normalized_alerts.append({
                "alert_id": identifier(item.get("alert_id"), "alert.alert_id"),
                "severity": severity,
                "code": required_text(item.get("code"), "alert.code", 64),
                "observed_at": required_text(item.get("observed_at"), "alert.observed_at", 40),
            })
        recall_level = required_text(data.get("recall_level", "none"), "evidence.recall_level", 16).lower()
        if recall_level not in RECALL_LEVELS:
            raise ValueError("evidence.recall_level 必须是 none、advisory、restricted 或 mandatory")
        rated = decimal_value(data.get("rated_capacity_kwh"), "evidence.rated_capacity_kwh", minimum=Decimal("0.001"))
        usable = decimal_value(data.get("usable_capacity_kwh"), "evidence.usable_capacity_kwh", minimum=Decimal("0"))
        if usable > rated:
            raise ValueError("evidence.usable_capacity_kwh 不能大于额定容量")
        cycle_count = integer_value(data.get("cycle_count", 0), "evidence.cycle_count", minimum=0, maximum=1_000_000)
        return cls(
            battery_id=identifier(data.get("battery_id"), "evidence.battery_id"),
            facility_id=identifier(data.get("facility_id"), "evidence.facility_id"),
            evidence_revision=identifier(data.get("evidence_revision"), "evidence.evidence_revision"),
            observed_at=required_text(data.get("observed_at"), "evidence.observed_at", 40),
            rated_capacity_kwh=rated,
            usable_capacity_kwh=usable,
            cycle_count=cycle_count,
            alerts=tuple(normalized_alerts),
            recall_level=recall_level,
            recall_reference=(
                required_text(data.get("recall_reference"), "evidence.recall_reference", 96)
                if recall_level != "none"
                else None
            ),
            blocked_recall=bool(data.get("blocked_recall", recall_level == "mandatory")),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "battery_id": self.battery_id,
            "facility_id": self.facility_id,
            "evidence_revision": self.evidence_revision,
            "observed_at": self.observed_at,
            "rated_capacity_kwh": format(self.rated_capacity_kwh, "f"),
            "usable_capacity_kwh": format(self.usable_capacity_kwh, "f"),
            "degradation_percent": format(self.degradation_percent, "f"),
            "cycle_count": self.cycle_count,
            "alerts": list(self.alerts),
            "recall_level": self.recall_level,
            "recall_reference": self.recall_reference,
            "blocked_recall": self.blocked_recall,
        }


@dataclass(frozen=True, slots=True)
class RiskRulebook:
    """风险评分规则的确定版本。"""

    rulebook_id: str
    version: int
    degradation_weight: Decimal
    alert_weight: Decimal
    recall_weight: Decimal
    cycle_weight: Decimal
    degradation_threshold_percent: Decimal
    high_risk_score: int
    horizon_days: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RiskRulebook":
        data = _mapping(raw, "rulebook")
        weights = {
            key: decimal_value(data.get(key, default), f"rulebook.{key}", minimum=Decimal("0"), maximum=Decimal("1"))
            for key, default in (
                ("degradation_weight", "0.4"),
                ("alert_weight", "0.3"),
                ("recall_weight", "0.2"),
                ("cycle_weight", "0.1"),
            )
        }
        total = sum(weights.values(), Decimal(0))
        if total != Decimal(1):
            raise ValueError("规则权重之和必须为 1")
        horizon_days = integer_value(data.get("horizon_days", 30), "rulebook.horizon_days", minimum=1, maximum=366)
        return cls(
            rulebook_id=identifier(data.get("rulebook_id"), "rulebook.rulebook_id"),
            version=integer_value(data.get("version"), "rulebook.version", minimum=1, maximum=1_000_000),
            **weights,
            degradation_threshold_percent=decimal_value(
                data.get("degradation_threshold_percent", "20"),
                "rulebook.degradation_threshold_percent",
                minimum=Decimal("0.1"),
                maximum=Decimal("100"),
            ),
            high_risk_score=integer_value(data.get("high_risk_score", 70), "rulebook.high_risk_score", minimum=1, maximum=100),
            horizon_days=horizon_days,
        )


@dataclass(frozen=True, slots=True)
class MaintenanceCatalog:
    """维修活动目录：所需资质、工时、备件与隔离要求。"""

    activity_id: str
    title: str
    required_certifications: frozenset[str]
    estimated_hours: Decimal
    required_spare_kinds: Mapping[str, int]
    isolation_required: bool
    minimum_crew: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "MaintenanceCatalog":
        data = _mapping(raw, "activity")
        certs = _sequence(data.get("required_certifications", ()), "activity.required_certifications")
        certifications = frozenset(
            required_text(item, "activity.required_certifications[]", 48) for item in certs
        )
        spares_raw = _mapping(data.get("required_spare_kinds", {}), "activity.required_spare_kinds")
        spares = {
            identifier(key, "spare kind"): integer_value(value, f"activity.required_spare_kinds.{key}", minimum=1, maximum=10000)
            for key, value in spares_raw.items()
        }
        return cls(
            activity_id=identifier(data.get("activity_id"), "activity.activity_id"),
            title=required_text(data.get("title"), "activity.title"),
            required_certifications=certifications,
            estimated_hours=decimal_value(
                data.get("estimated_hours", 4), "activity.estimated_hours", minimum=Decimal("0.25"), maximum=Decimal("240")
            ),
            required_spare_kinds=spares,
            isolation_required=bool(data.get("isolation_required", True)),
            minimum_crew=integer_value(data.get("minimum_crew", 1), "activity.minimum_crew", minimum=1, maximum=20),
        )
