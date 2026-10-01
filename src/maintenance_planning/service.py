"""维修计划领域用例：证据登记、风险排序、计划生成/批准、失效、现场回执与延期。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Iterable, Mapping

from .clock import SystemClock, parse_utc, utc_text
from .contracts import (
    HealthEvidence,
    MaintenanceCatalog,
    RiskRulebook,
)
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .risk import rank
from .scheduler import build_schedule
from .planning_digests import canonical_json, digest
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "planner": {
        "evidence.write", "rulebook.write", "catalog.write", "plan.create",
        "plan.generate", "plan.read", "window.reschedule",
    },
    "approver": {"plan.approve", "plan.read", "report.read"},
    "risk": {"event.write", "postponement.accept", "plan.read", "report.read", "audit.read"},
    "technician": {"receipt.write", "plan.read"},
    "auditor": {"plan.read", "report.read", "audit.read"},
}

# 已关闭：开始/暂停/复工/完工等操作类迟到回执一律拒绝，窗口不能复活。
CLOSED_STATES = {"completed", "retest_passed", "retest_failed", "cancelled"}
# 终态：连复测回执也不再接受（completed 仍允许登记一次复测）。
TERMINAL_STATES = {"retest_passed", "retest_failed", "cancelled"}


class MaintenanceService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    # ---- 账户与审计 ----------------------------------------------------

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM mp_users WHERE user_id=?", (user_id,)
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
            "SELECT event_hash FROM mp_audit_events ORDER BY event_id DESC LIMIT 1"
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
            "INSERT INTO mp_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
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

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO mp_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    # ---- 证据、规则与资源目录 ------------------------------------------

    def record_evidence(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "evidence.write")
        try:
            evidence = HealthEvidence.from_dict(raw)
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        parse_utc(evidence.observed_at, "evidence.observed_at")
        payload = evidence.as_dict()
        content_sha = digest(payload)
        previous = self.connection.execute(
            "SELECT evidence_id FROM health_evidence WHERE battery_id=? "
            "ORDER BY evidence_id DESC LIMIT 1",
            (evidence.battery_id,),
        ).fetchone()
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO health_evidence(battery_id,facility_id,evidence_revision,content_sha256,"
                    "payload_json,observed_at,supersedes_evidence_id,recorded_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        evidence.battery_id, evidence.facility_id, evidence.evidence_revision, content_sha,
                        canonical_json(payload), evidence.observed_at,
                        None if previous is None else previous["evidence_id"], actor_id, self._now(),
                    ),
                )
                evidence_id = int(cursor.lastrowid)
                affected: list[int] = []
                if previous is not None:
                    # 证据版本变化：只使引用旧证据的已批准计划中未执行窗口失效。
                    affected = self._invalidate_for_evidence(
                        evidence.battery_id, evidence_id, previous["evidence_id"]
                    )
                self._audit("evidence", str(evidence_id), "evidence.recorded", actor_id, {
                    "battery_id": evidence.battery_id,
                    "evidence_revision": evidence.evidence_revision,
                    "sha256": content_sha,
                    "invalidated_windows": affected,
                })
        except sqlite3.IntegrityError as exc:
            raise Conflict("证据版本编号或内容摘要冲突") from exc
        return {"evidence_id": evidence_id, "battery_id": evidence.battery_id,
                "evidence_revision": evidence.evidence_revision, "sha256": content_sha}

    def _invalidate_for_evidence(self, battery_id: str, new_evidence_id: int, old_evidence_id: int) -> list[int]:
        rows = self.connection.execute(
            "SELECT w.window_id FROM maintenance_windows w "
            "JOIN plan_items pi ON pi.item_id=w.item_id "
            "WHERE w.battery_id=? AND w.state='scheduled' AND pi.evidence_id=?",
            (battery_id, old_evidence_id),
        ).fetchall()
        ids = [row["window_id"] for row in rows]
        for window_id in ids:
            self.connection.execute(
                "UPDATE maintenance_windows SET state='invalidated',"
                "invalidation_reason=?,revision=revision+1 WHERE window_id=? AND state='scheduled'",
                (f"evidence:evidence:{new_evidence_id}:evidence_revision", window_id),
            )
            self.connection.execute(
                "UPDATE resource_holds SET state='released' WHERE window_id=? AND state='held'",
                (window_id,),
            )
            self.connection.execute(
                "INSERT INTO window_invalidations(window_id,trigger_kind,trigger_reference,affected_field,"
                "detail,created_at) VALUES(?,?, 'evidence','evidence_revision',?,?)",
                (window_id, f"evidence:{new_evidence_id}",
                 f"证据由 {old_evidence_id} 更新为 {new_evidence_id}", self._now()),
            )
            self._audit("window", str(window_id), "window.invalidated", "system", {
                "trigger_kind": "evidence", "trigger_reference": f"evidence:{new_evidence_id}",
                "affected_field": "evidence_revision", "battery_id": battery_id,
            })
        return ids

    def publish_rulebook(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "rulebook.write")
        try:
            rulebook = RiskRulebook.from_dict(raw)
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        text = canonical_json(raw)
        content_sha = digest(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO risk_rulebooks(rulebook_id,version,payload_json,content_sha256,active,"
                    "created_by,created_at) VALUES(?,?,?,?,1,?,?)",
                    (rulebook.rulebook_id, rulebook.version, text, content_sha, actor_id, self._now()),
                )
                self._audit("rulebook", f"{rulebook.rulebook_id}@{rulebook.version}",
                            "rulebook.published", actor_id, {"sha256": content_sha})
        except sqlite3.IntegrityError as exc:
            raise Conflict("规则版本或内容摘要已经存在") from exc
        return {"rulebook_id": rulebook.rulebook_id, "version": rulebook.version, "sha256": content_sha}

    def _load_rulebook(self, rulebook_id: str, version: int) -> RiskRulebook:
        row = self.connection.execute(
            "SELECT payload_json FROM risk_rulebooks WHERE rulebook_id=? AND version=? AND active=1",
            (rulebook_id, version),
        ).fetchone()
        if row is None:
            raise NotFound("风险规则版本不存在或未启用")
        return RiskRulebook.from_dict(json.loads(row["payload_json"]))

    def register_activity(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        try:
            activity = MaintenanceCatalog.from_dict(raw)
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO maintenance_activities(activity_id,title,payload_json,active,created_by,created_at) "
                    "VALUES(?,?,?,1,?,?)",
                    (activity.activity_id, activity.title, canonical_json(raw), actor_id, self._now()),
                )
                self._audit("activity", activity.activity_id, "activity.registered", actor_id, {})
        except sqlite3.IntegrityError as exc:
            raise Conflict("维修活动编号已经存在") from exc
        return {"activity_id": activity.activity_id}

    def assign_activity(self, actor_id: str, battery_id: str, activity_id: str) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        if self.connection.execute(
            "SELECT 1 FROM maintenance_activities WHERE activity_id=? AND active=1", (activity_id,)
        ).fetchone() is None:
            raise NotFound("维修活动不存在或未启用")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO battery_activity(battery_id,activity_id,updated_by,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(battery_id) DO UPDATE SET activity_id=excluded.activity_id,"
                "updated_by=excluded.updated_by,updated_at=excluded.updated_at",
                (battery_id, activity_id, actor_id, self._now()),
            )
            self._audit("battery", battery_id, "activity.assigned", actor_id, {"activity_id": activity_id})
        return {"battery_id": battery_id, "activity_id": activity_id}

    def register_crew(
        self, actor_id: str, crew_id: str, display_name: str, size: int, certifications: Iterable[str]
    ) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise ValidationFailed("班组人数必须是正整数")
        certs = sorted({str(item).strip() for item in certifications if str(item).strip()})
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO crews(crew_id,display_name,size,certifications_json,active,created_at) "
                    "VALUES(?,?,?,? ,1,?)",
                    (crew_id, display_name, size, canonical_json(certs), self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("班组编号已经存在") from exc
        return {"crew_id": crew_id, "certifications": certs}

    def upsert_spare_stock(self, actor_id: str, spare_kind: str, available_quantity: int) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        if not isinstance(available_quantity, int) or isinstance(available_quantity, bool) or available_quantity < 0:
            raise ValidationFailed("备件数量必须是非负整数")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO spare_inventory(spare_kind,available_quantity,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(spare_kind) DO UPDATE SET available_quantity=excluded.available_quantity,"
                "updated_at=excluded.updated_at",
                (spare_kind, available_quantity, self._now()),
            )
        return {"spare_kind": spare_kind, "available_quantity": available_quantity}

    def set_facility_quota(
        self, actor_id: str, facility_id: str, service_date_text: str, shutdown_quota_kwh: object
    ) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        try:
            day = date.fromisoformat(service_date_text)
        except ValueError as exc:
            raise ValidationFailed("服务日期必须是 YYYY-MM-DD") from exc
        quota = Decimal(str(shutdown_quota_kwh))
        if quota < 0:
            raise ValidationFailed("可停机额度不能为负")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO facility_quota(facility_id,service_date,shutdown_quota_kwh) VALUES(?,?,?) "
                "ON CONFLICT(facility_id,service_date) DO UPDATE SET shutdown_quota_kwh=excluded.shutdown_quota_kwh",
                (facility_id, day.isoformat(), format(quota, "f")),
            )
        return {"facility_id": facility_id, "service_date": day.isoformat(),
                "shutdown_quota_kwh": format(quota, "f")}

    # ---- 风险待办优先级 -----------------------------------------------

    def _latest_evidence_rows(self) -> dict[str, sqlite3.Row]:
        rows = self.connection.execute(
            "SELECT e.* FROM health_evidence e "
            "JOIN (SELECT battery_id, max(evidence_id) evidence_id FROM health_evidence GROUP BY battery_id) latest "
            "ON latest.evidence_id=e.evidence_id"
        ).fetchall()
        return {row["battery_id"]: row for row in rows}

    def risk_backlog(self, actor_id: str, rulebook_id: str, version: int) -> dict[str, Any]:
        self._require(actor_id, "plan.read")
        rulebook = self._load_rulebook(rulebook_id, version)
        evidence_rows = [
            HealthEvidence.from_dict(json.loads(row["payload_json"]))
            for row in self._latest_evidence_rows().values()
        ]
        ranked = rank(evidence_rows, rulebook)
        return {"rulebook_id": rulebook_id, "version": version, "backlog": ranked, "total": len(ranked)}

    # ---- 计划生成（草稿，可反复重算）----------------------------------

    @staticmethod
    def _date_range(start_text: str, end_text: str) -> list[str]:
        try:
            start = date.fromisoformat(start_text)
            end = date.fromisoformat(end_text)
        except ValueError as exc:
            raise ValidationFailed("计划区间必须是 YYYY-MM-DD 日期") from exc
        if end < start:
            raise ValidationFailed("计划截止日不能早于起始日")
        if (end - start).days > 180:
            raise ValidationFailed("计划区间不能超过 180 天")
        days = (start + timedelta(days=offset) for offset in range((end - start).days + 1))
        return [day.isoformat() for day in days]

    def _external_holds(self, service_dates: list[str]):
        """其它已批准且未释放的冻结占用（场站额度、班组、备件）。"""
        placeholders = ",".join("?" for _ in service_dates)
        quota_rows = self.connection.execute(
            f"SELECT resource_key,service_date,quantity FROM resource_holds "
            f"WHERE state='held' AND resource_type='facility_quota' AND service_date IN ({placeholders})",
            service_dates,
        ).fetchall()
        used_quota: dict[tuple[str, str], Decimal] = {}
        for row in quota_rows:
            used_quota[(row["resource_key"], row["service_date"])] = (
                used_quota.get((row["resource_key"], row["service_date"]), Decimal(0))
                + Decimal(row["quantity"])
            )
        crew_rows = self.connection.execute(
            f"SELECT resource_key,service_date FROM resource_holds "
            f"WHERE state='held' AND resource_type='crew' AND service_date IN ({placeholders})",
            service_dates,
        ).fetchall()
        held_crews = frozenset((row["resource_key"], row["service_date"]) for row in crew_rows)
        spare_rows = self.connection.execute(
            "SELECT spare_kind, quantity FROM resource_holds WHERE state='held' AND resource_type='spare'"
        ).fetchall()
        used_spares: dict[str, int] = {}
        for row in spare_rows:
            kind = row["spare_kind"]
            used_spares[kind] = used_spares.get(kind, 0) + int(Decimal(row["quantity"]))
        return used_quota, held_crews, used_spares

    def generate_plan(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "plan.generate")
        plan_id = str(raw.get("plan_id", "")).strip()
        title = str(raw.get("title", "")).strip()
        rulebook_id = str(raw.get("rulebook_id", "")).strip()
        version = raw.get("rulebook_version")
        if not plan_id or not title or not rulebook_id or not isinstance(version, int):
            raise ValidationFailed("plan_id、title、rulebook_id、rulebook_version 为必填")
        rulebook = self._load_rulebook(rulebook_id, int(version))
        service_dates = self._date_range(str(raw["horizon_start"]), str(raw["horizon_end"]))

        existing = self.connection.execute(
            "SELECT state,revision FROM maintenance_plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if existing is not None and existing["state"] != "draft":
            raise InvalidState("只有草稿计划可以重新生成")

        evidence_map = self._latest_evidence_rows()
        evidence_objects = [
            HealthEvidence.from_dict(json.loads(row["payload_json"]))
            for row in evidence_map.values()
        ]
        ranked = rank(evidence_objects, rulebook)
        evidence_by_battery = {
            item.battery_id: item.as_dict() for item in evidence_objects
        }

        assignment_rows = self.connection.execute(
            "SELECT battery_id,activity_id FROM battery_activity"
        ).fetchall()
        activity_rows = {
            row["activity_id"]: json.loads(row["payload_json"])
            for row in self.connection.execute(
                "SELECT activity_id,payload_json FROM maintenance_activities WHERE active=1"
            ).fetchall()
        }
        raw_activity_by_activity = {key: value.get("activity", value) for key, value in activity_rows.items()}
        activity_by_battery = {
            row["battery_id"]: raw_activity_by_activity[row["activity_id"]]
            for row in assignment_rows
            if row["activity_id"] in raw_activity_by_activity and row["battery_id"] in evidence_by_battery
        }

        crews = [
            {"crew_id": row["crew_id"], "size": row["size"],
             "certifications": json.loads(row["certifications_json"])}
            for row in self.connection.execute("SELECT * FROM crews WHERE active=1 ORDER BY crew_id").fetchall()
        ]

        quota_rows = self.connection.execute(
            "SELECT facility_id,service_date,shutdown_quota_kwh FROM facility_quota"
        ).fetchall()
        used_quota, held_crews, used_spares = self._external_holds(service_dates)
        calendars: dict[str, dict[str, Decimal]] = {}
        for row in quota_rows:
            if row["service_date"] not in service_dates:
                continue
            nominal = Decimal(row["shutdown_quota_kwh"])
            blocked = used_quota.get((row["facility_id"], row["service_date"]), Decimal(0))
            remaining = max(Decimal(0), nominal - blocked)
            calendars.setdefault(row["facility_id"], {})[row["service_date"]] = remaining

        stock = {
            row["spare_kind"]: int(row["available_quantity"])
            for row in self.connection.execute("SELECT spare_kind,available_quantity FROM spare_inventory").fetchall()
        }
        spare_availability = {
            kind: qty - used_spares.get(kind, 0) for kind, qty in stock.items()
        }
        spare_availability = {kind: qty for kind, qty in spare_availability.items() if qty > 0 or kind in stock}

        schedule = build_schedule(
            ranked=ranked,
            evidence_by_battery=evidence_by_battery,
            activity_by_battery=activity_by_battery,
            crews=crews,
            spare_availability=spare_availability,
            facility_calendars=calendars,
            service_dates=service_dates,
            crew_unavailable=held_crews,
        )
        frozen_input = {
            "rulebook": {"rulebook_id": rulebook_id, "version": int(version)},
            "horizon": [service_dates[0], service_dates[-1]],
            "evidence": {
                bid: {"evidence_id": row["evidence_id"], "content_sha256": row["content_sha256"]}
                for bid, row in sorted(evidence_map.items())
            },
            "activities": sorted({window["activity_id"] for window in schedule["windows"]}),
            "crews": sorted(crew["crew_id"] for crew in crews),
            "quota": {fid: {day: format(qty, "f") for day, qty in sorted(days.items())}
                      for fid, days in sorted(calendars.items())},
            "spares": dict(sorted(stock.items())),
        }
        input_sha = digest(frozen_input)
        frozen_text = canonical_json(frozen_input)
        now = self._now()
        try:
            with transaction(self.connection, immediate=True):
                if existing is None:
                    self.connection.execute(
                        "INSERT INTO maintenance_plans(plan_id,title,state,rulebook_id,rulebook_version,"
                        "horizon_start,horizon_end,input_sha256,frozen_input_json,ranking_json,schedule_json,"
                        "revision,created_by,created_at) VALUES(?,?,'draft',?,?,?,?,?,?,?,?,1,?,?)",
                        (plan_id, title, rulebook_id, int(version), service_dates[0], service_dates[-1],
                         input_sha, frozen_text, canonical_json(ranked), canonical_json(schedule), actor_id, now),
                    )
                else:
                    self.connection.execute(
                        "UPDATE maintenance_plans SET title=?,input_sha256=?,frozen_input_json=?,"
                        "ranking_json=?,schedule_json=?,revision=revision+1 "
                        "WHERE plan_id=? AND state='draft'",
                        (title, input_sha, frozen_text, canonical_json(ranked), canonical_json(schedule), plan_id),
                    )
                self._audit("plan", plan_id, "plan.generated", actor_id,
                            {"windows": len(schedule["windows"]),
                             "unscheduled": len(schedule["unscheduled"]),
                             "input_sha256": input_sha})
        except sqlite3.IntegrityError as exc:
            raise Conflict("计划生成发生并发冲突") from exc
        return {
            "plan_id": plan_id,
            "state": "draft",
            "input_sha256": input_sha,
            "windows": schedule["windows"],
            "unscheduled": schedule["unscheduled"],
            "arbitrations": schedule["arbitrations"],
            "facility_usage": schedule["facility_usage"],
        }

    def get_plan(self, actor_id: str, plan_id: str) -> dict[str, Any]:
        self._require(actor_id, "plan.read")
        row = self.connection.execute(
            "SELECT * FROM maintenance_plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if row is None:
            raise NotFound("维修计划不存在")
        return {
            "plan_id": plan_id,
            "title": row["title"],
            "state": row["state"],
            "revision": row["revision"],
            "rulebook_id": row["rulebook_id"],
            "rulebook_version": row["rulebook_version"],
            "horizon": [row["horizon_start"], row["horizon_end"]],
            "input_sha256": row["input_sha256"],
            "ranking": json.loads(row["ranking_json"]) if row["ranking_json"] else [],
            "schedule": json.loads(row["schedule_json"]) if row["schedule_json"] else {"windows": []},
            "approved_by": row["approved_by"],
            "approved_at": row["approved_at"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
        }

    # ---- 批准：冻结输入与资源占用、物化窗口 ----------------------------

    def approve_plan(self, actor_id: str, plan_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "plan.approve")
        row = self.connection.execute(
            "SELECT * FROM maintenance_plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if row is None:
            raise NotFound("维修计划不存在")
        if row["state"] != "draft" or row["revision"] != expected_revision:
            raise InvalidState("计划不是当前草稿版本")
        schedule = json.loads(row["schedule_json"])
        ranking = json.loads(row["ranking_json"])
        frozen = json.loads(row["frozen_input_json"])
        evidence_id_by_battery = {
            r["battery_id"]: r["evidence_id"]
            for r in self.connection.execute("SELECT evidence_id,battery_id FROM health_evidence").fetchall()
        }
        now = self._now()
        with transaction(self.connection, immediate=True):
            # 并发保护：冻结的证据快照必须仍是各电池的最新证据。
            for bid, snapshot in frozen["evidence"].items():
                latest = self.connection.execute(
                    "SELECT content_sha256 FROM health_evidence WHERE battery_id=? ORDER BY evidence_id DESC LIMIT 1",
                    (bid,),
                ).fetchone()
                if latest is None or latest["content_sha256"] != snapshot["content_sha256"]:
                    raise InvalidState(f"电池 {bid} 的证据已变化，请重新生成计划")
            # 资源占用也必须未被并发批准的其它计划抢先占满。
            for window in schedule["windows"]:
                held = self.connection.execute(
                    "SELECT COALESCE(sum(CAST(quantity AS REAL)),0) held FROM resource_holds "
                    "WHERE state='held' AND resource_type='facility_quota' AND resource_key=? AND service_date=?",
                    (window["facility_id"], window["service_date"]),
                ).fetchone()["held"]
                quota_nominal = self.connection.execute(
                    "SELECT shutdown_quota_kwh FROM facility_quota WHERE facility_id=? AND service_date=?",
                    (window["facility_id"], window["service_date"]),
                ).fetchone()
                if quota_nominal is None or Decimal(quota_nominal["shutdown_quota_kwh"]) - Decimal(str(held)) \
                        < Decimal(window["energy_offline_kwh"]):
                    raise InvalidState(
                        f"场站 {window['facility_id']} 在 {window['service_date']} 的停机额度已被占满，请重新生成"
                    )
                busy = self.connection.execute(
                    "SELECT 1 FROM resource_holds WHERE state='held' AND resource_type='crew' "
                    "AND resource_key=? AND service_date=? LIMIT 1",
                    (window["crew_id"], window["service_date"]),
                ).fetchone()
                if busy is not None:
                    raise InvalidState(f"班组 {window['crew_id']} 在 {window['service_date']} 已被其它计划冻结")
            cursor = self.connection.execute(
                "UPDATE maintenance_plans SET state='approved',revision=revision+1,approved_by=?,approved_at=? "
                "WHERE plan_id=? AND state='draft' AND revision=?",
                (actor_id, now, plan_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("计划状态或版本已变化")
            for item in ranking:
                self.connection.execute(
                    "INSERT INTO plan_items(plan_id,battery_id,facility_id,evidence_id,activity_id,"
                    "risk_score,risk_band,ranking,rationale_json) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        plan_id, item["battery_id"], item["facility_id"],
                        evidence_id_by_battery.get(item["battery_id"]),
                        next((w["activity_id"] for w in schedule["windows"]
                              if w["battery_id"] == item["battery_id"]), None),
                        item["risk_score"], item["risk_band"], item["ranking"],
                        canonical_json(item),
                    ),
                )
            for window in schedule["windows"]:
                item_id = self.connection.execute(
                    "SELECT item_id FROM plan_items WHERE plan_id=? AND battery_id=?",
                    (plan_id, window["battery_id"]),
                ).fetchone()["item_id"]
                wc = self.connection.execute(
                    "INSERT INTO maintenance_windows(plan_id,item_id,battery_id,facility_id,activity_id,"
                    "service_date,energy_offline_kwh,crew_id,spares_json,isolation_required,estimated_hours,"
                    "selection_reasons_json,state,scheduled_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'scheduled',?)",
                    (
                        plan_id, item_id,
                        window["battery_id"], window["facility_id"], window["activity_id"],
                        window["service_date"], window["energy_offline_kwh"], window["crew_id"],
                        canonical_json(window["spares"]), 1 if window["isolation_required"] else 0,
                        window["estimated_hours"], canonical_json(window["selection_reasons"]),
                        now,
                    ),
                )
                window_id = int(wc.lastrowid)
                self._write_holds(plan_id, window_id, window, now)
            self._audit("plan", plan_id, "plan.approved", actor_id, {
                "revision": expected_revision + 1,
                "windows": len(schedule["windows"]),
                "unscheduled": len(schedule.get("unscheduled", [])),
                "input_sha256": row["input_sha256"],
            })
        return {"plan_id": plan_id, "state": "approved", "revision": expected_revision + 1,
                "windows": len(schedule["windows"])}

    def _write_holds(self, plan_id: str, window_id: int, window: Mapping[str, Any], now: str) -> None:
        self.connection.execute(
            "INSERT INTO resource_holds(plan_id,window_id,resource_type,resource_key,service_date,quantity,state,created_at) "
            "VALUES(?,?, 'facility_quota',?,?,?, 'held',?)",
            (plan_id, window_id, window["facility_id"], window["service_date"],
             window["energy_offline_kwh"], now),
        )
        self.connection.execute(
            "INSERT INTO resource_holds(plan_id,window_id,resource_type,resource_key,service_date,quantity,state,created_at) "
            "VALUES(?,?, 'crew',?,?, 1, 'held',?)",
            (plan_id, window_id, window["crew_id"], window["service_date"], now),
        )
        for spare in window["spares"]:
            self.connection.execute(
                "INSERT INTO resource_holds(plan_id,window_id,resource_type,resource_key,service_date,"
                "spare_kind,quantity,state,created_at) VALUES(?,?, 'spare',?,?,?,?, 'held',?)",
                (plan_id, window_id, spare["spare_kind"], window["service_date"],
                 spare["spare_kind"], spare["quantity"], now),
            )

    # ---- 证据变化 / 紧急告警 / 召回升级：只失效受影响未执行窗口 --------

    def record_health_event(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "event.write")
        battery_id = str(raw.get("battery_id", "")).strip()
        kind = str(raw.get("kind", "")).strip()
        severity = str(raw.get("severity", "")).strip()
        if kind not in {"alert", "recall"}:
            raise ValidationFailed("kind 必须是 alert 或 recall")
        allowed = {"alert": {"warning", "critical"}, "recall": {"advisory", "restricted", "mandatory"}}[kind]
        if severity not in allowed:
            raise ValidationFailed(f"{kind} 事件的 severity 必须是 {'、'.join(sorted(allowed))}")
        reference = str(raw.get("reference", f"{kind}-{battery_id}")).strip()
        observed_at = str(raw.get("observed_at", self._now())).strip()
        parse_utc(observed_at, "observed_at")
        # 紧急告警（critical）与任何召回升级都使受影响的未执行窗口失效；
        # 一般告警只登记观察，不冲击已批准排期。
        should_invalidate = kind == "recall" or severity == "critical"
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO health_events(battery_id,kind,severity,reference,payload_json,observed_at,"
                "recorded_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (battery_id, kind, severity, reference, canonical_json(raw), observed_at, actor_id, self._now()),
            )
            event_id = int(cursor.lastrowid)
            affected: list[int] = []
            if should_invalidate:
                affected = self._invalidate_for_battery(
                    battery_id, "health_event", reference,
                    affected_field={"alert": "urgent_alert", "recall": "recall_upgrade"}[kind],
                    detail=f"{kind} 升级为 {severity}",
                )
            self._audit("health_event", str(event_id), f"{kind}.recorded", actor_id, {
                "battery_id": battery_id, "severity": severity, "invalidated_windows": affected,
            })
        return {"event_id": event_id, "battery_id": battery_id, "kind": kind,
                "severity": severity, "invalidated_windows": affected}

    def _invalidate_for_battery(
        self, battery_id: str, trigger_kind: str, trigger_reference: str,
        affected_field: str, detail: str,
    ) -> list[int]:
        """把受影响电池仍未执行的窗口置为失效并释放占用；已开始/已关闭窗口不动。"""
        rows = self.connection.execute(
            "SELECT window_id FROM maintenance_windows WHERE battery_id=? AND state='scheduled'",
            (battery_id,),
        ).fetchall()
        window_ids = [row["window_id"] for row in rows]
        for window_id in window_ids:
            self.connection.execute(
                "UPDATE maintenance_windows SET state='invalidated',invalidation_reason=?,revision=revision+1 "
                "WHERE window_id=? AND state='scheduled'",
                (f"{trigger_kind}:{trigger_reference}:{affected_field}", window_id),
            )
            self.connection.execute(
                "UPDATE resource_holds SET state='released' WHERE window_id=? AND state='held'",
                (window_id,),
            )
            self.connection.execute(
                "INSERT INTO window_invalidations(window_id,trigger_kind,trigger_reference,affected_field,detail,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (window_id, trigger_kind, trigger_reference, affected_field, detail, self._now()),
            )
            self._audit("window", str(window_id), "window.invalidated", "system", {
                "trigger_kind": trigger_kind, "trigger_reference": trigger_reference,
                "affected_field": affected_field, "battery_id": battery_id,
            })
        return window_ids

    def refresh_evidence_invalidations(self, actor_id: str | None = None) -> dict[str, Any]:
        """对账扫描：证据登记已在事务内自动失效受影响窗口。

        该用例用于数据补录或历史库恢复后的重新核对，按计划冻结快照找出
        证据已变化却仍为 scheduled 的窗口并失效，结果幂等。
        """
        if actor_id is not None:
            self._require(actor_id, "event.write")
        plans = self.connection.execute(
            "SELECT * FROM maintenance_plans WHERE state='approved'"
        ).fetchall()
        total_affected: list[int] = []
        with transaction(self.connection, immediate=True):
            for plan in plans:
                windows = self.connection.execute(
                    "SELECT window_id,battery_id FROM maintenance_windows "
                    "WHERE plan_id=? AND state='scheduled'", (plan["plan_id"],),
                ).fetchall()
                for window in windows:
                    item = self.connection.execute(
                        "SELECT evidence_id FROM plan_items WHERE plan_id=? AND battery_id=?",
                        (plan["plan_id"], window["battery_id"]),
                    ).fetchone()
                    if item is None or item["evidence_id"] is None:
                        continue
                    frozen = self.connection.execute(
                        "SELECT content_sha256 FROM health_evidence WHERE evidence_id=?",
                        (item["evidence_id"],),
                    ).fetchone()
                    latest = self.connection.execute(
                        "SELECT evidence_id,content_sha256 FROM health_evidence "
                        "WHERE battery_id=? ORDER BY evidence_id DESC LIMIT 1",
                        (window["battery_id"],),
                    ).fetchone()
                    if latest is not None and frozen is not None and latest["content_sha256"] != frozen["content_sha256"]:
                        ids = self._invalidate_for_battery(
                            window["battery_id"], "evidence",
                            f"evidence:{latest['evidence_id']}",
                            affected_field="evidence_revision",
                            detail=f"证据由 {item['evidence_id']} 更新为 {latest['evidence_id']}",
                        )
                        total_affected.extend(ids)
        return {"invalidated_windows": sorted(set(total_affected))}

    # ---- 现场回执：幂等、可乱序，关闭窗口永不复活 ----------------------

    def record_receipt(
        self,
        actor_id: str,
        window_id: int,
        receipt_type: str,
        client_receipt_key: str,
        raw: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._require(actor_id, "receipt.write")
        if receipt_type not in {"start", "pause", "resume", "complete", "retest"}:
            raise ValidationFailed("回执类型必须是 start、pause、resume、complete 或 retest")
        if not client_receipt_key.strip():
            raise ValidationFailed("client_receipt_key 不能为空")
        payload = dict(raw or {})
        observed_at = str(payload.pop("observed_at", self._now())).strip()
        parse_utc(observed_at, "observed_at")
        window = self.connection.execute(
            "SELECT * FROM maintenance_windows WHERE window_id=?", (window_id,)
        ).fetchone()
        if window is None:
            raise NotFound("维修窗口不存在")

        duplicate = self.connection.execute(
            "SELECT * FROM window_receipts WHERE window_id=? AND receipt_type=? AND client_receipt_key=?",
            (window_id, receipt_type, client_receipt_key),
        ).fetchone()
        if duplicate is not None:
            # 重复投递：原样返回首次裁决，绝不二次改变窗口状态。
            return {"window_id": window_id, "receipt_type": receipt_type,
                    "state": window["state"], "duplicate": True,
                    "accepted": bool(duplicate["accepted"]),
                    "reject_reason": duplicate["reject_reason"]}

        accepted = False
        reject_reason = None
        new_state = window["state"]
        stamps: dict[str, str | None] = {}
        current = window["state"]
        if receipt_type == "start":
            if current in CLOSED_STATES:
                reject_reason = f"窗口已关闭（{current}），迟到的开始回执被拒绝"
            elif current == "invalidated":
                reject_reason = "窗口已失效，需重新排期后才能开工"
            elif current in {"paused", "in_progress"}:
                reject_reason = f"窗口已处于 {current}，开始回执被忽略"
            else:
                accepted, new_state = True, "in_progress"
                stamps["started_at"] = observed_at
        elif receipt_type == "pause":
            if current in CLOSED_STATES:
                reject_reason = f"窗口已关闭（{current}），迟到的暂停回执被拒绝"
            elif current == "invalidated":
                reject_reason = "窗口已失效，不能暂停"
            elif current == "scheduled":
                reject_reason = "窗口尚未开始，不能暂停"
            elif current == "paused":
                reject_reason = "窗口已经暂停，重复暂停被忽略"
            else:
                accepted, new_state = True, "paused"
        elif receipt_type == "resume":
            if current in CLOSED_STATES:
                reject_reason = f"窗口已关闭（{current}），迟到的复工回执被拒绝"
            elif current == "invalidated":
                reject_reason = "窗口已失效，需重新排期"
            elif current == "scheduled":
                reject_reason = "窗口尚未开始，应发送开始回执"
            elif current == "in_progress":
                reject_reason = "窗口正在执行，复工回执被忽略"
            else:
                accepted, new_state = True, "in_progress"
        elif receipt_type == "complete":
            if current in CLOSED_STATES:
                reject_reason = f"窗口已关闭（{current}），迟到的完工回执被拒绝"
            elif current == "invalidated":
                reject_reason = "窗口已失效，不能完工"
            elif current == "scheduled":
                reject_reason = "窗口尚未开始，不能完工"
            else:
                accepted, new_state = True, "completed"
                stamps["completed_at"] = observed_at
        else:  # retest
            passed = bool(payload.get("passed", False))
            if current in TERMINAL_STATES:
                reject_reason = f"窗口已终态关闭（{current}），迟到的复测回执被拒绝"
            elif current == "completed":
                accepted = True
                new_state = "retest_passed" if passed else "retest_failed"
            else:
                reject_reason = f"窗口当前为 {current}，只有完工后可登记复测"

        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO window_receipts(window_id,receipt_type,client_receipt_key,payload_json,observed_at,"
                "recorded_by,recorded_at,accepted,reject_reason) VALUES(?,?,?,?,?,?,?,?,?)",
                (window_id, receipt_type, client_receipt_key, canonical_json(payload), observed_at,
                 actor_id, self._now(), 1 if accepted else 0, reject_reason),
            )
            if accepted:
                assignments = ", ".join(
                    f"{key}=?" for key in stamps
                )
                self.connection.execute(
                    f"UPDATE maintenance_windows SET state=?,revision=revision+1"
                    f"{', ' + assignments if assignments else ''} WHERE window_id=?",
                    (new_state, *stamps.values(), window_id),
                )
                if new_state == "completed":
                    self.connection.execute(
                        "UPDATE resource_holds SET state='released' WHERE window_id=? AND state='held'",
                        (window_id,),
                    )
                self._audit("window", str(window_id), f"receipt.{receipt_type}", actor_id,
                            {"accepted": True, "new_state": new_state, "key": client_receipt_key})
            else:
                self._audit("window", str(window_id), f"receipt.{receipt_type}", actor_id,
                            {"accepted": False, "reason": reject_reason, "key": client_receipt_key})
        return {"window_id": window_id, "receipt_type": receipt_type, "accepted": accepted,
                "state": new_state if accepted else current, "duplicate": False,
                "reject_reason": reject_reason}

    # ---- 延期：风险接受人与新的最迟日期 --------------------------------

    def request_postponement(
        self,
        actor_id: str,
        window_id: int,
        requested_date_text: str,
        new_latest_date_text: str,
        reason: str,
    ) -> dict[str, Any]:
        """计划员申请延期；窗口日期暂不移动，等待风险负责人接受容量风险。"""
        self._require(actor_id, "plan.read")
        window = self.connection.execute(
            "SELECT * FROM maintenance_windows WHERE window_id=?", (window_id,)
        ).fetchone()
        if window is None:
            raise NotFound("维修窗口不存在")
        if window["state"] != "scheduled":
            raise InvalidState("只有尚未开工的已排期窗口可以申请延期")
        try:
            requested_date = date.fromisoformat(requested_date_text)
            new_latest = date.fromisoformat(new_latest_date_text)
        except ValueError as exc:
            raise ValidationFailed("日期必须是 YYYY-MM-DD") from exc
        if requested_date <= date.fromisoformat(window["service_date"]):
            raise ValidationFailed("延期日期必须晚于当前排期")
        if new_latest < requested_date:
            raise ValidationFailed("新的最迟日期不能早于计划执行日期")
        if not reason.strip():
            raise ValidationFailed("延期原因不能为空")
        pending = self.connection.execute(
            "SELECT 1 FROM window_postponements WHERE window_id=? AND state='pending' LIMIT 1",
            (window_id,),
        ).fetchone()
        if pending is not None:
            raise Conflict("该窗口已有待接受的延期申请")
        # 资源可行性预检：新日期的场站额度、班组必须仍可用（此时本窗口占用尚在旧日期）。
        quota_row = self.connection.execute(
            "SELECT COALESCE(sum(CAST(quantity AS REAL)),0) held FROM resource_holds "
            "WHERE state='held' AND resource_type='facility_quota' AND resource_key=? AND service_date=?",
            (window["facility_id"], requested_date.isoformat()),
        ).fetchone()
        quota_nominal = self.connection.execute(
            "SELECT shutdown_quota_kwh FROM facility_quota WHERE facility_id=? AND service_date=?",
            (window["facility_id"], requested_date.isoformat()),
        ).fetchone()
        if quota_nominal is None:
            raise InvalidState("新日期未开放场站停机额度")
        offline = Decimal(window["energy_offline_kwh"])
        if Decimal(quota_nominal["shutdown_quota_kwh"]) - Decimal(str(quota_row["held"])) < offline:
            raise Conflict("新日期场站可停机额度不足")
        crew_busy = self.connection.execute(
            "SELECT 1 FROM resource_holds WHERE state='held' AND resource_type='crew' "
            "AND resource_key=? AND service_date=? LIMIT 1",
            (window["crew_id"], requested_date.isoformat()),
        ).fetchone()
        if crew_busy is not None:
            raise Conflict("班组在新日期已有冻结任务")
        with transaction(self.connection, immediate=True):
            pc = self.connection.execute(
                "INSERT INTO window_postponements(window_id,previous_date,requested_date,new_date,"
                "new_latest_date,requested_by,capacity_risk_kwh,reason,state,created_at) "
                "VALUES(?,?,?,NULL,?,?,?,?,'pending',?)",
                (window_id, window["service_date"], requested_date.isoformat(),
                 new_latest.isoformat(), actor_id, format(offline, "f"), reason.strip(), self._now()),
            )
            postponement_id = int(pc.lastrowid)
            self._audit("window", str(window_id), "postponement.requested", actor_id, {
                "postponement_id": postponement_id,
                "previous_date": window["service_date"],
                "requested_date": requested_date.isoformat(),
                "new_latest_date": new_latest.isoformat(),
                "exposed_capacity_kwh": format(offline, "f"),
                "reason": reason.strip(),
            })
        return {
            "postponement_id": postponement_id, "window_id": window_id,
            "state": "pending", "requested_date": requested_date.isoformat(),
            "new_latest_date": new_latest.isoformat(),
            "exposed_capacity_kwh": format(offline, "f"),
        }

    def resolve_postponement(
        self, actor_id: str, postponement_id: int, approve: bool, note: str
    ) -> dict[str, Any]:
        """风险负责人接受或拒绝延期；接受即记录风险接受人、移动窗口日期与冻结占用。"""
        self._require(actor_id, "postponement.accept")
        row = self.connection.execute(
            "SELECT * FROM window_postponements WHERE postponement_id=?", (postponement_id,)
        ).fetchone()
        if row is None:
            raise NotFound("延期记录不存在")
        if row["state"] != "pending":
            raise InvalidState("延期申请已经处理")
        window_id = row["window_id"]
        if not approve:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "UPDATE window_postponements SET state='rejected' WHERE postponement_id=? AND state='pending'",
                    (postponement_id,),
                )
                self._audit("postponement", str(postponement_id), "postponement.rejected", actor_id, {
                    "window_id": window_id, "note": note,
                })
            return {"postponement_id": postponement_id, "state": "rejected"}
        window = self.connection.execute(
            "SELECT * FROM maintenance_windows WHERE window_id=?", (window_id,)
        ).fetchone()
        if window is None:
            raise NotFound("维修窗口不存在")
        if window["state"] != "scheduled":
            raise InvalidState("窗口已离开待执行状态，不能接受延期")
        new_date = row["requested_date"]
        # 接受时再次校验资源，防止申请后被其它计划占用。
        held = self.connection.execute(
            "SELECT COALESCE(sum(CAST(quantity AS REAL)),0) held FROM resource_holds "
            "WHERE state='held' AND resource_type='facility_quota' AND resource_key=? AND service_date=?",
            (window["facility_id"], new_date),
        ).fetchone()["held"]
        quota_nominal = self.connection.execute(
            "SELECT shutdown_quota_kwh FROM facility_quota WHERE facility_id=? AND service_date=?",
            (window["facility_id"], new_date),
        ).fetchone()
        if quota_nominal is None or Decimal(quota_nominal["shutdown_quota_kwh"]) - Decimal(str(held)) \
                < Decimal(window["energy_offline_kwh"]):
            raise Conflict("新日期场站可停机额度已被占用，无法接受延期")
        crew_busy = self.connection.execute(
            "SELECT 1 FROM resource_holds WHERE state='held' AND resource_type='crew' "
            "AND resource_key=? AND service_date=? AND window_id<>? LIMIT 1",
            (window["crew_id"], new_date, window_id),
        ).fetchone()
        if crew_busy is not None:
            raise Conflict("班组在新日期已有冻结任务")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE window_postponements SET state='accepted',new_date=?,risk_accepter_id=?,"
                "accepted_at=? WHERE postponement_id=? AND state='pending'",
                (new_date, actor_id, self._now(), postponement_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("延期申请状态已变化")
            self.connection.execute(
                "UPDATE maintenance_windows SET service_date=?,revision=revision+1 "
                "WHERE window_id=? AND state='scheduled'",
                (new_date, window_id),
            )
            self.connection.execute(
                "UPDATE resource_holds SET service_date=? WHERE window_id=? AND state='held'",
                (new_date, window_id),
            )
            self._audit("window", str(window_id), "window.postponed", actor_id, {
                "postponement_id": postponement_id,
                "previous_date": row["previous_date"], "new_date": new_date,
                "new_latest_date": row["new_latest_date"],
                "risk_accepter_id": actor_id,
                "capacity_risk_kwh": row["capacity_risk_kwh"],
                "reason": row["reason"], "note": note,
            })
        return {
            "postponement_id": postponement_id, "state": "accepted",
            "window_id": window_id, "service_date": new_date,
            "new_latest_date": row["new_latest_date"],
            "risk_accepter_id": actor_id,
            "capacity_risk_kwh": row["capacity_risk_kwh"],
        }

    # ---- 运营接口：窗口解释、冲突裁决、延期风险、重启后待办 --------------

    def window_detail(self, actor_id: str, window_id: int) -> dict[str, Any]:
        self._require(actor_id, "plan.read")
        window = self.connection.execute(
            "SELECT * FROM maintenance_windows WHERE window_id=?", (window_id,)
        ).fetchone()
        if window is None:
            raise NotFound("维修窗口不存在")
        invalidations = self.connection.execute(
            "SELECT trigger_kind,trigger_reference,affected_field,detail,created_at "
            "FROM window_invalidations WHERE window_id=? ORDER BY invalidation_id",
            (window_id,),
        ).fetchall()
        postponements = self.connection.execute(
            "SELECT * FROM window_postponements WHERE window_id=? ORDER BY postponement_id",
            (window_id,),
        ).fetchall()
        receipts = self.connection.execute(
            "SELECT receipt_type,client_receipt_key,accepted,reject_reason,observed_at,recorded_at "
            "FROM window_receipts WHERE window_id=? ORDER BY receipt_id",
            (window_id,),
        ).fetchall()
        return {
            "window_id": window_id,
            "plan_id": window["plan_id"],
            "battery_id": window["battery_id"],
            "facility_id": window["facility_id"],
            "activity_id": window["activity_id"],
            "service_date": window["service_date"],
            "energy_offline_kwh": window["energy_offline_kwh"],
            "crew_id": window["crew_id"],
            "spares": json.loads(window["spares_json"]),
            "isolation_required": bool(window["isolation_required"]),
            "estimated_hours": window["estimated_hours"],
            "state": window["state"],
            "revision": window["revision"],
            "selection_reasons": json.loads(window["selection_reasons_json"]),
            "invalidation": None if not invalidations else [dict(row) for row in invalidations][-1],
            "postponements": [
                {
                    "postponement_id": row["postponement_id"],
                    "previous_date": row["previous_date"],
                    "requested_date": row["requested_date"],
                    "new_date": row["new_date"],
                    "new_latest_date": row["new_latest_date"],
                    "requested_by": row["requested_by"],
                    "risk_accepter_id": row["risk_accepter_id"],
                    "state": row["state"],
                    "capacity_risk_kwh": row["capacity_risk_kwh"],
                    "reason": row["reason"],
                    "created_at": row["created_at"],
                }
                for row in postponements
            ],
            "receipts": [dict(row) for row in receipts],
            "started_at": window["started_at"],
            "completed_at": window["completed_at"],
        }

    def operations_dashboard(self, actor_id: str, plan_id: str | None = None) -> dict[str, Any]:
        self._user(actor_id)  # 全部已认证角色均可读运营接口。
        plan_filter = ""
        params: list[Any] = []
        if plan_id:
            plan_filter = "AND w.plan_id=?"
            params.append(plan_id)
        windows = self.connection.execute(
            f"SELECT w.*,p.title AS plan_title FROM maintenance_windows w "
            f"JOIN maintenance_plans p ON p.plan_id=w.plan_id WHERE 1=1 {plan_filter} "
            f"ORDER BY w.service_date,w.window_id",
            params,
        ).fetchall()
        by_state: dict[str, int] = {}
        pending_actions: list[dict[str, Any]] = []
        postponed_risk: list[dict[str, Any]] = []
        for window in windows:
            by_state[window["state"]] = by_state.get(window["state"], 0) + 1
            latest_post = self.connection.execute(
                "SELECT * FROM window_postponements WHERE window_id=? ORDER BY postponement_id DESC LIMIT 1",
                (window["window_id"],),
            ).fetchone()
            if latest_post is not None and latest_post["state"] in {"pending", "accepted"} \
                    and window["state"] not in CLOSED_STATES:
                postponed_risk.append({
                    "window_id": window["window_id"],
                    "battery_id": window["battery_id"],
                    "facility_id": window["facility_id"],
                    "service_date": window["service_date"],
                    "state": latest_post["state"],
                    "requested_date": latest_post["requested_date"],
                    "new_latest_date": latest_post["new_latest_date"],
                    "risk_accepter_id": latest_post["risk_accepter_id"],
                    "exposed_capacity_kwh": latest_post["capacity_risk_kwh"],
                    "reason": latest_post["reason"],
                })
            if window["state"] in {"scheduled", "paused"}:
                action = "awaiting_start" if window["state"] == "scheduled" else "awaiting_resume"
                pending_actions.append({
                    "window_id": window["window_id"],
                    "plan_id": window["plan_id"],
                    "battery_id": window["battery_id"],
                    "service_date": window["service_date"],
                    "crew_id": window["crew_id"],
                    "action": action,
                    "new_latest_date": None if latest_post is None else latest_post["new_latest_date"],
                })
            if window["state"] == "completed":
                pending_actions.append({
                    "window_id": window["window_id"], "plan_id": window["plan_id"],
                    "battery_id": window["battery_id"], "service_date": window["service_date"],
                    "crew_id": window["crew_id"], "action": "awaiting_retest",
                    "new_latest_date": None,
                })
            if window["state"] == "retest_failed":
                pending_actions.append({
                    "window_id": window["window_id"], "plan_id": window["plan_id"],
                    "battery_id": window["battery_id"], "service_date": window["service_date"],
                    "crew_id": window["crew_id"], "action": "requiring_rework",
                    "new_latest_date": None,
                })
            if window["state"] == "invalidated":
                invalidation = self.connection.execute(
                    "SELECT affected_field,detail,created_at FROM window_invalidations "
                    "WHERE window_id=? ORDER BY invalidation_id DESC LIMIT 1", (window["window_id"],),
                ).fetchone()
                pending_actions.append({
                    "window_id": window["window_id"], "plan_id": window["plan_id"],
                    "battery_id": window["battery_id"], "service_date": window["service_date"],
                    "crew_id": window["crew_id"], "action": "requiring_reschedule",
                    "reason": None if invalidation is None else invalidation["detail"],
                    "new_latest_date": None,
                })
        arbitrations: list[dict[str, Any]] = []
        approved_plans = self.connection.execute(
            "SELECT plan_id,schedule_json FROM maintenance_plans WHERE state='approved'"
            + (" AND plan_id=?" if plan_id else ""),
            ([plan_id] if plan_id else []),
        ).fetchall()
        for plan in approved_plans:
            schedule = json.loads(plan["schedule_json"])
            for item in schedule.get("arbitrations", []):
                arbitrations.append({"plan_id": plan["plan_id"], **item})
        # 未排期高风险设备（各计划最近版本）。
        unscheduled: list[dict[str, Any]] = []
        for plan in approved_plans:
            for item in json.loads(plan["schedule_json"]).get("unscheduled", []):
                unscheduled.append({"plan_id": plan["plan_id"], **item})
        return {
            "window_counts": by_state,
            "pending_actions": pending_actions,
            "postponed_capacity_risk": postponed_risk,
            "conflict_arbitrations": arbitrations,
            "unscheduled_backlog": unscheduled,
        }

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute("SELECT * FROM mp_audit_events ORDER BY event_id").fetchall()
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
