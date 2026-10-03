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
CREATE TABLE IF NOT EXISTS accessibility_facilities (
    facility_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    facility_type TEXT NOT NULL,
    location_desc TEXT NOT NULL,
    affected_groups_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('in_service', 'out_of_service')),
    current_event_id TEXT,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS maintenance_contracts (
    contract_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    provider_organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    title TEXT NOT NULL,
    facility_type TEXT,
    facility_id TEXT REFERENCES accessibility_facilities(facility_id),
    contact TEXT NOT NULL,
    sla_json TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS defect_events (
    event_id TEXT PRIMARY KEY,
    chain_id TEXT NOT NULL,
    chain_seq INTEGER NOT NULL,
    facility_id TEXT NOT NULL REFERENCES accessibility_facilities(facility_id),
    report_type TEXT NOT NULL CHECK(report_type IN ('initial', 'recurrence', 'evidence_withdrawn')),
    parent_event_id TEXT REFERENCES defect_events(event_id),
    description TEXT NOT NULL,
    impact_groups_json TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    reported_risk_level TEXT NOT NULL,
    escalated INTEGER NOT NULL CHECK(escalated IN (0, 1)),
    status TEXT NOT NULL CHECK(status IN ('open', 'in_progress', 'awaiting_reinspection', 'closed')),
    frozen_contract_id TEXT,
    frozen_provider_org_id TEXT,
    frozen_sla_json TEXT NOT NULL,
    repeat_count INTEGER NOT NULL DEFAULT 0,
    reported_by TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    closed_evidence_id TEXT,
    UNIQUE(chain_id, chain_seq)
);
CREATE TABLE IF NOT EXISTS work_orders (
    work_order_id TEXT PRIMARY KEY,
    chain_id TEXT NOT NULL,
    event_id TEXT NOT NULL REFERENCES defect_events(event_id),
    seq INTEGER NOT NULL,
    provider_organization_id TEXT NOT NULL,
    contract_id TEXT,
    status TEXT NOT NULL CHECK(status IN ('open', 'in_progress', 'completed', 'rejected', 'transferred')),
    due_response_at TEXT NOT NULL,
    due_repair_at TEXT NOT NULL,
    responded_at TEXT,
    completed_at TEXT,
    response_breached_at TEXT,
    repair_breached_at TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(event_id, seq)
);
CREATE TABLE IF NOT EXISTS interim_measures (
    measure_id TEXT PRIMARY KEY,
    chain_id TEXT NOT NULL,
    event_id TEXT NOT NULL REFERENCES defect_events(event_id),
    kind TEXT NOT NULL,
    description TEXT NOT NULL,
    provider_organization_id TEXT NOT NULL,
    committed_by TEXT NOT NULL,
    linked_from_measure_id TEXT,
    effective_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'expired', 'handed_over')),
    handed_to_org_id TEXT,
    handed_over_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reinspection_evidence (
    evidence_id TEXT PRIMARY KEY,
    chain_id TEXT NOT NULL,
    event_id TEXT NOT NULL REFERENCES defect_events(event_id),
    work_order_id TEXT NOT NULL,
    inspector_actor_id TEXT NOT NULL,
    inspector_organization_id TEXT NOT NULL,
    result TEXT NOT NULL CHECK(result IN ('passed', 'failed')),
    summary TEXT NOT NULL,
    evidence_ref TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    evidence_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('valid', 'withdrawn')),
    withdrawn_by TEXT,
    withdrawn_at TEXT,
    withdraw_reason TEXT,
    inspected_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notifications (
    notification_id TEXT PRIMARY KEY,
    chain_id TEXT,
    event_id TEXT,
    facility_id TEXT,
    target_actor_id TEXT,
    target_organization_id TEXT,
    target_role TEXT,
    channel TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK(target_actor_id IS NOT NULL OR target_organization_id IS NOT NULL)
);
CREATE TABLE IF NOT EXISTS facility_status_history (
    history_id INTEGER PRIMARY KEY AUTOINCREMENT,
    facility_id TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    event_id TEXT,
    changed_by TEXT NOT NULL,
    changed_at TEXT NOT NULL
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
