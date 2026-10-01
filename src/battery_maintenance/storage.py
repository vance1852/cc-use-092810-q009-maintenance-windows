"""维修计划服务的 SQLite 模式与事务辅助。"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path


SCHEMA_VERSION = 1

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS maintenance_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('planner','risk','technician','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stations (
    station_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    timezone TEXT NOT NULL,
    daily_outage_kwh TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT PRIMARY KEY,
    station_id TEXT NOT NULL REFERENCES stations(station_id),
    device_kind TEXT NOT NULL,
    model_name TEXT NOT NULL,
    rated_capacity_kwh TEXT NOT NULL,
    required_qualification TEXT NOT NULL,
    required_isolation_json TEXT NOT NULL DEFAULT '[]',
    required_spare_skus_json TEXT NOT NULL DEFAULT '[]',
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_devices_station ON devices(station_id);

CREATE TABLE IF NOT EXISTS health_evidence (
    evidence_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    version TEXT NOT NULL,
    content_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
    observed_at TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES maintenance_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(device_id, version)
);

CREATE INDEX IF NOT EXISTS idx_evidence_device ON health_evidence(device_id, observed_at, evidence_id);

CREATE TABLE IF NOT EXISTS risk_rule_set_versions (
    rule_set_id TEXT NOT NULL,
    version TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
    created_by TEXT NOT NULL REFERENCES maintenance_users(user_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(rule_set_id, version)
);

CREATE TABLE IF NOT EXISTS risk_rule_sets (
    rule_set_id TEXT PRIMARY KEY,
    current_version TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS technicians (
    technician_id TEXT PRIMARY KEY REFERENCES maintenance_users(user_id),
    display_name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS technician_qualifications (
    technician_id TEXT NOT NULL REFERENCES technicians(technician_id),
    qualification TEXT NOT NULL,
    PRIMARY KEY(technician_id, qualification)
);

CREATE TABLE IF NOT EXISTS technician_unavailability (
    unavailability_id INTEGER PRIMARY KEY AUTOINCREMENT,
    technician_id TEXT NOT NULL REFERENCES technicians(technician_id),
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    CHECK(ends_at > starts_at)
);

CREATE INDEX IF NOT EXISTS idx_unavailable_tech ON technician_unavailability(technician_id, starts_at, ends_at);

CREATE TABLE IF NOT EXISTS isolation_bays (
    bay_id TEXT PRIMARY KEY,
    station_id TEXT NOT NULL REFERENCES stations(station_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bay_isolation_kinds (
    bay_id TEXT NOT NULL REFERENCES isolation_bays(bay_id),
    isolation_kind TEXT NOT NULL,
    PRIMARY KEY(bay_id, isolation_kind)
);

CREATE TABLE IF NOT EXISTS spare_parts (
    station_id TEXT NOT NULL REFERENCES stations(station_id),
    sku TEXT NOT NULL,
    quantity_on_hand INTEGER NOT NULL CHECK(quantity_on_hand >= 0),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(station_id, sku)
);

CREATE TABLE IF NOT EXISTS maintenance_plans (
    plan_id TEXT PRIMARY KEY,
    rule_set_id TEXT NOT NULL,
    rule_set_version TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('proposed','approved','superseded')),
    anchor_date TEXT NOT NULL,
    horizon_days INTEGER NOT NULL CHECK(horizon_days > 0),
    station_ids_json TEXT NOT NULL,
    input_sha256 TEXT NOT NULL CHECK(length(input_sha256)=64),
    result_json TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
    idempotency_key TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL REFERENCES maintenance_users(user_id),
    created_at TEXT NOT NULL,
    approved_by TEXT REFERENCES maintenance_users(user_id),
    approved_at TEXT
);

CREATE TABLE IF NOT EXISTS plan_evidence_snapshot (
    plan_id TEXT NOT NULL REFERENCES maintenance_plans(plan_id),
    device_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    evidence_version TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    PRIMARY KEY(plan_id, device_id)
);

CREATE TABLE IF NOT EXISTS maintenance_windows (
    window_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES maintenance_plans(plan_id),
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    station_id TEXT NOT NULL,
    service_date TEXT NOT NULL,
    original_date TEXT NOT NULL,
    latest_date TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    risk_points INTEGER NOT NULL,
    evidence_id TEXT NOT NULL,
    technician_id TEXT,
    bay_id TEXT,
    required_spare_skus_json TEXT NOT NULL DEFAULT '[]',
    required_isolation_json TEXT NOT NULL DEFAULT '[]',
    rationale_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN
        ('proposed','approved','invalidated','superseded','in_progress','paused','completed','failed','cancelled')),
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
    created_at TEXT NOT NULL,
    started_at TEXT,
    paused_at TEXT,
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_windows_state_date ON maintenance_windows(state, service_date);
CREATE INDEX IF NOT EXISTS idx_windows_device ON maintenance_windows(device_id, state);
CREATE INDEX IF NOT EXISTS idx_windows_station_date ON maintenance_windows(station_id, service_date, state);

CREATE TABLE IF NOT EXISTS resource_reservations (
    reservation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    window_id TEXT NOT NULL REFERENCES maintenance_windows(window_id),
    resource_type TEXT NOT NULL CHECK(resource_type IN ('station_outage','technician','bay','spare')),
    station_id TEXT,
    resource_id TEXT,
    service_date TEXT NOT NULL,
    quantity TEXT NOT NULL DEFAULT '1',
    state TEXT NOT NULL DEFAULT 'held' CHECK(state IN ('held','released','consumed')),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reservations_lookup
ON resource_reservations(resource_type, station_id, resource_id, service_date, state);
CREATE INDEX IF NOT EXISTS idx_reservations_window ON resource_reservations(window_id, state);

CREATE TABLE IF NOT EXISTS work_order_receipts (
    receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    window_id TEXT NOT NULL REFERENCES maintenance_windows(window_id),
    event TEXT NOT NULL CHECK(event IN ('started','paused','resumed','completed','retest')),
    idempotency_key TEXT,
    payload_json TEXT NOT NULL,
    actor_id TEXT NOT NULL REFERENCES maintenance_users(user_id),
    received_at TEXT NOT NULL,
    UNIQUE(window_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS window_extensions (
    extension_id INTEGER PRIMARY KEY AUTOINCREMENT,
    window_id TEXT NOT NULL REFERENCES maintenance_windows(window_id),
    previous_latest_date TEXT NOT NULL,
    new_latest_date TEXT NOT NULL,
    reason TEXT NOT NULL,
    risk_acceptor_id TEXT NOT NULL REFERENCES maintenance_users(user_id),
    created_by TEXT NOT NULL REFERENCES maintenance_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_extensions_window ON window_extensions(window_id, extension_id);

CREATE TABLE IF NOT EXISTS window_invalidations (
    invalidation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    window_id TEXT NOT NULL REFERENCES maintenance_windows(window_id),
    reason TEXT NOT NULL CHECK(reason IN ('evidence_changed','urgent_alarm','recall_escalation','plan_superseded')),
    detail_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_invalidations_window ON window_invalidations(window_id, invalidation_id);

CREATE TABLE IF NOT EXISTS incoming_signals (
    signal_id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    signal_kind TEXT NOT NULL CHECK(signal_kind IN ('urgent_alarm','recall_escalation','evidence_changed')),
    payload_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES maintenance_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_signals_device ON incoming_signals(device_id, signal_id);

CREATE TABLE IF NOT EXISTS maintenance_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS maintenance_audit_events (
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

CREATE INDEX IF NOT EXISTS idx_maintenance_audit_entity
ON maintenance_audit_events(entity_type, entity_id, event_id);
"""

