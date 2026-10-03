"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS facilities (
    facility_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    facility_type TEXT NOT NULL,
    location TEXT NOT NULL,
    description TEXT NOT NULL,
    affected_groups_json TEXT NOT NULL,
    sole_accessible_route INTEGER NOT NULL CHECK(sole_accessible_route IN (0, 1)),
    available INTEGER NOT NULL CHECK(available IN (0, 1)),
    available_note TEXT,
    current_incident_id TEXT,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS maintenance_contracts (
    contract_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    contractor_org_id TEXT NOT NULL REFERENCES organizations(organization_id),
    facility_id TEXT REFERENCES facilities(facility_id),
    valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL,
    sla_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS incidents (
    incident_id TEXT PRIMARY KEY,
    facility_id TEXT NOT NULL REFERENCES facilities(facility_id),
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    status TEXT NOT NULL,
    reported_severity TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    impact_json TEXT NOT NULL,
    affected_groups_json TEXT NOT NULL,
    frozen_contractor_org TEXT NOT NULL,
    frozen_contract_id TEXT NOT NULL,
    sla_json TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    due_response_at TEXT NOT NULL,
    due_measure_at TEXT NOT NULL,
    due_repair_at TEXT NOT NULL,
    escalation_level INTEGER NOT NULL DEFAULT 0 CHECK(escalation_level >= 0),
    recurrence_count INTEGER NOT NULL DEFAULT 0 CHECK(recurrence_count >= 0),
    recurrence_of_incident_id TEXT,
    reopened_at TEXT,
    reopened_by TEXT,
    basis_reinspection_id TEXT,
    guarantee_until TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS work_orders (
    work_order_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES incidents(incident_id),
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    contractor_org_id TEXT NOT NULL,
    contract_id TEXT NOT NULL,
    status TEXT NOT NULL,
    due_response_at TEXT,
    due_repair_at TEXT NOT NULL,
    due_reinspect_at TEXT,
    responded_at TEXT,
    completed_at TEXT,
    completed_by TEXT,
    continued_from_work_order_id TEXT,
    summary TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(incident_id, seq)
);
CREATE TABLE IF NOT EXISTS temporary_measures (
    measure_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES incidents(incident_id),
    work_order_id TEXT REFERENCES work_orders(work_order_id),
    provider_org_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    description TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL,
    handover_rule TEXT NOT NULL,
    status TEXT NOT NULL,
    supersedes_measure_id TEXT,
    handed_over_to TEXT,
    handed_over_at TEXT,
    handover_note TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reinspections (
    reinspection_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES incidents(incident_id),
    work_order_id TEXT NOT NULL REFERENCES work_orders(work_order_id),
    inspector_actor_id TEXT NOT NULL,
    inspector_org_id TEXT NOT NULL,
    result TEXT NOT NULL CHECK(result IN ('pass', 'fail')),
    independent INTEGER NOT NULL CHECK(independent IN (0, 1)),
    evidence_json TEXT NOT NULL,
    evidence_hash TEXT NOT NULL,
    note TEXT NOT NULL,
    withdrawn INTEGER NOT NULL DEFAULT 0 CHECK(withdrawn IN (0, 1)),
    withdrawn_by TEXT,
    withdrawn_at TEXT,
    withdrawn_reason TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS incident_events (
    event_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES incidents(incident_id),
    seq INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    work_order_id TEXT,
    measure_id TEXT,
    reinspection_id TEXT,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(incident_id, seq)
);
CREATE TABLE IF NOT EXISTS notifications (
    notification_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES incidents(incident_id),
    event_seq INTEGER,
    recipient_kind TEXT NOT NULL,
    recipient_id TEXT NOT NULL,
    recipient_label TEXT NOT NULL,
    channel TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS overdue_flags (
    incident_id TEXT NOT NULL REFERENCES incidents(incident_id),
    flag_key TEXT NOT NULL,
    escalation_level INTEGER NOT NULL,
    first_flagged_at TEXT NOT NULL,
    resolved_at TEXT,
    PRIMARY KEY(incident_id, flag_key)
);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
