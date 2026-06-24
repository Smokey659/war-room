"""BM25 search over the vault FTS5 index.

Given a user query, return the top-N most-relevant vault notes. Caller passes them
to Claude as context.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from config import PER_NOTE_CHAR_CAP, TOP_N_RESULTS
from conversations.db import conn_ctx


@dataclass
class SearchResult:
    path: str       # vault-relative path
    name: str       # filename stem
    content: str    # full body, possibly truncated
    rank: float     # BM25 rank (lower = better in SQLite's bm25())
    truncated: bool


def _sanitize_query(raw: str) -> str:
    """Make a user query safe for FTS5 MATCH.

    Strategy: tokenize on whitespace, strip non-alphanumeric chars from each token,
    drop empties, and OR them together. This is the most permissive interpretation —
    we're not trying to do precise boolean queries, we want recall.
    """
    tokens = re.findall(r"[A-Za-z0-9_]+", raw)
    if not tokens:
        return ""
    # Quote each token to escape any FTS5 syntax weirdness, then OR them.
    quoted = [f'"{t}"' for t in tokens]
    return " OR ".join(quoted)


def search(query: str, top_n: int = TOP_N_RESULTS) -> list[SearchResult]:
    """Return up to top_n vault notes ranked by BM25.

    Empty results = either no matching notes, or the query sanitized to nothing
    (e.g., user typed only punctuation). Caller decides how to handle.
    """
    fts_query = _sanitize_query(query)
    if not fts_query:
        return []

    results: list[SearchResult] = []
    with conn_ctx() as conn:
        rows = conn.execute(
            """SELECT path, name, content, bm25(notes) AS rank
               FROM notes
               WHERE notes MATCH ?
               ORDER BY rank
               LIMIT ?""",
            (fts_query, top_n),
        ).fetchall()

    for r in rows:
        body = r["content"] or ""
        truncated = len(body) > PER_NOTE_CHAR_CAP
        if truncated:
            body = body[:PER_NOTE_CHAR_CAP] + "\n\n[…truncated; full note in Obsidian]"
        results.append(
            SearchResult(
                path=r["path"],
                name=r["name"],
                content=body,
                rank=float(r["rank"]),
                truncated=truncated,
            )
        )
    return results
