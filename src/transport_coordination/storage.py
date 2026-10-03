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
CREATE TABLE IF NOT EXISTS charter_regions (
    region_code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    transport_org_id TEXT NOT NULL REFERENCES organizations(organization_id),
    tourism_org_id TEXT REFERENCES organizations(organization_id),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS charter_vehicles (
    vehicle_id TEXT PRIMARY KEY,
    owner_org_id TEXT NOT NULL REFERENCES organizations(organization_id),
    plate TEXT NOT NULL UNIQUE,
    seat_count INTEGER NOT NULL CHECK(seat_count > 0),
    transport_license_no TEXT NOT NULL,
    license_valid_until TEXT NOT NULL,
    insurance_valid_until TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS charter_drivers (
    driver_id TEXT PRIMARY KEY,
    owner_org_id TEXT NOT NULL REFERENCES organizations(organization_id),
    display_name TEXT NOT NULL,
    qualification_no TEXT NOT NULL UNIQUE,
    qualification_valid_until TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS charter_filings (
    filing_id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES organizations(organization_id),
    tour_code TEXT NOT NULL,
    passenger_count INTEGER NOT NULL CHECK(passenger_count > 0),
    planned_start TEXT NOT NULL,
    planned_end TEXT NOT NULL,
    vehicle_id TEXT NOT NULL,
    driver_id TEXT NOT NULL,
    contract_json TEXT NOT NULL,
    status TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    completed_at TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(org_id, tour_code)
);
CREATE TABLE IF NOT EXISTS charter_segments (
    segment_id TEXT PRIMARY KEY,
    filing_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    region_code TEXT NOT NULL,
    road_from TEXT NOT NULL,
    road_to TEXT NOT NULL,
    depart_at TEXT NOT NULL,
    arrive_at TEXT NOT NULL,
    UNIQUE(filing_id, revision, seq)
);
CREATE TABLE IF NOT EXISTS charter_stops (
    stop_id TEXT PRIMARY KEY,
    filing_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    segment_seq INTEGER NOT NULL,
    name TEXT NOT NULL,
    region_code TEXT NOT NULL,
    arrive_at TEXT NOT NULL,
    leave_at TEXT NOT NULL,
    UNIQUE(filing_id, revision, seq)
);
CREATE TABLE IF NOT EXISTS charter_permits (
    permit_id TEXT PRIMARY KEY,
    filing_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    segment_id TEXT NOT NULL,
    region_code TEXT NOT NULL,
    scope_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    prior_permit_id TEXT,
    amendment_id TEXT,
    decided_at TEXT,
    note TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(filing_id, revision, seq)
);
CREATE TABLE IF NOT EXISTS charter_closures (
    closure_id TEXT PRIMARY KEY,
    region_code TEXT NOT NULL,
    route_label TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL,
    reason TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS charter_permit_decisions (
    decision_id TEXT PRIMARY KEY,
    permit_id TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    department TEXT NOT NULL,
    decided_by TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('approve', 'reject')),
    conditions_json TEXT,
    note_text TEXT,
    inherited_from TEXT,
    decided_at TEXT NOT NULL,
    UNIQUE(permit_id, organization_id)
);
CREATE TABLE IF NOT EXISTS charter_certificates (
    certificate_no TEXT PRIMARY KEY,
    filing_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    status TEXT NOT NULL,
    parent_certificate_no TEXT,
    chain_head TEXT NOT NULL,
    permit_ids_json TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    issued_by TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    void_reason TEXT
);
CREATE TABLE IF NOT EXISTS charter_segment_closures (
    filing_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    closure_id TEXT NOT NULL,
    PRIMARY KEY(filing_id, revision, seq, closure_id)
);
CREATE TABLE IF NOT EXISTS charter_amendments (
    amendment_id TEXT PRIMARY KEY,
    filing_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    kind TEXT NOT NULL,
    reason TEXT,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    affected_seqs_json TEXT NOT NULL,
    certificate_no TEXT,
    refund_json TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
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
