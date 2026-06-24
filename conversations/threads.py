"""Thread + message CRUD on SQLite.

Threads are conversations. Messages are the per-turn entries. Each message stores
its own token counts + cost (computed at write time from the message's role and
the model's $/M rates in config).
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from config import INPUT_PRICE_PER_M_USD, OUTPUT_PRICE_PER_M_USD
from conversations.db import conn_ctx


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class Source:
    """A vault note that was used as context for an assistant message."""
    path: str   # path relative to vault root
    name: str   # display name (filename stem)


@dataclass
class Message:
    id: str
    thread_id: str
    role: str            # 'user' | 'assistant'
    content: str
    sources: list[Source] = field(default_factory=list)
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cost_usd: Optional[float] = None
    created_at: int = 0


@dataclass
class Thread:
    id: str
    title: str
    created_at: int
    updated_at: int


# ---------------------------------------------------------------------------
# Threads
# ---------------------------------------------------------------------------


def create_thread(title: str = "New chat") -> Thread:
    """Insert a thread; return the row."""
    now = int(time.time())
    tid = uuid.uuid4().hex
    with conn_ctx() as conn:
        conn.execute(
            "INSERT INTO threads (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (tid, title, now, now),
        )
    return Thread(id=tid, title=title, created_at=now, updated_at=now)


def list_threads(limit: int = 50) -> list[Thread]:
    """Most-recently-updated first."""
    with conn_ctx() as conn:
        rows = conn.execute(
            "SELECT id, title, created_at, updated_at FROM threads ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [Thread(**dict(r)) for r in rows]


def get_thread(thread_id: str) -> Optional[Thread]:
    with conn_ctx() as conn:
        row = conn.execute(
            "SELECT id, title, created_at, updated_at FROM threads WHERE id = ?",
            (thread_id,),
        ).fetchone()
    return Thread(**dict(row)) if row else None


def update_thread_title(thread_id: str, title: str) -> None:
    with conn_ctx() as conn:
        conn.execute(
            "UPDATE threads SET title = ?, updated_at = ? WHERE id = ?",
            (title, int(time.time()), thread_id),
        )


def touch_thread(thread_id: str) -> None:
    """Bump the updated_at timestamp (for sorting in the sidebar)."""
    with conn_ctx() as conn:
        conn.execute(
            "UPDATE threads SET updated_at = ? WHERE id = ?",
            (int(time.time()), thread_id),
        )


def delete_thread(thread_id: str) -> None:
    with conn_ctx() as conn:
        conn.execute("DELETE FROM threads WHERE id = ?", (thread_id,))


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


def list_messages(thread_id: str) -> list[Message]:
    """All messages in a thread, oldest first."""
    with conn_ctx() as conn:
        rows = conn.execute(
            """SELECT id, thread_id, role, content, sources_json,
                      input_tokens, output_tokens, cost_usd, created_at
               FROM messages WHERE thread_id = ? ORDER BY created_at ASC""",
            (thread_id,),
        ).fetchall()
    return [_row_to_message(r) for r in rows]


def get_message(message_id: str) -> Optional[Message]:
    with conn_ctx() as conn:
        row = conn.execute(
            """SELECT id, thread_id, role, content, sources_json,
                      input_tokens, output_tokens, cost_usd, created_at
               FROM messages WHERE id = ?""",
            (message_id,),
        ).fetchone()
    return _row_to_message(row) if row else None


def append_message(
    thread_id: str,
    role: str,
    content: str,
    sources: Optional[list[Source]] = None,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
) -> Message:
    """Insert a message; auto-compute cost from token counts; touch the thread."""
    now = int(time.time())
    mid = uuid.uuid4().hex
    sources = sources or []
    cost_usd = _compute_cost(input_tokens, output_tokens)

    with conn_ctx() as conn:
        conn.execute(
            """INSERT INTO messages
               (id, thread_id, role, content, sources_json,
                input_tokens, output_tokens, cost_usd, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (mid, thread_id, role, content,
             json.dumps([s.__dict__ for s in sources]),
             input_tokens, output_tokens, cost_usd, now),
        )
        conn.execute(
            "UPDATE threads SET updated_at = ? WHERE id = ?",
            (now, thread_id),
        )

    return Message(
        id=mid, thread_id=thread_id, role=role, content=content,
        sources=sources, input_tokens=input_tokens, output_tokens=output_tokens,
        cost_usd=cost_usd, created_at=now,
    )


def update_message_content(
    message_id: str,
    content: str,
    sources: Optional[list[Source]] = None,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
) -> None:
    """Used during streaming finalization — replace the placeholder body with the full response."""
    cost_usd = _compute_cost(input_tokens, output_tokens)
    with conn_ctx() as conn:
        if sources is not None:
            conn.execute(
                """UPDATE messages
                   SET content = ?, sources_json = ?, input_tokens = ?,
                       output_tokens = ?, cost_usd = ?
                   WHERE id = ?""",
                (content, json.dumps([s.__dict__ for s in sources]),
                 input_tokens, output_tokens, cost_usd, message_id),
            )
        else:
            conn.execute(
                """UPDATE messages
                   SET content = ?, input_tokens = ?, output_tokens = ?, cost_usd = ?
                   WHERE id = ?""",
                (content, input_tokens, output_tokens, cost_usd, message_id),
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_to_message(row) -> Message:
    sources_json = row["sources_json"]
    sources = (
        [Source(**s) for s in json.loads(sources_json)] if sources_json else []
    )
    return Message(
        id=row["id"],
        thread_id=row["thread_id"],
        role=row["role"],
        content=row["content"],
        sources=sources,
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        cost_usd=row["cost_usd"],
        created_at=row["created_at"],
    )


def _compute_cost(input_tokens: Optional[int], output_tokens: Optional[int]) -> Optional[float]:
    if input_tokens is None or output_tokens is None:
        return None
    return (
        (input_tokens / 1_000_000) * INPUT_PRICE_PER_M_USD
        + (output_tokens / 1_000_000) * OUTPUT_PRICE_PER_M_USD
    )


def derive_title_from_first_message(content: str, max_len: int = 50) -> str:
    """Auto-title a thread from the first user message — first line, truncated."""
    first_line = content.strip().split("\n", 1)[0]
    if len(first_line) <= max_len:
        return first_line
    return first_line[:max_len].rstrip() + "…"
