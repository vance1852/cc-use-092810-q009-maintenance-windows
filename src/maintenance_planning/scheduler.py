"""在容量、人员、备件与隔离约束内生成确定性维修窗口（纯函数）。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping, Sequence


ZERO = Decimal("0")


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


@dataclass(frozen=True, slots=True)
class Crew:
    crew_id: str
    size: int
    certifications: frozenset[str]


@dataclass
class _FacilityDay:
    quota: Decimal
    used: Decimal = ZERO

    @property
    def remaining(self) -> Decimal:
        return self.quota - self.used


def _parse_crews(rows: Sequence[Mapping[str, Any]]) -> list[Crew]:
    crews: list[Crew] = []
    for row in rows:
        crews.append(Crew(
            crew_id=str(row["crew_id"]),
            size=int(row.get("size", 1)),
            certifications=frozenset(str(item) for item in row.get("certifications", ())),
        ))
    return sorted(crews, key=lambda item: item.crew_id)


def build_schedule(
    *,
    ranked: Sequence[Mapping[str, Any]],
    evidence_by_battery: Mapping[str, Mapping[str, Any]],
    activity_by_battery: Mapping[str, Mapping[str, Any]],
    crews: Sequence[Mapping[str, Any]],
    spare_availability: Mapping[str, int],
    facility_calendars: Mapping[str, Mapping[str, Decimal]],
    service_dates: Sequence[str],
    crew_unavailable: frozenset[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """按风险优先级逐套设备分配最早可行窗口。

    facility_calendars 已由服务层合并其它已批准计划的冻结占用；
    crew_unavailable 是其它已批准计划已冻结的 (班组, 日期) 占用；
    本函数只负责本计划内部的资源扣减与冲突裁决，全部选择按编号确定性排序。
    """
    parsed_crews = _parse_crews(crews)
    external_crew_holds = crew_unavailable or frozenset()
    dates = sorted(service_dates)
    facility_days: dict[tuple[str, str], _FacilityDay] = {}
    for facility_id, calendar in facility_calendars.items():
        for day, quota in calendar.items():
            facility_days[(facility_id, day)] = _FacilityDay(_decimal(quota))
    spares = {str(kind): int(quantity) for kind, quantity in spare_availability.items()}
    booked_crews: set[tuple[str, str]] = set(external_crew_holds)

    windows: list[dict[str, Any]] = []
    unscheduled: list[dict[str, Any]] = []
    arbitrations: list[dict[str, Any]] = []

    def find_feasible(battery_id: str, facility_id: str, footprint: Decimal, activity: Mapping[str, Any]):
        required_certs = frozenset(str(item) for item in activity.get("required_certifications", ()))
        minimum_crew = int(activity.get("minimum_crew", 1))
        needed_spares = {str(k): int(v) for k, v in activity.get("required_spare_kinds", {}).items()}
        blockers: list[dict[str, Any]] = []
        for day in dates:
            day_cell = facility_days.get((facility_id, day))
            quota_ok = day_cell is not None and day_cell.remaining >= footprint
            if not quota_ok:
                blockers.append({
                    "service_date": day,
                    "resource": "facility_shutdown_quota",
                    "detail": "场站当日可停机额度不足或未开放",
                    "remaining": None if day_cell is None else format(day_cell.remaining, "f"),
                    "required": format(footprint, "f"),
                })
                continue
            eligible = [
                crew for crew in parsed_crews
                if (crew.crew_id, day) not in booked_crews
                and crew.size >= minimum_crew
                and required_certs <= crew.certifications
            ]
            if not eligible:
                blockers.append({
                    "service_date": day,
                    "resource": "crew",
                    "detail": "没有同时满足资质与人数且当日空闲的班组",
                    "required_certifications": sorted(required_certs),
                })
                continue
            missing = {kind: qty for kind, qty in needed_spares.items() if spares.get(kind, 0) < qty}
            if missing:
                blockers.append({
                    "service_date": day,
                    "resource": "spare",
                    "detail": "备件可用数量不足",
                    "missing": missing,
                })
                continue
            crew = eligible[0]
            return day, day_cell, crew, sorted(needed_spares.items()), blockers
        return None

    for item in ranked:
        battery_id = str(item["battery_id"])
        facility_id = str(item["facility_id"])
        evidence = evidence_by_battery[battery_id]
        footprint = _decimal(evidence["rated_capacity_kwh"])
        if item.get("blocked"):
            unscheduled.append({
                "battery_id": battery_id,
                "facility_id": facility_id,
                "risk_score": item["risk_score"],
                "risk_band": item["risk_band"],
                "reasons": ["recall_blocked"],
                "detail": "召回限制禁止按常规维修活动排期，需走召回处置通道",
                "exposed_capacity_kwh": format(footprint, "f"),
            })
            continue
        activity = activity_by_battery.get(battery_id)
        if activity is None:
            unscheduled.append({
                "battery_id": battery_id,
                "facility_id": facility_id,
                "risk_score": item["risk_score"],
                "risk_band": item["risk_band"],
                "reasons": ["activity_undefined"],
                "exposed_capacity_kwh": format(footprint, "f"),
            })
            continue
        feasible = find_feasible(battery_id, facility_id, footprint, activity)
        if feasible is None:
            continue
        day, day_cell, crew, needed_spares, blockers = feasible
        before = day_cell.remaining
        day_cell.used += footprint
        booked_crews.add((crew.crew_id, day))
        spare_lines = []
        for kind, qty in needed_spares:
            before_qty = spares.get(kind, 0)
            spares[kind] = before_qty - qty
            spare_lines.append({"spare_kind": kind, "quantity": qty, "available_before": before_qty, "available_after": before_qty - qty})
        selection_reasons = [
            f"风险分 {item['risk_score']}（{item['risk_band']}），待办优先级 {item['ranking']}/{len(ranked)}，宽限 {item.get('slack_days', 0)} 天",
            f"在候选日期中选取最早可行日 {day}",
            f"场站 {facility_id} 当日停机额度 {format(day_cell.quota, 'f')}kWh，本窗口离线 {format(footprint, 'f')}kWh，占用后余 {format(day_cell.remaining, 'f')}kWh",
            f"班组 {crew.crew_id}（{crew.size} 人，资质 {'、'.join(sorted(crew.certifications)) or '无要求'}）满足 {activity['activity_id']} 并在当日空闲",
        ]
        if spare_lines:
            selection_reasons.append(
                "备件扣减：" + "；".join(f"{line['spare_kind']} {line['quantity']}（余 {line['available_after']}）" for line in spare_lines)
            )
        if activity.get("isolation_required", True):
            selection_reasons.append("活动要求电气隔离，已计入场站离线容量")
        windows.append({
            "battery_id": battery_id,
            "facility_id": facility_id,
            "activity_id": activity["activity_id"],
            "service_date": day,
            "energy_offline_kwh": format(footprint, "f"),
            "crew_id": crew.crew_id,
            "spares": spare_lines,
            "isolation_required": bool(activity.get("isolation_required", True)),
            "estimated_hours": format(_decimal(activity.get("estimated_hours", 4)), "f"),
            "risk_score": item["risk_score"],
            "risk_band": item["risk_band"],
            "selection_reasons": selection_reasons,
        })
        # 记录被本窗口挤出更早候选日的资源竞争裁决。
        for block in blockers:
            if block["service_date"] == day:
                continue
            arbitrations.append({
                "winner_battery_id": battery_id,
                "winner_risk_score": item["risk_score"],
                "service_date": block["service_date"],
                "resource": block["resource"],
                "rule": "higher_risk_takes_earliest_date",
                "detail": block["detail"],
            })

    # 第二轮：为未能排程的设备汇总确定性原因（上面只在闭包内累积尝试明细）。
    final_unscheduled: list[dict[str, Any]] = []
    scheduled_ids = {str(window["battery_id"]) for window in windows}
    blocked_ids = {row["battery_id"] for row in unscheduled}
    for row in unscheduled:
        final_unscheduled.append(row)
    for item in ranked:
        battery_id = str(item["battery_id"])
        if battery_id in scheduled_ids or battery_id in blocked_ids:
            continue
        reasons = _unscheduled_reasons(
            item, evidence_by_battery[battery_id], activity_by_battery.get(battery_id),
            parsed_crews, spares, facility_days, dates, booked_crews,
        )
        final_unscheduled.append({
            "battery_id": battery_id,
            "facility_id": item["facility_id"],
            "risk_score": item["risk_score"],
            "risk_band": item["risk_band"],
            "reasons": reasons["codes"],
            "attempts": reasons["attempts"],
            "exposed_capacity_kwh": format(_decimal(evidence_by_battery[battery_id]["rated_capacity_kwh"]), "f"),
        })

    facility_totals: dict[str, dict[str, str]] = {}
    for (facility_id, day), cell in sorted(facility_days.items()):
        if cell.used == ZERO:
            continue
        totals = facility_totals.setdefault(facility_id, {})
        totals[day] = format(cell.used, "f")

    return {
        "windows": windows,
        "unscheduled": final_unscheduled,
        "arbitrations": arbitrations,
        "facility_usage": facility_totals,
        "spare_remaining": dict(sorted(spares.items())),
    }


def _unscheduled_reasons(
    item: Mapping[str, Any],
    evidence: Mapping[str, Any],
    activity: Mapping[str, Any] | None,
    crews: Sequence[Crew],
    spares: Mapping[str, int],
    facility_days: Mapping[tuple[str, str], _FacilityDay],
    dates: Sequence[str],
    booked_crews: set[tuple[str, str]],
) -> dict[str, Any]:
    facility_id = str(item["facility_id"])
    footprint = _decimal(evidence["rated_capacity_kwh"])
    codes: set[str] = set()
    attempts: list[dict[str, Any]] = []
    quota_ever = False
    for day in dates:
        cell = facility_days.get((facility_id, day))
        if cell is None or cell.remaining < footprint:
            codes.add("no_shutdown_quota")
            attempts.append({"service_date": day, "resource": "facility_shutdown_quota",
                             "remaining": None if cell is None else format(cell.remaining, "f")})
            continue
        quota_ever = True
        required_certs = frozenset() if activity is None else frozenset(
            str(x) for x in activity.get("required_certifications", ())
        )
        minimum_crew = 1 if activity is None else int(activity.get("minimum_crew", 1))
        eligible = [
            crew for crew in crews
            if (crew.crew_id, day) not in booked_crews
            and crew.size >= minimum_crew and required_certs <= crew.certifications
        ]
        if not eligible:
            catalog_match = any(c.size >= minimum_crew and required_certs <= c.certifications for c in crews)
            codes.add("crew_unavailable" if catalog_match else "no_qualified_crew")
            attempts.append({
                "service_date": day, "resource": "crew",
                "reason": "合格班组当日已被更高优先级窗口占用" if catalog_match else "目录内无满足资质/人数的班组",
            })
            continue
        needed = {} if activity is None else {str(k): int(v) for k, v in activity.get("required_spare_kinds", {}).items()}
        missing = {k: v for k, v in needed.items() if spares.get(k, 0) < v}
        if missing:
            codes.add("insufficient_spares")
            attempts.append({"service_date": day, "resource": "spare", "missing": missing})
    if not quota_ever and not codes:
        codes.add("horizon_empty")
    return {"codes": sorted(codes) or ["no_feasible_window"], "attempts": attempts}
