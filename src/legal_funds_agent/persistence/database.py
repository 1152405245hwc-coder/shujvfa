from __future__ import annotations

import sqlite3
from pathlib import Path


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS claims (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transactions (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transactions_case ON transactions(case_id);
CREATE INDEX IF NOT EXISTS idx_transactions_fingerprint ON transactions(case_id, fingerprint);
CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    claim_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    decision_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    UNIQUE(claim_id, version)
);
CREATE TABLE IF NOT EXISTS audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    step TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS investigation_items (
    case_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY(case_id, item_id)
);
CREATE TABLE IF NOT EXISTS case_snapshots (
    case_id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS case_meta (
    case_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

# Idempotent migration for databases created before case_snapshots existed.
# Existing files already pass the `claims` table check in connect(), so the
# full SCHEMA script never runs for them; each new mutable table must also be
# ensured here.
MIGRATIONS = """
CREATE TABLE IF NOT EXISTS case_snapshots (
    case_id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS case_meta (
    case_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialized = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='claims'"
    ).fetchone()
    if not initialized:
        connection.executescript(SCHEMA)
    else:
        connection.executescript(MIGRATIONS)
    return connection
