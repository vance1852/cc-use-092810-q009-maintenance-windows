"""维修计划服务的 SQLite 模式和事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS mp_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('planner','approver','risk','technician','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

-- 确定版本的健康证据：同一证据版本编号只接受同一内容摘要，形成不可变快照。
CREATE TABLE IF NOT EXISTS health_evidence (
    evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
    battery_id TEXT NOT NULL,
    facility_id TEXT NOT NULL,
    evidence_revision TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    supersedes_evidence_id INTEGER,
    recorded_by TEXT NOT NULL REFERENCES mp_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(battery_id, evidence_revision),
    UNIQUE(content_sha256)
);

CREATE INDEX IF NOT EXISTS idx_evidence_battery ON health_evidence(battery_id, evidence_id);

-- 风险规则版本。
CREATE TABLE IF NOT EXISTS risk_rulebooks (
    rulebook_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version > 0),
    payload_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_by TEXT NOT NULL REFERENCES mp_users(user_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(rulebook_id, version),
    UNIQUE(content_sha256)
);

-- 紧急告警与召回升级事件，用来使受影响的未执行窗口失效。
CREATE TABLE IF NOT EXISTS health_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    battery_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('alert','recall','evidence')),
    severity TEXT NOT NULL CHECK(severity IN ('info','warning','critical','advisory','restricted','mandatory')),
    reference TEXT,
    payload_json TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    recorded_by TEXT NOT NULL REFERENCES mp_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_health_events_battery ON health_events(battery_id, event_id);

-- 维修活动目录。
CREATE TABLE IF NOT EXISTS maintenance_activities (
    activity_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_by TEXT NOT NULL REFERENCES mp_users(user_id),
    created_at TEXT NOT NULL
);

-- 可派工班组。
CREATE TABLE IF NOT EXISTS crews (
    crew_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    size INTEGER NOT NULL CHECK(size > 0),
    certifications_json TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

-- 备件库存（以种类计的可用件数）。
CREATE TABLE IF NOT EXISTS spare_inventory (
    spare_kind TEXT PRIMARY KEY,
    available_quantity INTEGER NOT NULL CHECK(available_quantity >= 0),
    updated_at TEXT NOT NULL
);

-- 场站每日可停机额度（kWh）：只登记开放日期。
CREATE TABLE IF NOT EXISTS facility_quota (
    facility_id TEXT NOT NULL,
    service_date TEXT NOT NULL,
    shutdown_quota_kwh TEXT NOT NULL,
    PRIMARY KEY(facility_id, service_date)
);

-- 每套电池当前拟执行的维修活动（生成计划时读取）。
CREATE TABLE IF NOT EXISTS battery_activity (
    battery_id TEXT PRIMARY KEY,
    activity_id TEXT NOT NULL REFERENCES maintenance_activities(activity_id),
    updated_by TEXT NOT NULL REFERENCES mp_users(user_id),
    updated_at TEXT NOT NULL
);

-- 维修计划：草稿可重算，批准后冻结输入摘要与占用。
CREATE TABLE IF NOT EXISTS maintenance_plans (
    plan_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'draft'
        CHECK(state IN ('draft','approved','superseded','closed')),
    rulebook_id TEXT NOT NULL,
    rulebook_version INTEGER NOT NULL,
    horizon_start TEXT NOT NULL,
    horizon_end TEXT NOT NULL,
    input_sha256 TEXT,
    frozen_input_json TEXT,
    ranking_json TEXT,
    schedule_json TEXT,
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES mp_users(user_id),
    approved_by TEXT,
    created_at TEXT NOT NULL,
    approved_at TEXT,
    FOREIGN KEY(rulebook_id, rulebook_version) REFERENCES risk_rulebooks(rulebook_id, version)
);

-- 计划内每个电池的待办条目（批准时由排序结果物化）。
CREATE TABLE IF NOT EXISTS plan_items (
    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL REFERENCES maintenance_plans(plan_id),
    battery_id TEXT NOT NULL,
    facility_id TEXT NOT NULL,
    evidence_id INTEGER REFERENCES health_evidence(evidence_id),
    activity_id TEXT REFERENCES maintenance_activities(activity_id),
    risk_score INTEGER NOT NULL,
    risk_band TEXT NOT NULL,
    ranking INTEGER NOT NULL,
    rationale_json TEXT NOT NULL,
    UNIQUE(plan_id, battery_id)
);

-- 维修窗口：批准时物化；失效只影响未执行窗口。
CREATE TABLE IF NOT EXISTS maintenance_windows (
    window_id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL REFERENCES maintenance_plans(plan_id),
    item_id INTEGER NOT NULL REFERENCES plan_items(item_id),
    battery_id TEXT NOT NULL,
    facility_id TEXT NOT NULL,
    activity_id TEXT NOT NULL,
    service_date TEXT NOT NULL,
    energy_offline_kwh TEXT NOT NULL,
    crew_id TEXT NOT NULL,
    spares_json TEXT NOT NULL,
    isolation_required INTEGER NOT NULL CHECK(isolation_required IN (0,1)),
    estimated_hours TEXT NOT NULL,
    selection_reasons_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'scheduled'
        CHECK(state IN ('scheduled','in_progress','paused','completed','retest_passed','retest_failed','invalidated','cancelled')),
    invalidation_reason TEXT,
    revision INTEGER NOT NULL DEFAULT 1,
    scheduled_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_windows_state ON maintenance_windows(state, service_date);
CREATE INDEX IF NOT EXISTS idx_windows_plan ON maintenance_windows(plan_id, window_id);
CREATE INDEX IF NOT EXISTS idx_windows_facility_date ON maintenance_windows(facility_id, service_date);

-- 窗口延期：记录风险接受人与新的最迟日期，保留全部历史。
CREATE TABLE IF NOT EXISTS window_postponements (
    postponement_id INTEGER PRIMARY KEY AUTOINCREMENT,
    window_id INTEGER NOT NULL REFERENCES maintenance_windows(window_id),
    previous_date TEXT NOT NULL,
    requested_date TEXT NOT NULL,
    new_date TEXT,
    new_latest_date TEXT NOT NULL,
    requested_by TEXT NOT NULL REFERENCES mp_users(user_id),
    risk_accepter_id TEXT REFERENCES mp_users(user_id),
    accepted_at TEXT,
    capacity_risk_kwh TEXT NOT NULL,
    reason TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','accepted','rejected')),
    created_at TEXT NOT NULL
);

-- 现场回执：以 (窗口, 回执类型, 客户端回执键) 幂等去重，迟到消息永不复活已关闭窗口。
CREATE TABLE IF NOT EXISTS window_receipts (
    receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    window_id INTEGER NOT NULL REFERENCES maintenance_windows(window_id),
    receipt_type TEXT NOT NULL CHECK(receipt_type IN ('start','pause','resume','complete','retest')),
    client_receipt_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    recorded_by TEXT NOT NULL REFERENCES mp_users(user_id),
    recorded_at TEXT NOT NULL,
    accepted INTEGER NOT NULL CHECK(accepted IN (0,1)),
    reject_reason TEXT,
    UNIQUE(window_id, receipt_type, client_receipt_key)
);

CREATE INDEX IF NOT EXISTS idx_receipts_window ON window_receipts(window_id, receipt_id);

-- 计划冻结后的资源占用（批准时写入，取消/失效时释放）。
CREATE TABLE IF NOT EXISTS resource_holds (
    hold_id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL REFERENCES maintenance_plans(plan_id),
    window_id INTEGER REFERENCES maintenance_windows(window_id),
    resource_type TEXT NOT NULL CHECK(resource_type IN ('facility_quota','crew','spare')),
    resource_key TEXT NOT NULL,
    service_date TEXT,
    spare_kind TEXT,
    quantity TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'held' CHECK(state IN ('held','released')),
    created_at TEXT NOT NULL,
    UNIQUE(plan_id, window_id, resource_type, resource_key, service_date)
);

CREATE INDEX IF NOT EXISTS idx_holds_calendar
ON resource_holds(resource_type, resource_key, service_date, state);

-- 失效事件影响到的窗口与原因，供运营接口解释。
CREATE TABLE IF NOT EXISTS window_invalidations (
    invalidation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    window_id INTEGER NOT NULL REFERENCES maintenance_windows(window_id),
    trigger_kind TEXT NOT NULL,
    trigger_reference TEXT NOT NULL,
    affected_field TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mp_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mp_audit_entity
ON mp_audit_events(entity_type, entity_id, event_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()
