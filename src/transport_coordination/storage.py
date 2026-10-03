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
CREATE TABLE IF NOT EXISTS region_licenses (
    license_id TEXT PRIMARY KEY,
    region_code TEXT NOT NULL,
    segment_id TEXT,
    route_code TEXT,
    status TEXT NOT NULL CHECK(status IN ('active','suspended')),
    valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(region_code, segment_id, route_code)
);
CREATE TABLE IF NOT EXISTS reviewer_regions (
    actor_id TEXT NOT NULL,
    region_code TEXT NOT NULL,
    assigned_at TEXT NOT NULL,
    PRIMARY KEY(actor_id, region_code)
);
CREATE TABLE IF NOT EXISTS inspection_keys (
    api_key TEXT PRIMARY KEY,
    region_code TEXT,
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS charter_groups (
    group_id TEXT PRIMARY KEY,
    travel_agency_org_id TEXT NOT NULL,
    current_version INTEGER NOT NULL CHECK(current_version >= 1),
    head_status TEXT NOT NULL,
    executable INTEGER NOT NULL CHECK(executable IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS charter_versions (
    group_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    parent_version INTEGER,
    trigger_event_id TEXT,
    status TEXT NOT NULL,
    executable INTEGER NOT NULL CHECK(executable IN (0, 1)),
    started_at TEXT,
    payload_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(group_id, version)
);
CREATE TABLE IF NOT EXISTS charter_segments (
    group_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    segment_id TEXT NOT NULL,
    region_code TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    route_code TEXT,
    payload_json TEXT NOT NULL,
    PRIMARY KEY(group_id, version, segment_id)
);
CREATE TABLE IF NOT EXISTS segment_reviews (
    group_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    segment_id TEXT NOT NULL,
    region_code TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('approved','rejected','revoked')),
    basis_license_id TEXT,
    reason TEXT,
    reviewed_by TEXT NOT NULL,
    reviewed_at TEXT NOT NULL,
    carried_from_version INTEGER,
    PRIMARY KEY(group_id, version, segment_id)
);
CREATE TABLE IF NOT EXISTS trip_events (
    event_id TEXT PRIMARY KEY,
    group_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    impact_scope TEXT NOT NULL CHECK(impact_scope IN ('all','segments','none')),
    affected_segments_json TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_segments_region ON charter_segments(region_code, version);
CREATE INDEX IF NOT EXISTS idx_reviews_region ON segment_reviews(region_code, version);
CREATE INDEX IF NOT EXISTS idx_trip_events_group ON trip_events(group_id, version);
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
