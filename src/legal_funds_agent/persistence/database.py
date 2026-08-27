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
"""


def connect(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    return connection

