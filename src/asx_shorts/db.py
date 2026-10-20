"""SQLite storage. One file, no server; fine for ~3M rows and read-heavy traffic."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from .sec_insiders import SCHEMA as INSIDER_SCHEMA
from .sec_form144 import SCHEMA as FORM144_SCHEMA

DEFAULT_PATH = Path(os.environ.get("ASX_SHORTS_DB", "data/asx_shorts.sqlite"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    position_date TEXT PRIMARY KEY,          -- ASIC file date = positions as at this date
    status        TEXT NOT NULL CHECK (status IN ('ok', 'none')),
    rows          INTEGER NOT NULL DEFAULT 0,
    source_url    TEXT,
    sha256        TEXT,
    fetched_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS positions (
    code            TEXT NOT NULL,
    position_date   TEXT NOT NULL,
    short_positions INTEGER NOT NULL,
    shares_on_issue INTEGER NOT NULL,
    short_pct       REAL NOT NULL,
    PRIMARY KEY (code, position_date)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS positions_by_date ON positions (position_date, short_pct DESC);
CREATE TABLE IF NOT EXISTS products (
    code       TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    first_date TEXT NOT NULL,
    last_date  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS api_keys (
    key_hash   TEXT PRIMARY KEY,               -- sha256 of the key; the key itself is never stored
    label      TEXT NOT NULL,
    tier       TEXT NOT NULL CHECK (tier IN ('free', 'pro')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS usage (
    key_hash TEXT NOT NULL,
    day      TEXT NOT NULL,
    requests INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (key_hash, day)
) WITHOUT ROWID;
-- Self-serve sign-up limits. Client IPs are stored only as a salted hash and
-- deleted after two days; they exist to stop one machine minting endless keys.
CREATE TABLE IF NOT EXISTS signups (
    day     TEXT NOT NULL,
    ip_hash TEXT NOT NULL,
    keys    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, ip_hash)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS waitlist (
    email      TEXT PRIMARY KEY,
    use_case   TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
-- Anonymous demand signal: requests per endpoint / MCP tool per day and plan.
CREATE TABLE IF NOT EXISTS route_usage (
    day      TEXT NOT NULL,
    route    TEXT NOT NULL,
    tier     TEXT NOT NULL,
    requests INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, route, tier)
) WITHOUT ROWID;
"""


def connect(path: Path | str | None = None, *, readonly: bool = False) -> sqlite3.Connection:
    p = Path(path or DEFAULT_PATH)
    if readonly:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, check_same_thread=False)
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(p, check_same_thread=False, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")   # parallel ingest workers wait instead of failing
        conn.executescript(SCHEMA)
        conn.executescript(INSIDER_SCHEMA)
        conn.executescript(FORM144_SCHEMA)
    conn.row_factory = sqlite3.Row
    return conn