REQUIRED_TABLES = frozenset({
    "schema_meta", "maintenance_users", "stations", "devices", "health_evidence",
    "risk_rule_set_versions", "risk_rule_sets", "technicians", "technician_qualifications",
    "technician_unavailability", "isolation_bays", "bay_isolation_kinds", "spare_parts",
    "maintenance_plans", "plan_evidence_snapshot", "maintenance_windows",
    "resource_reservations", "work_order_receipts", "window_extensions",
    "window_invalidations", "incoming_signals", "maintenance_idempotency",
    "maintenance_audit_events",
})


def connect(path: str | Path) -> sqlite3.Connection:
    # HTTP 服务使用 ThreadingHTTPServer；写入一律走 BEGIN IMMEDIATE 串行化。
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


@contextlib.contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA_SQL)
    with transaction(connection, immediate=True):
        connection.execute(
            "INSERT INTO schema_meta(key,value) VALUES('schema_version',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )


def inspect_schema(connection: sqlite3.Connection) -> dict[str, object]:
    table_rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    tables = tuple(row["name"] for row in table_rows)
    version_row = connection.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
    return {
        "tables": tables,
        "missing_tables": sorted(REQUIRED_TABLES - set(tables)),
        "schema_version": None if version_row is None else version_row["value"],
        "foreign_keys": bool(foreign_keys),
    }
