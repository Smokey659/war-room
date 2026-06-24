"""Walk the Obsidian vault, extract markdown content, write into the FTS5 `notes` table.

Strategy for v1:
  - Index whole files (no chunking). Vault is small (~75 .md files, <5MB).
  - Strip YAML frontmatter from indexed content (it's noise for retrieval; metadata).
  - Re-index = drop the FTS5 table contents and re-walk. Fast on a small vault.
  - Excluded folders per config.EXCLUDED_DIRS (Obsidian internals, etc.).

When the vault grows past the size where whole-file indexing stops working, swap this
module's internals for a chunking + embedding strategy. The interface (`reindex_vault`,
returning a count) stays the same.
"""

from __future__ import annotations

import time
from pathlib import Path

import frontmatter

from config import EXCLUDED_DIRS, VAULT_PATH
from conversations.db import conn_ctx


def _iter_markdown_files(root: Path) -> list[Path]:
    """Walk root, yielding every .md file outside excluded folders."""
    out: list[Path] = []
    for p in root.rglob("*.md"):
        # Skip if any parent dir is excluded
        if any(part in EXCLUDED_DIRS for part in p.relative_to(root).parts):
            continue
        out.append(p)
    return out


def _read_for_index(path: Path) -> tuple[str, str]:
    """Return (display_name, indexable_content) for a markdown file.

    Strips YAML frontmatter; keeps the body. Display name is the file stem.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        post = frontmatter.loads(text)
        body = post.content
    except Exception:
        # If frontmatter parsing fails, index the full text — better than dropping.
        body = text
    return path.stem, body


def reindex_vault() -> dict:
    """Drop and rebuild the FTS5 index over the vault. Returns stats."""
    started = time.time()

    files = _iter_markdown_files(VAULT_PATH)

    with conn_ctx() as conn:
        # FTS5 doesn't support TRUNCATE; DELETE is the right move.
        conn.execute("DELETE FROM notes")
        for p in files:
            try:
                name, body = _read_for_index(p)
            except Exception as exc:
                # Log to stderr-style; don't crash on a bad file.
                print(f"[indexer] skip {p}: {exc}")
                continue
            rel_path = str(p.relative_to(VAULT_PATH))
            mtime = int(p.stat().st_mtime)
            conn.execute(
                "INSERT INTO notes (path, name, content, last_modified) VALUES (?, ?, ?, ?)",
                (rel_path, name, body, mtime),
            )

    elapsed = time.time() - started
    return {
        "indexed": len(files),
        "elapsed_seconds": round(elapsed, 3),
        "vault_path": str(VAULT_PATH),
    }


def index_size() -> int:
    """How many notes are currently indexed."""
    with conn_ctx() as conn:
        return conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
