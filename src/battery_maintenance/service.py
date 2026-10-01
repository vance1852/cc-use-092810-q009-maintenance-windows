"""维修计划领域用例：证据与规则登记、计划生成/批准、信号失效与现场回执。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Iterable, Mapping

from .clock import SystemClock, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .models import (
    Device,
    HealthEvidence,
    IsolationBay,
    PlanRequest,
    RiskRuleSet,
    SparePart,
    Station,
    Technician,
    UnavailabilityWindow,
)
from .planning import (
    ResourceSnapshot,
    canonical_json,
    decimal_text,
    digest,
    schedule_windows,
    score_evidence,
)
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "planner": {
        "resource.write", "evidence.write", "plan.generate", "signal.record", "window.extend",
        "report.read",
    },
    "risk": {"ruleset.publish", "plan.approve", "report.read"},
    "technician": {"receipt.send", "report.read"},
    "auditor": {"report.read", "audit.read"},
}

ACTIVE_WORK_STATES = ("approved", "in_progress", "paused")
DAY = timedelta(days=1)


class MaintenancePlanService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    # ── 基础设施工具 ────────────────────────────────────────────────

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM maintenance_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM maintenance_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO maintenance_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    def _idempotent(self, scope: str, key: str, request_digest: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT request_sha256,response_json FROM maintenance_idempotency WHERE scope=? AND idempotency_key=?",
            (scope, key),
        ).fetchone()
        if row is None:
            return None
        if row["request_sha256"] != request_digest:
            raise Conflict("同一幂等键对应了不同请求内容")
        return json.loads(row["response_json"])

    def _save_idempotent(self, scope: str, key: str, request_digest: str, response: Mapping[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO maintenance_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
            "VALUES(?,?,?,?,?)",
            (scope, key, request_digest, canonical_json(response), self._now()),
        )

    # ── 用户与资源台账 ──────────────────────────────────────────────

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO maintenance_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    def register_station(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "resource.write")
        station = Station.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO stations(station_id,name,timezone,daily_outage_kwh,created_at) VALUES(?,?,?,?,?)",
                    (station.station_id, station.name, station.timezone,
                     decimal_text(station.daily_outage_kwh), self._now()),
                )
                self._audit("station", station.station_id, "station.registered", actor_id, {"name": station.name})
        except sqlite3.IntegrityError as exc:
            raise Conflict("场站编号已经存在") from exc
        return {"station_id": station.station_id, "daily_outage_kwh": decimal_text(station.daily_outage_kwh)}

    def register_device(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "resource.write")
        device = Device.from_dict(raw)
        if self.connection.execute("SELECT 1 FROM stations WHERE station_id=?", (device.station_id,)).fetchone() is None:
            raise NotFound("场站不存在")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO devices(device_id,station_id,device_kind,model_name,rated_capacity_kwh,"
                    "required_qualification,required_isolation_json,required_spare_skus_json,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        device.device_id, device.station_id, device.device_kind, device.model_name,
                        decimal_text(device.rated_capacity_kwh), device.required_qualification,
                        canonical_json(sorted(device.required_isolation)),
                        canonical_json(sorted(device.required_spare_skus)),
                        self._now(),
                    ),
                )
                self._audit("device", device.device_id, "device.registered", actor_id,
                            {"station_id": device.station_id, "device_kind": device.device_kind})
        except sqlite3.IntegrityError as exc:
            raise Conflict("设备编号已经存在") from exc
        return {"device_id": device.device_id, "station_id": device.station_id}

    def register_technician(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "resource.write")
        technician = Technician.from_dict(raw)
        user = self.connection.execute(
            "SELECT role FROM maintenance_users WHERE user_id=?", (technician.technician_id,)
        ).fetchone()
        if user is None:
            raise NotFound("技师用户不存在，请先创建用户")
        if user["role"] != "technician":
            raise ValidationFailed("只有 technician 角色用户可以登记为现场技师")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO technicians(technician_id,display_name,created_at) VALUES(?,?,?)",
                    (technician.technician_id, technician.display_name, self._now()),
                )
                for qualification in sorted(technician.qualifications):
                    self.connection.execute(
                        "INSERT INTO technician_qualifications(technician_id,qualification) VALUES(?,?)",
                        (technician.technician_id, qualification),
                    )
                self._audit("technician", technician.technician_id, "technician.registered", actor_id,
                            {"qualifications": sorted(technician.qualifications)})
        except sqlite3.IntegrityError as exc:
            raise Conflict("技师已经存在或用户不存在") from exc
        return {"technician_id": technician.technician_id,
                "qualifications": sorted(technician.qualifications)}

    def add_unavailability(self, actor_id: str, technician_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "resource.write")
        if self.connection.execute("SELECT 1 FROM technicians WHERE technician_id=?", (technician_id,)).fetchone() is None:
            raise NotFound("技师不存在")
        window = UnavailabilityWindow.from_dict(raw)
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO technician_unavailability(technician_id,starts_at,ends_at,reason,created_at) "
                "VALUES(?,?,?,?,?)",
                (technician_id, window.starts_at, window.ends_at,
                 str(raw.get("reason", ""))[:512], self._now()),
            )
            self._audit("technician", technician_id, "technician.unavailable", actor_id,
                        {"unavailability_id": cursor.lastrowid})
        return {"technician_id": technician_id, "starts_at": window.starts_at, "ends_at": window.ends_at}

    def register_bay(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "resource.write")
        bay = IsolationBay.from_dict(raw)
        if self.connection.execute("SELECT 1 FROM stations WHERE station_id=?", (bay.station_id,)).fetchone() is None:
            raise NotFound("场站不存在")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO isolation_bays(bay_id,station_id,created_at) VALUES(?,?,?)",
                    (bay.bay_id, bay.station_id, self._now()),
                )
                for kind in sorted(bay.isolation_kinds):
                    self.connection.execute(
                        "INSERT INTO bay_isolation_kinds(bay_id,isolation_kind) VALUES(?,?)",
                        (bay.bay_id, kind),
                    )
                self._audit("bay", bay.bay_id, "bay.registered", actor_id,
                            {"station_id": bay.station_id, "isolation_kinds": sorted(bay.isolation_kinds)})
        except sqlite3.IntegrityError as exc:
            raise Conflict("隔离位编号已经存在") from exc
        return {"bay_id": bay.bay_id, "isolation_kinds": sorted(bay.isolation_kinds)}

    def restock_part(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "resource.write")
        part = SparePart.from_dict(raw)
        if self.connection.execute("SELECT 1 FROM stations WHERE station_id=?", (part.station_id,)).fetchone() is None:
            raise NotFound("场站不存在")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO spare_parts(station_id,sku,quantity_on_hand,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(station_id,sku) DO UPDATE SET quantity_on_hand=excluded.quantity_on_hand,"
                "updated_at=excluded.updated_at",
                (part.station_id, part.sku, part.quantity_on_hand, self._now()),
            )
            self._audit("spare_part", f"{part.station_id}:{part.sku}", "spare.restocked", actor_id,
                        {"quantity_on_hand": part.quantity_on_hand})
        held = self.connection.execute(
            "SELECT COALESCE(sum(CAST(quantity AS INTEGER)),0) held FROM resource_reservations "
            "WHERE resource_type='spare' AND station_id=? AND resource_id=? AND state='held'",
            (part.station_id, part.sku),
        ).fetchone()["held"]
        return {"station_id": part.station_id, "sku": part.sku,
                "quantity_on_hand": part.quantity_on_hand, "available": part.quantity_on_hand - int(held)}

    # ── 健康证据与风险规则 ──────────────────────────────────────────

    def record_evidence(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "evidence.write")
        evidence = HealthEvidence.from_dict(raw)
        if self.connection.execute("SELECT 1 FROM devices WHERE device_id=?", (evidence.device_id,)).fetchone() is None:
            raise NotFound("设备不存在")
        content = {
            "evidence_id": evidence.evidence_id,
            "device_id": evidence.device_id,
            "version": evidence.version,
            "observed_at": evidence.observed_at,
            "capacity_retention_percent": decimal_text(evidence.capacity_retention_percent),
            "cycle_count": evidence.cycle_count,
            "alarm_severity": evidence.alarm_severity,
            "alarm_count_30d": evidence.alarm_count_30d,
            "recall_level": evidence.recall_level,
            "recall_code": evidence.recall_code,
            "note": evidence.note,
        }
        content_sha = digest(content)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO health_evidence(evidence_id,device_id,version,content_json,content_sha256,"
                    "observed_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (evidence.evidence_id, evidence.device_id, evidence.version, canonical_json(content),
                     content_sha, evidence.observed_at, actor_id, self._now()),
                )
                self._audit("evidence", evidence.evidence_id, "evidence.recorded", actor_id,
                            {"device_id": evidence.device_id, "version": evidence.version, "sha256": content_sha})
        except sqlite3.IntegrityError as exc:
            raise Conflict("证据编号或设备内版本号冲突") from exc
        return {"evidence_id": evidence.evidence_id, "device_id": evidence.device_id,
                "version": evidence.version, "content_sha256": content_sha}

    def publish_rule_set(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "ruleset.publish")
        rules = RiskRuleSet.from_dict(raw)
        definition = canonical_json(raw)
        content_sha = hashlib.sha256(definition.encode("utf-8")).hexdigest()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO risk_rule_set_versions(rule_set_id,version,definition_json,content_sha256,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?)",
                    (rules.rule_set_id, rules.version, definition, content_sha, actor_id, self._now()),
                )
                self.connection.execute(
                    "INSERT INTO risk_rule_sets(rule_set_id,current_version,updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(rule_set_id) DO UPDATE SET current_version=excluded.current_version,"
                    "updated_at=excluded.updated_at",
                    (rules.rule_set_id, rules.version, self._now()),
                )
                self._audit("rule_set", f"{rules.rule_set_id}@{rules.version}", "rule_set.published",
                            actor_id, {"sha256": content_sha})
        except sqlite3.IntegrityError as exc:
            raise Conflict("规则集版本已经存在") from exc
        return {"rule_set_id": rules.rule_set_id, "version": rules.version, "content_sha256": content_sha}

    def _current_rule_set(self, rule_set_id: str) -> tuple[RiskRuleSet, str, str]:
        pointer = self.connection.execute(
            "SELECT current_version FROM risk_rule_sets WHERE rule_set_id=?", (rule_set_id,)
        ).fetchone()
        if pointer is None:
            raise NotFound("风险规则集不存在")
        row = self.connection.execute(
            "SELECT definition_json,content_sha256 FROM risk_rule_set_versions WHERE rule_set_id=? AND version=?",
            (rule_set_id, pointer["current_version"]),
        ).fetchone()
        return RiskRuleSet.from_dict(json.loads(row["definition_json"])), pointer["current_version"], row["content_sha256"]

    # ── 计划生成 ────────────────────────────────────────────────────

    def _latest_evidence_rows(self, device_ids: Iterable[str]) -> dict[str, sqlite3.Row]:
        result: dict[str, sqlite3.Row] = {}
        for device_id in device_ids:
            row = self.connection.execute(
                "SELECT * FROM health_evidence WHERE device_id=? ORDER BY observed_at DESC,evidence_id DESC LIMIT 1",
                (device_id,),
            ).fetchone()
            if row is not None:
                result[device_id] = row
        return result

    def _horizon_dates(self, anchor: date, horizon_days: int) -> list[str]:
        return [(anchor + i * DAY).isoformat() for i in range(horizon_days)]

    def _build_resources(self, station_ids: frozenset[str], dates: list[str]) -> ResourceSnapshot:
        stations = self.connection.execute(
            "SELECT station_id,daily_outage_kwh FROM stations WHERE active=1 ORDER BY station_id"
        ).fetchall()
        quota: dict[str, dict[str, Decimal]] = {}
        for row in stations:
            if station_ids and row["station_id"] not in station_ids:
                continue
            daily = {day: Decimal(row["daily_outage_kwh"]) for day in dates}
            quota[row["station_id"]] = daily
        held_outage = self.connection.execute(
            "SELECT station_id,service_date,sum(CAST(quantity AS REAL)) qty FROM resource_reservations "
            "WHERE resource_type='station_outage' AND state='held' GROUP BY station_id,service_date"
        ).fetchall()
        for row in held_outage:
            if row["station_id"] in quota and row["service_date"] in quota[row["station_id"]]:
                quota[row["station_id"]][row["service_date"]] -= Decimal(str(row["qty"]))

        technicians: list[Mapping[str, object]] = []
        qualification_rows = self.connection.execute(
            "SELECT technician_id,qualification FROM technician_qualifications ORDER BY technician_id,qualification"
        ).fetchall()
        quals: dict[str, set[str]] = {}
        for row in qualification_rows:
            quals.setdefault(row["technician_id"], set()).add(row["qualification"])
        tech_rows = self.connection.execute(
            "SELECT technician_id FROM technicians ORDER BY technician_id"
        ).fetchall()
        for row in tech_rows:
            technicians.append({"technician_id": row["technician_id"],
                                "qualifications": frozenset(quals.get(row["technician_id"], set()))})

        unavailable: dict[str, list[tuple[str, str]]] = {}
        leave_rows = self.connection.execute(
            "SELECT technician_id,starts_at,ends_at FROM technician_unavailability ORDER BY unavailability_id"
        ).fetchall()
        for row in leave_rows:
            unavailable.setdefault(row["technician_id"], []).append((row["starts_at"], row["ends_at"]))
        held_tech = self.connection.execute(
            "SELECT resource_id,service_date FROM resource_reservations "
            "WHERE resource_type='technician' AND state='held'"
        ).fetchall()
        for row in held_tech:
            unavailable.setdefault(row["resource_id"], []).append(
                (f"{row['service_date']}T00:00:00Z", f"{row['service_date']}T23:59:59.999999Z")
            )

        bays: list[Mapping[str, object]] = []
        bay_rows = self.connection.execute("SELECT bay_id,station_id FROM isolation_bays ORDER BY bay_id").fetchall()
        bay_kinds: dict[str, set[str]] = {}
        for row in self.connection.execute("SELECT bay_id,isolation_kind FROM bay_isolation_kinds"):
            bay_kinds.setdefault(row["bay_id"], set()).add(row["isolation_kind"])
        for row in bay_rows:
            if station_ids and row["station_id"] not in station_ids:
                continue
            bays.append({"bay_id": row["bay_id"], "station_id": row["station_id"],
                         "isolation_kinds": frozenset(bay_kinds.get(row["bay_id"], set()))})
        busy_bays: set[tuple[str, str]] = {
            (row["resource_id"], row["service_date"])
            for row in self.connection.execute(
                "SELECT resource_id,service_date FROM resource_reservations "
                "WHERE resource_type='bay' AND state='held'"
            ).fetchall()
        }

        spare_availability: dict[tuple[str, str], int] = {}
        part_rows = self.connection.execute("SELECT station_id,sku,quantity_on_hand FROM spare_parts").fetchall()
        for row in part_rows:
            spare_availability[(row["station_id"], row["sku"])] = int(row["quantity_on_hand"])
        held_spare = self.connection.execute(
            "SELECT station_id,resource_id,sum(CAST(quantity AS INTEGER)) qty FROM resource_reservations "
            "WHERE resource_type='spare' AND state='held' GROUP BY station_id,resource_id"
        ).fetchall()
        for row in held_spare:
            key = (row["station_id"], row["resource_id"])
            spare_availability[key] = spare_availability.get(key, 0) - int(row["qty"])

        return ResourceSnapshot(
            station_quota_by_date=quota,
            technicians=technicians,
            unavailable=unavailable,
            bays=bays,
            busy_bays=frozenset(busy_bays),
            spare_availability=spare_availability,
        )

    def generate_plan(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "plan.generate")
        request = PlanRequest.from_dict(raw)
        request_digest = digest(raw)
        replay = self._idempotent("plan", request.idempotency_key, request_digest)
        if replay is not None:
            return replay
        rules, rule_version, rule_sha = self._current_rule_set(request.rule_set_id)

        device_rows = self.connection.execute(
            "SELECT * FROM devices WHERE active=1 ORDER BY device_id"
        ).fetchall()
        if request.station_ids:
            device_rows = [row for row in device_rows if row["station_id"] in request.station_ids]
        # 已有未执行或执行中窗口的设备不重复排程；只有失效/取消/失败的设备才重新进入计划
        busy_rows = self.connection.execute(
            "SELECT DISTINCT device_id FROM maintenance_windows "
            "WHERE state IN ('approved','in_progress','paused')"
        ).fetchall()
        busy_devices = {row["device_id"] for row in busy_rows}
        device_rows = [row for row in device_rows if row["device_id"] not in busy_devices]
        devices: dict[str, Device] = {}
        for row in device_rows:
            devices[row["device_id"]] = Device(
                device_id=row["device_id"],
                station_id=row["station_id"],
                device_kind=row["device_kind"],
                model_name=row["model_name"],
                rated_capacity_kwh=Decimal(row["rated_capacity_kwh"]),
                required_qualification=row["required_qualification"],
                required_isolation=frozenset(json.loads(row["required_isolation_json"])),
                required_spare_skus=frozenset(json.loads(row["required_spare_skus_json"])),
            )
        evidence_rows = self._latest_evidence_rows(devices.keys())
        if not evidence_rows:
            raise InvalidState("所选范围内没有任何健康证据，无法生成计划")
        anchor = self.clock.now().date()
        dates = self._horizon_dates(anchor, request.horizon_days)
        scores = [
            score_evidence(HealthEvidence.from_dict(json.loads(row["content_json"])), rules, anchor_date=anchor)
            for row in evidence_rows.values()
        ]
        resources = self._build_resources(request.station_ids, dates)
        scheduled = schedule_windows(
            scores=scores, devices=devices, rules=rules, resources=resources,
            anchor_date=anchor, horizon_days=request.horizon_days,
        )

        freeze_material = {
            "anchor_date": anchor.isoformat(),
            "horizon_days": request.horizon_days,
            "station_ids": sorted(request.station_ids),
            "rule_set": {"rule_set_id": request.rule_set_id, "version": rule_version, "content_sha256": rule_sha},
            "evidence": sorted(
                (
                    {
                        "device_id": row["device_id"],
                        "evidence_id": row["evidence_id"],
                        "version": row["version"],
                        "content_sha256": row["content_sha256"],
                    }
                    for row in evidence_rows.values()
                ),
                key=lambda item: item["device_id"],
            ),
        }
        input_sha = digest(freeze_material)
        response = {
            "plan_id": request.plan_id,
            "state": "proposed",
            "rule_set_id": request.rule_set_id,
            "rule_set_version": rule_version,
            "anchor_date": anchor.isoformat(),
            "horizon_days": request.horizon_days,
            "input_sha256": input_sha,
            "window_count": len(scheduled["windows"]),
            "unscheduled_count": len(scheduled["unscheduled"]),
            "windows": scheduled["windows"],
            "unscheduled": scheduled["unscheduled"],
        }
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO maintenance_plans(plan_id,rule_set_id,rule_set_version,state,anchor_date,"
                    "horizon_days,station_ids_json,input_sha256,result_json,idempotency_key,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        request.plan_id, request.rule_set_id, rule_version, "proposed", anchor.isoformat(),
                        request.horizon_days, canonical_json(sorted(request.station_ids)), input_sha,
                        canonical_json(scheduled), request.idempotency_key, actor_id, self._now(),
                    ),
                )
                for item in scheduled["windows"]:
                    window_id = f"{request.plan_id}:{item['device_id']}"
                    self.connection.execute(
                        "INSERT INTO maintenance_windows(window_id,plan_id,device_id,station_id,service_date,"
                        "original_date,latest_date,risk_level,risk_points,evidence_id,technician_id,bay_id,"
                        "required_spare_skus_json,required_isolation_json,rationale_json,state,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            window_id, request.plan_id, item["device_id"], item["station_id"],
                            item["service_date"], item["service_date"], item["latest_date"],
                            item["risk_level"], item["risk_points"], item["evidence_id"],
                            item["technician_id"], item["bay_id"],
                            canonical_json(item["required_spare_skus"]),
                            canonical_json(item["required_isolation"]),
                            canonical_json(item["rationale"]),
                            "proposed",
                            self._now(),
                        ),
                    )
                self._save_idempotent("plan", request.idempotency_key, request_digest, response)
                self._audit("plan", request.plan_id, "plan.generated", actor_id,
                            {"input_sha256": input_sha, "windows": len(scheduled["windows"]),
                             "unscheduled": len(scheduled["unscheduled"])})
        except sqlite3.IntegrityError as exc:
            raise Conflict("计划编号或幂等键冲突") from exc
        return response

    # ── 批准：冻结输入与资源占用 ────────────────────────────────────

    def _freeze_material_current(self, plan: sqlite3.Row) -> dict[str, Any]:
        station_ids = frozenset(json.loads(plan["station_ids_json"]))
        device_rows = self.connection.execute("SELECT device_id,station_id FROM devices ORDER BY device_id").fetchall()
        busy_devices = {
            row["device_id"] for row in self.connection.execute(
                "SELECT DISTINCT device_id FROM maintenance_windows "
                "WHERE state IN ('approved','in_progress','paused')"
            ).fetchall()
        }
        device_ids = [
            row["device_id"] for row in device_rows
            if (not station_ids or row["station_id"] in station_ids)
            and row["device_id"] not in busy_devices
        ]
        evidence_rows = self._latest_evidence_rows(device_ids)
        rule_row = self.connection.execute(
            "SELECT v.content_sha256 FROM risk_rule_sets p JOIN risk_rule_set_versions v "
            "ON v.rule_set_id=p.rule_set_id AND v.version=p.current_version WHERE p.rule_set_id=?",
            (plan["rule_set_id"],),
        ).fetchone()
        return {
            "anchor_date": plan["anchor_date"],
            "horizon_days": plan["horizon_days"],
            "station_ids": sorted(station_ids),
            "rule_set": {"rule_set_id": plan["rule_set_id"], "version": plan["rule_set_version"],
                         "content_sha256": rule_row["content_sha256"]},
            "evidence": sorted(
                (
                    {
                        "device_id": row["device_id"],
                        "evidence_id": row["evidence_id"],
                        "version": row["version"],
                        "content_sha256": row["content_sha256"],
                    }
                    for row in evidence_rows.values()
                ),
                key=lambda item: item["device_id"],
            ),
        }

    def approve_plan(self, actor_id: str, plan_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "plan.approve")
        plan = self.connection.execute("SELECT * FROM maintenance_plans WHERE plan_id=?", (plan_id,)).fetchone()
        if plan is None:
            raise NotFound("计划不存在")
        if plan["state"] != "proposed" or plan["revision"] != expected_revision:
            raise InvalidState("计划不是当前待批准草稿版本")
        current_material = self._freeze_material_current(plan)
        if digest(current_material) != plan["input_sha256"]:
            raise InvalidState("生成后健康证据或风险规则已变化，请重新生成计划后再批准")
        windows = self.connection.execute(
            "SELECT * FROM maintenance_windows WHERE plan_id=? AND state='proposed' ORDER BY service_date,window_id",
            (plan_id,),
        ).fetchall()

        with transaction(self.connection, immediate=True):
            plan_row = self.connection.execute(
                "SELECT state,revision FROM maintenance_plans WHERE plan_id=?", (plan_id,)
            ).fetchone()
            if plan_row is None or plan_row["state"] != "proposed" or plan_row["revision"] != expected_revision:
                raise InvalidState("计划在批准过程中状态已变化")
            # 事务持锁后重算输入摘要，消除检查与提交之间的竞态窗口
            if digest(self._freeze_material_current(plan)) != plan["input_sha256"]:
                raise InvalidState("生成后健康证据或风险规则已变化，请重新生成计划后再批准")
            conflicts = self._check_resources(windows)
            if conflicts:
                raise Conflict("资源已被其他已批准计划占用，请重新生成计划：" + canonical_json(conflicts))
            now = self._now()
            for window in windows:
                wid = window["window_id"]
                device = self.connection.execute(
                    "SELECT rated_capacity_kwh FROM devices WHERE device_id=?", (window["device_id"],)
                ).fetchone()
                self.connection.execute(
                    "INSERT INTO resource_reservations(window_id,resource_type,station_id,resource_id,"
                    "service_date,quantity,created_at) VALUES(?,?,?,?,?,?,?)",
                    (wid, "station_outage", window["station_id"], None, window["service_date"],
                     device["rated_capacity_kwh"], now),
                )
                self.connection.execute(
                    "INSERT INTO resource_reservations(window_id,resource_type,station_id,resource_id,"
                    "service_date,created_at) VALUES(?,?,?,?,?,?)",
                    (wid, "technician", window["station_id"], window["technician_id"],
                     window["service_date"], now),
                )
                if window["bay_id"]:
                    self.connection.execute(
                        "INSERT INTO resource_reservations(window_id,resource_type,station_id,resource_id,"
                        "service_date,created_at) VALUES(?,?,?,?,?,?)",
                        (wid, "bay", window["station_id"], window["bay_id"], window["service_date"], now),
                    )
                for sku in json.loads(window["required_spare_skus_json"]):
                    self.connection.execute(
                        "INSERT INTO resource_reservations(window_id,resource_type,station_id,resource_id,"
                        "service_date,quantity,created_at) VALUES(?,?,?,?,?,?,?)",
                        (wid, "spare", window["station_id"], sku, window["service_date"], "1", now),
                    )
                self.connection.execute(
                    "UPDATE maintenance_windows SET state='approved',revision=revision+1 WHERE window_id=?",
                    (wid,),
                )
            snapshot = {item["device_id"]: item for item in current_material["evidence"]}
            for device_id, item in snapshot.items():
                self.connection.execute(
                    "INSERT INTO plan_evidence_snapshot(plan_id,device_id,evidence_id,evidence_version,content_sha256) "
                    "VALUES(?,?,?,?,?)",
                    (plan_id, device_id, item["evidence_id"], item["version"], item["content_sha256"]),
                )
            self.connection.execute(
                "UPDATE maintenance_plans SET state='approved',revision=revision+1,approved_by=?,approved_at=? "
                "WHERE plan_id=? AND state='proposed' AND revision=?",
                (actor_id, now, plan_id, expected_revision),
            )
            self.connection.execute(
                "UPDATE maintenance_plans SET state='superseded',revision=revision+1 "
                "WHERE state='proposed' AND plan_id<>?",
                (plan_id,),
            )
            self.connection.execute(
                "UPDATE maintenance_windows SET state='superseded',revision=revision+1 "
                "WHERE state='proposed' AND plan_id<>?",
                (plan_id,),
            )
            self._audit("plan", plan_id, "plan.approved", actor_id,
                        {"windows": len(windows), "input_sha256": plan["input_sha256"]})
        return {"plan_id": plan_id, "state": "approved", "revision": expected_revision + 1,
                "windows": len(windows), "input_sha256": plan["input_sha256"]}

    def _check_resources(self, windows: list[sqlite3.Row]) -> list[dict[str, Any]]:
        """对照当前 held 预留和库存，验证待批准窗口仍可全部落地。"""

        conflicts: list[dict[str, Any]] = []
        outage: dict[tuple[str, str], Decimal] = {}
        for row in self.connection.execute(
            "SELECT station_id,service_date,sum(CAST(quantity AS REAL)) qty FROM resource_reservations "
            "WHERE resource_type='station_outage' AND state='held' GROUP BY station_id,service_date"
        ):
            outage[(row["station_id"], row["service_date"])] = Decimal(str(row["qty"]))
        tech_busy = {
            (row["resource_id"], row["service_date"])
            for row in self.connection.execute(
                "SELECT resource_id,service_date FROM resource_reservations "
                "WHERE resource_type='technician' AND state='held'"
            )
        }
        bay_busy = {
            (row["resource_id"], row["service_date"])
            for row in self.connection.execute(
                "SELECT resource_id,service_date FROM resource_reservations "
                "WHERE resource_type='bay' AND state='held'"
            )
        }
        spare_held: dict[tuple[str, str], int] = {}
        for row in self.connection.execute(
            "SELECT station_id,resource_id,sum(CAST(quantity AS INTEGER)) qty FROM resource_reservations "
            "WHERE resource_type='spare' AND state='held' GROUP BY station_id,resource_id"
        ):
            spare_held[(row["station_id"], row["resource_id"])] = int(row["qty"])
        station_quota = {
            row["station_id"]: Decimal(row["daily_outage_kwh"])
            for row in self.connection.execute("SELECT station_id,daily_outage_kwh FROM stations")
        }
        spare_stock = {
            (row["station_id"], row["sku"]): int(row["quantity_on_hand"])
            for row in self.connection.execute("SELECT station_id,sku,quantity_on_hand FROM spare_parts")
        }
        leave_rows = self.connection.execute(
            "SELECT technician_id,starts_at,ends_at FROM technician_unavailability"
        ).fetchall()

        planned_outage: dict[tuple[str, str], Decimal] = {}
        planned_tech: set[tuple[str, str]] = set()
        planned_bay: set[tuple[str, str]] = set()
        planned_spare: dict[tuple[str, str], int] = {}
        for window in windows:
            key = (window["station_id"], window["service_date"])
            device = self.connection.execute(
                "SELECT rated_capacity_kwh FROM devices WHERE device_id=?", (window["device_id"],)
            ).fetchone()
            capacity = Decimal(device["rated_capacity_kwh"])
            used = outage.get(key, Decimal("0")) + planned_outage.get(key, Decimal("0"))
            if used + capacity > station_quota.get(window["station_id"], Decimal("0")):
                conflicts.append({"window_id": window["window_id"], "resource": "station_outage",
                                  "service_date": window["service_date"],
                                  "used_kwh": decimal_text(used), "required_kwh": decimal_text(capacity),
                                  "quota_kwh": decimal_text(station_quota.get(window["station_id"], Decimal("0")))})
            planned_outage[key] = planned_outage.get(key, Decimal("0")) + capacity
            tkey = (window["technician_id"], window["service_date"])
            if tkey in tech_busy or tkey in planned_tech:
                conflicts.append({"window_id": window["window_id"], "resource": "technician",
                                  "technician_id": window["technician_id"],
                                  "service_date": window["service_date"]})
            day_start = f"{window['service_date']}T00:00:00Z"
            day_end = f"{window['service_date']}T23:59:59.999999Z"
            for leave in leave_rows:
                if leave["technician_id"] != window["technician_id"]:
                    continue
                if leave["starts_at"] <= day_end and leave["ends_at"] > day_start:
                    conflicts.append({"window_id": window["window_id"], "resource": "technician_leave",
                                      "technician_id": window["technician_id"],
                                      "service_date": window["service_date"],
                                      "leave_starts_at": leave["starts_at"],
                                      "leave_ends_at": leave["ends_at"]})
            planned_tech.add(tkey)
            if window["bay_id"]:
                bkey = (window["bay_id"], window["service_date"])
                if bkey in bay_busy or bkey in planned_bay:
                    conflicts.append({"window_id": window["window_id"], "resource": "bay",
                                      "bay_id": window["bay_id"], "service_date": window["service_date"]})
                planned_bay.add(bkey)
            for sku in json.loads(window["required_spare_skus_json"]):
                skey = (window["station_id"], sku)
                total = spare_held.get(skey, 0) + planned_spare.get(skey, 0) + 1
                if total > spare_stock.get(skey, 0):
                    conflicts.append({"window_id": window["window_id"], "resource": "spare",
                                      "station_id": window["station_id"], "sku": sku,
                                      "available": spare_stock.get(skey, 0),
                                      "requested": total})
                planned_spare[skey] = planned_spare.get(skey, 0) + 1
        return conflicts

    # ── 信号：只失效受影响的未执行窗口 ──────────────────────────────

    def record_signal(self, actor_id: str, device_id: str, signal_kind: str,
                      payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        self._require(actor_id, "signal.record")
        if signal_kind not in {"urgent_alarm", "recall_escalation", "evidence_changed"}:
            raise ValidationFailed("未知信号类型")
        if self.connection.execute("SELECT 1 FROM devices WHERE device_id=?", (device_id,)).fetchone() is None:
            raise NotFound("设备不存在")
        payload = dict(payload or {})
        reason_map = {"urgent_alarm": "urgent_alarm", "recall_escalation": "recall_escalation",
                      "evidence_changed": "evidence_changed"}
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO incoming_signals(device_id,signal_kind,payload_json,created_by,created_at) "
                "VALUES(?,?,?,?,?)",
                (device_id, signal_kind, canonical_json(payload), actor_id, self._now()),
            )
            signal_id = int(cursor.lastrowid)
            candidate_windows = self.connection.execute(
                "SELECT * FROM maintenance_windows WHERE device_id=? AND state='approved' ORDER BY window_id",
                (device_id,),
            ).fetchall()
            latest = self.connection.execute(
                "SELECT evidence_id FROM health_evidence WHERE device_id=? "
                "ORDER BY observed_at DESC,evidence_id DESC LIMIT 1",
                (device_id,),
            ).fetchone()
            invalidated: list[str] = []
            for window in candidate_windows:
                if signal_kind == "evidence_changed" and latest is not None \
                        and window["evidence_id"] == latest["evidence_id"]:
                    continue
                wid = window["window_id"]
                self.connection.execute(
                    "UPDATE maintenance_windows SET state='invalidated',revision=revision+1 WHERE window_id=? "
                    "AND state='approved'",
                    (wid,),
                )
                self._release_reservations(wid)
                self.connection.execute(
                    "INSERT INTO window_invalidations(window_id,reason,detail_json,created_by,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (wid, reason_map[signal_kind],
                     canonical_json({"signal_id": signal_id, "device_id": device_id, "payload": payload}),
                     actor_id, self._now()),
                )
                self._audit("window", wid, "window.invalidated", actor_id,
                            {"reason": reason_map[signal_kind], "signal_id": signal_id})
                invalidated.append(wid)
            self._audit("device", device_id, "signal.recorded", actor_id,
                        {"signal_id": signal_id, "kind": signal_kind, "invalidated": invalidated})
        return {"signal_id": signal_id, "device_id": device_id, "kind": signal_kind,
                "invalidated_windows": invalidated}

    def _release_reservations(self, window_id: str) -> None:
        self.connection.execute(
            "UPDATE resource_reservations SET state='released' WHERE window_id=? AND state='held'",
            (window_id,),
        )

    # ── 现场回执：幂等、去重、乱序安全、终态不可恢复 ────────────────

    def receive_receipt(
        self,
        actor_id: str,
        window_id: str,
        event: str,
        idempotency_key: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._require(actor_id, "receipt.send")
        if event not in {"started", "paused", "resumed", "completed", "retest"}:
            raise ValidationFailed("未知现场回执事件")
        if not idempotency_key:
            raise ValidationFailed("现场回执必须携带 idempotency_key 以支持重复投递去重")
        window = self.connection.execute(
            "SELECT * FROM maintenance_windows WHERE window_id=?", (window_id,)
        ).fetchone()
        if window is None:
            raise NotFound("维修窗口不存在")
        if window["technician_id"] and actor_id != window["technician_id"]:
            raise Forbidden("只有窗口指派的技师可以提出现场回执")
        payload = dict(payload or {})
        request_digest = digest({"window_id": window_id, "event": event, "payload": payload})
        if idempotency_key:
            replay = self._idempotent(f"receipt:{window_id}", idempotency_key, request_digest)
            if replay is not None:
                return replay
        allowed = {
            "started": {"approved": "in_progress"},
            "paused": {"in_progress": "paused"},
            "resumed": {"paused": "in_progress"},
            "completed": {"in_progress": "completed"},
        }
        now = self._now()
        with transaction(self.connection, immediate=True):
            if event == "retest":
                if window["state"] != "completed":
                    raise InvalidState("复测回执只在工单完工后有效，乱序消息被拒绝")
                state_after = "completed"
            else:
                if window["state"] not in allowed[event]:
                    raise InvalidState(
                        f"重复或乱序回执：{event} 不能作用于 {window['state']} 状态的工单；"
                        "已关闭工单不会被迟到消息恢复"
                    )
                state_after = allowed[event][window["state"]]
            try:
                cursor = self.connection.execute(
                    "INSERT INTO work_order_receipts(window_id,event,idempotency_key,payload_json,actor_id,"
                    "received_at) VALUES(?,?,?,?,?,?)",
                    (window_id, event, idempotency_key, canonical_json(payload), actor_id, now),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("回执幂等键冲突或重复投递") from exc
            receipt_id = int(cursor.lastrowid)
            if event == "started":
                self.connection.execute(
                    "UPDATE maintenance_windows SET state='in_progress',revision=revision+1,started_at=? "
                    "WHERE window_id=?",
                    (now, window_id),
                )
            elif event == "paused":
                self.connection.execute(
                    "UPDATE maintenance_windows SET state='paused',revision=revision+1,paused_at=? "
                    "WHERE window_id=?",
                    (now, window_id),
                )
            elif event == "resumed":
                self.connection.execute(
                    "UPDATE maintenance_windows SET state='in_progress',revision=revision+1,paused_at=NULL "
                    "WHERE window_id=?",
                    (window_id,),
                )
            elif event == "completed":
                self.connection.execute(
                    "UPDATE maintenance_windows SET state='completed',revision=revision+1,completed_at=? "
                    "WHERE window_id=?",
                    (now, window_id),
                )
                self.connection.execute(
                    "UPDATE resource_reservations SET state='consumed' "
                    "WHERE window_id=? AND resource_type='spare' AND state='held'",
                    (window_id,),
                )
                self.connection.execute(
                    "UPDATE resource_reservations SET state='released' "
                    "WHERE window_id=? AND resource_type!='spare' AND state='held'",
                    (window_id,),
                )
            response = {"receipt_id": receipt_id, "window_id": window_id, "event": event,
                        "state_after": state_after, "duplicate": False}
            if idempotency_key:
                self._save_idempotent(f"receipt:{window_id}", idempotency_key, request_digest, response)
            self._audit("window", window_id, f"receipt.{event}", actor_id,
                        {"receipt_id": receipt_id, "state_after": state_after})
        return response

    # ── 延期：记录风险接受人与新最迟日期 ────────────────────────────

    def extend_window(
        self,
        actor_id: str,
        window_id: str,
        new_latest_date: str,
        reason: str,
        risk_acceptor_id: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "window.extend")
        window = self.connection.execute(
            "SELECT * FROM maintenance_windows WHERE window_id=?", (window_id,)
        ).fetchone()
        if window is None:
            raise NotFound("维修窗口不存在")
        if window["state"] not in ACTIVE_WORK_STATES:
            raise InvalidState(f"{window['state']} 状态的窗口不能延期")
        try:
            new_date = date.fromisoformat(str(new_latest_date).strip())
        except ValueError as exc:
            raise ValidationFailed("new_latest_date 必须是 YYYY-MM-DD 日期") from exc
        previous_date = date.fromisoformat(window["latest_date"])
        if new_date <= previous_date:
            raise ValidationFailed("新的最迟日期必须晚于当前最迟日期")
        acceptor = self._user(risk_acceptor_id)
        if acceptor["role"] != "risk":
            raise ValidationFailed("风险接受人必须是 risk 角色用户")
        if not reason.strip():
            raise ValidationFailed("延期原因不能为空")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO window_extensions(window_id,previous_latest_date,new_latest_date,reason,"
                "risk_acceptor_id,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (window_id, window["latest_date"], new_date.isoformat(), reason.strip()[:1000],
                 risk_acceptor_id, actor_id, self._now()),
            )
            self.connection.execute(
                "UPDATE maintenance_windows SET latest_date=?,revision=revision+1 WHERE window_id=?",
                (new_date.isoformat(), window_id),
            )
            device = self.connection.execute(
                "SELECT rated_capacity_kwh FROM devices WHERE device_id=?", (window["device_id"],)
            ).fetchone()
            exposure_kwh_days = Decimal(device["rated_capacity_kwh"]) * (new_date - previous_date).days
            self._audit("window", window_id, "window.extended", actor_id,
                        {"extension_id": cursor.lastrowid, "previous_latest_date": window["latest_date"],
                         "new_latest_date": new_date.isoformat(), "risk_acceptor_id": risk_acceptor_id,
                         "exposure_kwh_days": decimal_text(exposure_kwh_days)})
        return {"window_id": window_id, "previous_latest_date": window["latest_date"],
                "new_latest_date": new_date.isoformat(), "risk_acceptor_id": risk_acceptor_id,
                "exposure_kwh_days": decimal_text(exposure_kwh_days)}

    # ── 运营视图 ────────────────────────────────────────────────────

    def plan_detail(self, actor_id: str, plan_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        plan = self.connection.execute("SELECT * FROM maintenance_plans WHERE plan_id=?", (plan_id,)).fetchone()
        if plan is None:
            raise NotFound("计划不存在")
        result = json.loads(plan["result_json"])
        snapshots = [
            dict(row) for row in self.connection.execute(
                "SELECT device_id,evidence_id,evidence_version,content_sha256 FROM plan_evidence_snapshot "
                "WHERE plan_id=? ORDER BY device_id", (plan_id,)
            ).fetchall()
        ]
        return {
            "plan_id": plan["plan_id"],
            "state": plan["state"],
            "revision": plan["revision"],
            "rule_set_id": plan["rule_set_id"],
            "rule_set_version": plan["rule_set_version"],
            "anchor_date": plan["anchor_date"],
            "horizon_days": plan["horizon_days"],
            "input_sha256": plan["input_sha256"],
            "approved_by": plan["approved_by"],
            "approved_at": plan["approved_at"],
            "frozen_evidence": snapshots,
            "arbitration": result.get("arbitration", []),
            "daily_load": result.get("daily_load", []),
            "unscheduled": result.get("unscheduled", []),
        }

    def _window_receipts(self, window_id: str) -> list[dict[str, Any]]:
        return [
            {"event": row["event"], "idempotency_key": row["idempotency_key"],
             "actor_id": row["actor_id"], "received_at": row["received_at"],
             "payload": json.loads(row["payload_json"])}
            for row in self.connection.execute(
                "SELECT * FROM work_order_receipts WHERE window_id=? ORDER BY receipt_id", (window_id,)
            ).fetchall()
        ]

    def operations_view(self, actor_id: str, station_id: str | None = None) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        today = self.clock.now().date().isoformat()
        where = "WHERE 1=1"
        params: list[Any] = []
        if station_id:
            where += " AND w.station_id=?"
            params.append(station_id)
        rows = self.connection.execute(
            f"SELECT w.*,d.rated_capacity_kwh FROM maintenance_windows w "
            f"JOIN devices d ON d.device_id=w.device_id {where} ORDER BY w.service_date,w.window_id",
            params,
        ).fetchall()
        windows: list[dict[str, Any]] = []
        pending_actions: list[dict[str, Any]] = []
        overdue: list[dict[str, Any]] = []
        total_extension_exposure = Decimal("0")
        for row in rows:
            wid = row["window_id"]
            receipts = self._window_receipts(wid)
            extensions = [
                {"extension_id": r["extension_id"], "previous_latest_date": r["previous_latest_date"],
                 "new_latest_date": r["new_latest_date"], "reason": r["reason"],
                 "risk_acceptor_id": r["risk_acceptor_id"], "created_at": r["created_at"]}
                for r in self.connection.execute(
                    "SELECT * FROM window_extensions WHERE window_id=? ORDER BY extension_id", (wid,)
                ).fetchall()
            ]
            invalidations = [
                {"reason": r["reason"], "detail": json.loads(r["detail_json"]),
                 "created_by": r["created_by"], "created_at": r["created_at"]}
                for r in self.connection.execute(
                    "SELECT * FROM window_invalidations WHERE window_id=? ORDER BY invalidation_id", (wid,)
                ).fetchall()
            ]
            reservations = [
                {"resource_type": r["resource_type"], "station_id": r["station_id"],
                 "resource_id": r["resource_id"], "service_date": r["service_date"],
                 "quantity": r["quantity"], "state": r["state"]}
                for r in self.connection.execute(
                    "SELECT * FROM resource_reservations WHERE window_id=? ORDER BY reservation_id", (wid,)
                ).fetchall()
            ]
            window_exposure = Decimal("0")
            for extension in extensions:
                delta = date.fromisoformat(extension["new_latest_date"]) - date.fromisoformat(
                    extension["previous_latest_date"]
                )
                window_exposure += Decimal(row["rated_capacity_kwh"]) * delta.days
            total_extension_exposure += window_exposure
            has_retest = any(item["event"] == "retest" for item in receipts)
            action = self._pending_action(row["state"], has_retest)
            item = {
                "window_id": wid,
                "plan_id": row["plan_id"],
                "device_id": row["device_id"],
                "station_id": row["station_id"],
                "service_date": row["service_date"],
                "original_date": row["original_date"],
                "latest_date": row["latest_date"],
                "state": row["state"],
                "revision": row["revision"],
                "risk_level": row["risk_level"],
                "risk_points": row["risk_points"],
                "technician_id": row["technician_id"],
                "bay_id": row["bay_id"],
                "required_spare_skus": json.loads(row["required_spare_skus_json"]),
                "required_isolation": json.loads(row["required_isolation_json"]),
                "why": json.loads(row["rationale_json"]),
                "receipts": receipts,
                "extensions": extensions,
                "invalidations": invalidations,
                "reservations": reservations,
                "extension_exposure_kwh_days": decimal_text(window_exposure),
                "pending_action": action,
            }
            windows.append(item)
            if action:
                pending_actions.append({"window_id": wid, "device_id": row["device_id"],
                                        "station_id": row["station_id"], "action": action,
                                        "service_date": row["service_date"], "latest_date": row["latest_date"]})
            if row["state"] in ACTIVE_WORK_STATES and row["latest_date"] < today:
                overdue.append({"window_id": wid, "device_id": row["device_id"],
                                "station_id": row["station_id"], "state": row["state"],
                                "latest_date": row["latest_date"], "overdue_days":
                                (date.fromisoformat(today) - date.fromisoformat(row["latest_date"])).days,
                                "rated_capacity_kwh": row["rated_capacity_kwh"]})

        station_load = self._station_daily_load(station_id)
        return {
            "as_of": today,
            "windows": windows,
            "pending_actions": pending_actions,
            "overdue_windows": overdue,
            "station_daily_load": station_load,
            "extension_exposure_total_kwh_days": decimal_text(total_extension_exposure),
        }

    @staticmethod
    def _pending_action(state: str, has_retest: bool) -> str | None:
        if state == "approved":
            return "start_work"
        if state == "in_progress":
            return "complete_work"
        if state == "paused":
            return "resume_work"
        if state == "invalidated":
            return "replan"
        if state == "completed" and not has_retest:
            return "retest"
        return None

    def _station_daily_load(self, station_id: str | None) -> list[dict[str, Any]]:
        sql = (
            "SELECT r.station_id,r.service_date,sum(CAST(r.quantity AS REAL)) outage_kwh,"
            "s.daily_outage_kwh quota_kwh FROM resource_reservations r "
            "JOIN stations s ON s.station_id=r.station_id "
            "WHERE r.resource_type='station_outage' AND r.state='held' "
        )
        params: list[Any] = []
        if station_id:
            sql += "AND r.station_id=? "
            params.append(station_id)
        sql += "GROUP BY r.station_id,r.service_date ORDER BY r.service_date,r.station_id"
        rows = self.connection.execute(sql, params).fetchall()
        result = []
        for row in rows:
            outage = Decimal(str(row["outage_kwh"]))
            quota = Decimal(row["quota_kwh"])
            result.append({
                "station_id": row["station_id"],
                "service_date": row["service_date"],
                "outage_kwh": decimal_text(outage),
                "quota_kwh": decimal_text(quota),
                "remaining_kwh": decimal_text(quota - outage),
                "within_quota": outage <= quota,
            })
        return result

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute(
            "SELECT * FROM maintenance_audit_events ORDER BY event_id"
        ).fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}
