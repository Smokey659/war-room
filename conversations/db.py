"""SQLite connection + schema for War Room.

One database, three responsibilities:
  - Vault FTS5 index (the `notes` table) → fed by retrieval/indexer.py
  - Conversation threads + messages → fed by conversations/threads.py
  - Cost log per Claude call → stored on the messages row itself

Single-file SQLite is plenty for v1. If/when the scale changes, swap the
connection layer; the rest of the code stays.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from config import DB_PATH


SCHEMA = """
-- Threads = conversations. Each has many messages.
CREATE TABLE IF NOT EXISTS threads (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_threads_updated ON threads(updated_at DESC);

-- Messages = the chat log inside a thread. Includes per-message cost data.
CREATE TABLE IF NOT EXISTS messages (
    id              TEXT PRIMARY KEY,
    thread_id       TEXT NOT NULL,
    role            TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content         TEXT NOT NULL,
    sources_json    TEXT,                    -- JSON array of {path, name} used as context
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    cost_usd        REAL,
    created_at      INTEGER NOT NULL,
    FOREIGN KEY (thread_id) REFERENCES threads(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id, created_at);

-- FTS5 over the vault. Re-built on demand by retrieval/indexer.py.
-- `path` and `last_modified` are unindexed metadata.
CREATE VIRTUAL TABLE IF NOT EXISTS notes USING fts5(
    path UNINDEXED,
    name,
    content,
    last_modified UNINDEXED,
    tokenize = 'porter unicode61'
);
"""


def get_conn() -> sqlite3.Connection:
    """Open a connection with sensible defaults. Caller closes."""
    conn = sqlite3.connect(DB_PATH, isolation_level=None)  # autocommit
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def conn_ctx() -> Iterator[sqlite3.Connection]:
    """Context-managed connection. Use for short-lived operations."""
    conn = get_conn()
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    """Run schema. Idempotent."""
    with conn_ctx() as conn:
        conn.executescript(SCHEMA)
