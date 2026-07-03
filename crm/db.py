"""Personal CRM — SQLite layer.

Standalone database (data/crm.db), separate from war_room.db by design:
the CRM is an editable working surface with its own lifecycle. All real data
lives under data/ (gitignored); this code ships with zero seed data.

Integrity rules (the drift contract):
  - Every record carries `source` provenance: sap-export | meeting-notes |
    vault-parse | manual.
  - Re-ingesting the same file is idempotent (dedupe keys below).
  - Ingest refreshes SYSTEM fields freely (phase, amounts, dates, owner,
    untouched-days) but never touches USER fields (status, next_step,
    user_notes, motion) once set by hand.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from typing import Iterator

from config import DATA_DIR

CRM_DB_PATH = DATA_DIR / "crm.db"
CRM_INBOX = DATA_DIR / "crm" / "inbox"


SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    sap_account_id  TEXT,
    state           TEXT,
    vertical        TEXT,
    notes           TEXT,
    source          TEXT NOT NULL DEFAULT 'manual',
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS deals (
    id              INTEGER PRIMARY KEY,
    account_id      INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    opp_id          TEXT UNIQUE,             -- CRM opportunity id (dedupe key for exports)
    name            TEXT NOT NULL,           -- description line from the export, or manual
    product         TEXT,                    -- inferred / edited
    phase           TEXT,
    days_in_phase   INTEGER,
    forecast_cat    TEXT,
    close_date      TEXT,                    -- ISO date
    close_quarter   TEXT,
    amount_1x       REAL,
    amount_mp       REAL,
    opp_owner       TEXT,
    untouched_days  INTEGER,
    passive         INTEGER NOT NULL DEFAULT 0,   -- 006-prefix: paid, not worked
    renewal         INTEGER NOT NULL DEFAULT 0,
    -- user-owned fields (ingest never writes these once set):
    status          TEXT NOT NULL DEFAULT 'active',  -- active|stalled|no-bid|lost|won
    motion          TEXT,                    -- anchor | small-deal | passive | ...
    next_step       TEXT,
    next_step_date  TEXT,
    user_notes      TEXT,
    source_hub      TEXT,                    -- vault path of the deal hub, if any
    source          TEXT NOT NULL DEFAULT 'manual',
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_deals_account ON deals(account_id);

CREATE TABLE IF NOT EXISTS contacts (
    id              INTEGER PRIMARY KEY,
    account_id      INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    title           TEXT,
    org             TEXT DEFAULT 'customer',  -- customer | internal | partner
    role_status     TEXT,                     -- champion | blocker | exec | neutral | sponsor
    disc            TEXT,
    email           TEXT,
    phone           TEXT,
    notes           TEXT,
    last_touch      TEXT,
    source          TEXT NOT NULL DEFAULT 'manual',
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    UNIQUE(account_id, name)
);

CREATE TABLE IF NOT EXISTS activities (
    id              INTEGER PRIMARY KEY,
    account_id      INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    deal_id         INTEGER REFERENCES deals(id) ON DELETE SET NULL,
    date            TEXT NOT NULL,            -- ISO date
    type            TEXT NOT NULL DEFAULT 'meeting',  -- meeting|call|email|internal|decision
    title           TEXT NOT NULL,
    summary         TEXT,
    next_steps      TEXT,                     -- JSON array of strings
    source          TEXT NOT NULL DEFAULT 'manual',
    source_ref      TEXT,
    created_at      INTEGER NOT NULL,
    UNIQUE(account_id, date, title)
);
CREATE INDEX IF NOT EXISTS idx_activities_date ON activities(date DESC);

CREATE TABLE IF NOT EXISTS reminders (
    id              INTEGER PRIMARY KEY,
    account_id      INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
    deal_id         INTEGER REFERENCES deals(id) ON DELETE CASCADE,
    due_date        TEXT,
    kind            TEXT NOT NULL DEFAULT 'follow-up',  -- follow-up|re-engage|t90-renewal|stale-alert
    note            TEXT NOT NULL,
    done            INTEGER NOT NULL DEFAULT 0,
    source          TEXT NOT NULL DEFAULT 'manual',
    created_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS imports (
    id              INTEGER PRIMARY KEY,
    kind            TEXT NOT NULL,            -- sap-export | meeting-notes | vault-seed
    filename        TEXT,
    imported_at     INTEGER NOT NULL,
    added           INTEGER NOT NULL DEFAULT 0,
    updated         INTEGER NOT NULL DEFAULT 0,
    skipped         INTEGER NOT NULL DEFAULT 0,
    detail          TEXT
);
"""


def now() -> int:
    return int(time.time())


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(CRM_DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def conn_ctx() -> Iterator[sqlite3.Connection]:
    conn = get_conn()
    try:
        yield conn
    finally:
        conn.close()


def init_crm_db() -> None:
    CRM_INBOX.mkdir(parents=True, exist_ok=True)
    with conn_ctx() as conn:
        conn.executescript(SCHEMA)


# ---------------------------------------------------------------------------
# Account matching — export account names are often truncated, so matching is
# tiered: exact (case-insensitive) → prefix/containment → create new.
# ---------------------------------------------------------------------------

def _norm(name: str) -> str:
    return " ".join(name.lower().replace(",", " ").replace(".", " ").split())


def find_or_create_account(conn: sqlite3.Connection, name: str,
                           source: str, sap_account_id: str | None = None) -> int:
    """Match an account by normalized name with containment fallback."""
    name = name.strip()
    n = _norm(name)
    rows = conn.execute("SELECT id, name, sap_account_id FROM accounts").fetchall()
    # 1) exact normalized
    for r in rows:
        if _norm(r["name"]) == n:
            if sap_account_id and not r["sap_account_id"]:
                conn.execute("UPDATE accounts SET sap_account_id=?, updated_at=? WHERE id=?",
                             (sap_account_id, now(), r["id"]))
            return r["id"]
    # 2) sap account id
    if sap_account_id:
        for r in rows:
            if r["sap_account_id"] == sap_account_id:
                return r["id"]
    # 3) containment either way (handles truncated export names)
    for r in rows:
        rn = _norm(r["name"])
        if (n and rn.startswith(n)) or (rn and n.startswith(rn)):
            # keep the longer name as canonical
            if len(name) > len(r["name"]):
                conn.execute("UPDATE accounts SET name=?, updated_at=? WHERE id=?",
                             (name, now(), r["id"]))
            if sap_account_id and not r["sap_account_id"]:
                conn.execute("UPDATE accounts SET sap_account_id=?, updated_at=? WHERE id=?",
                             (sap_account_id, now(), r["id"]))
            return r["id"]
    cur = conn.execute(
        "INSERT INTO accounts (name, sap_account_id, source, created_at, updated_at) VALUES (?,?,?,?,?)",
        (name, sap_account_id, source, now(), now()))
    return cur.lastrowid


def log_import(conn: sqlite3.Connection, kind: str, filename: str | None,
               added: int, updated: int, skipped: int, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO imports (kind, filename, imported_at, added, updated, skipped, detail) VALUES (?,?,?,?,?,?,?)",
        (kind, filename, now(), added, updated, skipped, detail))
