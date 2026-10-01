"""基于确定版本证据与规则的风险评分（纯函数，可复算）。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Mapping, Sequence

from .contracts import ALERT_SEVERITIES, RECALL_LEVELS, HealthEvidence, RiskRulebook


ZERO = Decimal("0")
HUNDRED = Decimal("100")
SCALE = Decimal("100")  # 各分项归一化到 0-100
REFERENCE_CYCLES = Decimal("6000")  # 循环寿命分项的满刻度


def _bounded(value: Decimal) -> Decimal:
    return max(ZERO, min(HUNDRED, value))


def degradation_component(evidence: HealthEvidence) -> Decimal:
    """容量衰减相对规则阈值线性归一化，阈值即满分。"""
    threshold = HUNDRED  # 由调用方按规则覆盖
    return _bounded(evidence.degradation_percent)


def score_evidence(
    evidence: HealthEvidence,
    rulebook: RiskRulebook,
    *,
    cycle_full_scale: Decimal = REFERENCE_CYCLES,
) -> dict[str, Any]:
    """返回 0-100 风险分、等级与可解释分项。"""
    degradation = _bounded(
        evidence.degradation_percent / rulebook.degradation_threshold_percent * HUNDRED
    )
    if evidence.alerts:
        alert_raw = max(ALERT_SEVERITIES[item["severity"]] for item in evidence.alerts)
        # 多条告警按条数轻微叠加，封顶 100。
        alert_score = Decimal(min(100, alert_raw + 2 * (len(evidence.alerts) - 1)))
    else:
        alert_score = ZERO
    recall_score = Decimal(RECALL_LEVELS[evidence.recall_level])
    cycle_score = _bounded(Decimal(evidence.cycle_count) / cycle_full_scale * HUNDRED)
    weighted = (
        degradation * rulebook.degradation_weight
        + alert_score * rulebook.alert_weight
        + recall_score * rulebook.recall_weight
        + cycle_score * rulebook.cycle_weight
    )
    score = int(weighted.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if evidence.blocked_recall or evidence.recall_level == "mandatory" or score >= rulebook.high_risk_score:
        band = "critical"
    elif score >= rulebook.high_risk_score * 2 // 3:
        band = "high"
    elif score >= rulebook.high_risk_score // 3:
        band = "medium"
    else:
        band = "low"
    drivers: list[str] = []
    if evidence.recall_level == "mandatory" or evidence.blocked_recall:
        drivers.append("recall_block")
    if evidence.recall_level in {"restricted", "advisory"}:
        drivers.append(f"recall:{evidence.recall_level}")
    if any(item["severity"] == "critical" for item in evidence.alerts):
        drivers.append("critical_alert")
    if degradation >= HUNDRED:
        drivers.append("degradation_over_threshold")
    elif degradation >= HUNDRED * Decimal("0.8"):
        drivers.append("degradation_near_threshold")
    if cycle_score >= HUNDRED:
        drivers.append("cycle_end_of_life")
    return {
        "battery_id": evidence.battery_id,
        "facility_id": evidence.facility_id,
        "evidence_revision": evidence.evidence_revision,
        "rulebook_id": rulebook.rulebook_id,
        "rulebook_version": rulebook.version,
        "risk_score": score,
        "risk_band": band,
        "blocked": evidence.blocked_recall,
        "components": {
            "degradation": format(degradation.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP), "f"),
            "alerts": format(alert_score.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP), "f"),
            "recall": format(recall_score.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP), "f"),
            "cycles": format(cycle_score.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP), "f"),
        },
        "observed_facts": {
            "degradation_percent": format(
                evidence.degradation_percent.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f"
            ),
            "alert_count": len(evidence.alerts),
            "max_alert_severity": max(
                (item["severity"] for item in evidence.alerts),
                key=lambda item: ALERT_SEVERITIES[item],
                default=None,
            ),
            "recall_level": evidence.recall_level,
            "cycle_count": evidence.cycle_count,
        },
        "drivers": drivers,
    }


@dataclass(frozen=True, slots=True)
class RankedBattery:
    ranking: int
    battery_id: str
    facility_id: str
    risk_score: int
    risk_band: str
    blocked: bool
    score_detail: Mapping[str, Any]

    @property
    def sort_key(self) -> tuple[int, str]:
        # 分数降序、编号升序，保证确定性。
        return (-self.risk_score, self.battery_id)


def rank(
    evidence_rows: Sequence[HealthEvidence],
    rulebook: RiskRulebook,
) -> list[dict[str, Any]]:
    """对证据全集评分并形成待办优先级序列。"""
    scored = [score_evidence(item, rulebook) for item in evidence_rows]
    scored.sort(key=lambda item: (-int(item["risk_score"]), item["battery_id"]))
    for index, item in enumerate(scored, start=1):
        item["ranking"] = index
        item["slack_days"] = _slack_days(int(item["risk_score"]), rulebook)
    return scored


def _slack_days(score: int, rulebook: RiskRulebook) -> int:
    """距高风险线的宽限天数，供界面解释“为什么现在排”。"""
    if score >= rulebook.high_risk_score:
        return 0
    gap = max(1, rulebook.high_risk_score - score)
    return max(0, rulebook.horizon_days * gap // rulebook.high_risk_score)
