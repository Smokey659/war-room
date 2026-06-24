"""Subprocess wrapper around the X Agent project's `trending_main.py`.

Loose coupling pattern: the dashboard never imports X Agent code. It only invokes
the existing scripts using the X Agent's own venv. That means:
  - The X Agent codebase stays editable; refinements appear automatically on the
    next brief run.
  - Dashboard can keep using its own venv (war_room) without dependency conflicts.
  - If the X Agent CLI shape changes, the dashboard breaks at the call site only —
    fixable in one place here.

This module:
  - Generates today's brief by running `trending_main.py` (subprocess, streamed).
  - Lists past briefs from `briefs/` folder.
  - Reads a single brief file by date.
  - Prevents concurrent runs (Playwright would crash on shared user-data-dir).
  - Enforces a timeout so a hung Playwright session doesn't block the dashboard.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import AsyncIterator

from config import (
    X_AGENT_BRIEF_TIMEOUT_SECONDS,
    X_AGENT_BRIEFS_DIR,
    X_AGENT_PATH,
    X_AGENT_PYTHON,
)


# ---------------------------------------------------------------------------
# Account registry — what the dashboard's account picker shows
# ---------------------------------------------------------------------------

# Each account maps a dashboard display name to the X agent's `--account` CLI flag.
# `account_arg = None` means "don't pass --account" (uses the X agent's 'default'
# session, which historically maps to SmokeySnipe — see the X agent main.py PROMPTS dict).
ACCOUNTS: list[dict] = [
    {
        "key": "smokeysnipe",
        "display_name": "SmokeySnipe",
        "account_arg": None,  # uses X agent's 'default' → existing browser_session/
        "description": "Original account. Uses the existing logged-in browser session.",
    },
    {
        "key": "bullishbytes777",
        "display_name": "BullishBytes777",
        "account_arg": "BullishBytes777",  # creates browser_session_BullishBytes777/
        "description": "New account. First run will require manual login in the Playwright browser window.",
    },
]


def get_account(key: str) -> dict | None:
    """Look up an account by its lowercase key. Returns None if unknown."""
    for a in ACCOUNTS:
        if a["key"] == key:
            return a
    return None


# ---------------------------------------------------------------------------
# Run lock — single global slot. Brief OR replies, not both at once.
# Reason: both workflows use Playwright + the X agent's persistent browser_session,
# and concurrent runs would crash on shared user-data-dir locks.
# ---------------------------------------------------------------------------

_run_lock = asyncio.Lock()


@dataclass
class AgentRun:
    """Tracks a single in-flight X-agent subprocess (brief OR reply scrape)."""
    kind: str            # 'brief' | 'replies'
    started_at: float
    status: str          # 'running' | 'done' | 'error' | 'timeout'
    output_buffer: str = ""
    account: str | None = None  # account display_name for replies; None for brief
    error_message: str | None = None


# Single in-flight run, regardless of kind. None when nothing is running.
_active_run: AgentRun | None = None


# Backwards-compat alias — older import sites use BriefRun.
BriefRun = AgentRun


# ---------------------------------------------------------------------------
# Brief I/O
# ---------------------------------------------------------------------------


def list_briefs() -> list[dict]:
    """Return all past briefs (date, path) sorted newest first."""
    if not X_AGENT_BRIEFS_DIR.exists():
        return []
    rows = []
    for p in sorted(X_AGENT_BRIEFS_DIR.glob("*.md"), reverse=True):
        try:
            d = date.fromisoformat(p.stem)
        except ValueError:
            # Filename isn't an ISO date — skip (probably a stray file)
            continue
        rows.append({"date": d.isoformat(), "path": p, "size": p.stat().st_size})
    return rows


def read_brief(d: str) -> str | None:
    """Return the markdown body of a brief by ISO date string. None if missing."""
    p = X_AGENT_BRIEFS_DIR / f"{d}.md"
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8", errors="replace")


def today_brief_exists() -> bool:
    """Quick check used by the page renderer to decide which CTA to show."""
    today = date.today().isoformat()
    return (X_AGENT_BRIEFS_DIR / f"{today}.md").exists()


def today_brief_date() -> str:
    return date.today().isoformat()


# ---------------------------------------------------------------------------
# Subprocess streaming
# ---------------------------------------------------------------------------


def is_run_in_progress() -> bool:
    """True if any X-agent subprocess (brief or replies) is currently running."""
    return _active_run is not None and _active_run.status == "running"


# Backwards-compat alias — used by app.py + the brief-streaming code path.
def is_brief_running() -> bool:
    return is_run_in_progress()


def get_active_run() -> AgentRun | None:
    return _active_run


def clear_active_run() -> None:
    """Reset the in-memory run state. Called by the POST endpoints when the user
    explicitly requests a (re)generation or new reply scrape, so the next SSE
    connection actually starts a fresh subprocess (rather than the SSE-auto-reconnect
    guard short-circuiting against the previous done-state)."""
    global _active_run
    if _active_run is not None and _active_run.status == "running":
        # Don't clobber an in-flight run — that would cause a duplicate spawn.
        return
    _active_run = None


async def _stream_subprocess(
    *,
    kind: str,
    cmd: list[str],
    timeout_seconds: int,
    account_display_name: str | None = None,
    timeout_hint: str = "Likely Playwright hung on a captcha or X.com layout change.",
) -> AsyncIterator[str]:
    """Generic streaming-subprocess helper. Used by both brief generation and reply scraping.

    Acquires the global run lock at start; releases on completion. Sets `_active_run`
    so the SSE endpoint can short-circuit on auto-reconnect. Yields plain-text chunks;
    caller HTML-escapes for SSE.
    """
    global _active_run

    if _run_lock.locked():
        raise RuntimeError(f"An X agent run ({kind}) is already in progress")

    async with _run_lock:
        _active_run = AgentRun(
            kind=kind, started_at=time.time(), status="running",
            account=account_display_name,
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(X_AGENT_PATH),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                # Force unbuffered output so we get streaming line-by-line.
                env={"PYTHONUNBUFFERED": "1", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

            try:
                async with asyncio.timeout(timeout_seconds):
                    while True:
                        line = await proc.stdout.readline()
                        if not line:
                            break
                        text = line.decode("utf-8", errors="replace")
                        _active_run.output_buffer += text
                        yield text
                    await proc.wait()

                if proc.returncode == 0:
                    _active_run.status = "done"
                else:
                    _active_run.status = "error"
                    _active_run.error_message = (
                        f"subprocess exited with code {proc.returncode}"
                    )
                    yield f"\n[exit code: {proc.returncode}]\n"

            except TimeoutError:
                proc.kill()
                await proc.wait()
                _active_run.status = "timeout"
                _active_run.error_message = (
                    f"Process killed after {timeout_seconds}s timeout"
                )
                yield (
                    f"\n[TIMEOUT — killed after {timeout_seconds}s. "
                    f"{timeout_hint} Check the X Agent's browser session manually.]\n"
                )

        except Exception as exc:
            if _active_run:
                _active_run.status = "error"
                _active_run.error_message = str(exc)
            yield f"\n[ERROR — {exc}]\n"
            raise


async def stream_brief_generation() -> AsyncIterator[str]:
    """Run `trending_main.py` and yield each stdout chunk as it arrives."""
    cmd = [str(X_AGENT_PYTHON), "trending_main.py"]
    async for chunk in _stream_subprocess(
        kind="brief",
        cmd=cmd,
        timeout_seconds=X_AGENT_BRIEF_TIMEOUT_SECONDS,
    ):
        yield chunk


async def stream_reply_scrape(account_key: str) -> AsyncIterator[str]:
    """Run `python main.py scrape` for the selected account; yield stdout chunks.

    `account_key` matches one of the entries in ACCOUNTS. Resolves to the
    `--account <name>` flag (or no flag for the default account).
    """
    account = get_account(account_key)
    if account is None:
        raise ValueError(f"Unknown account key: {account_key!r}")

    cmd = [str(X_AGENT_PYTHON), "main.py", "scrape"]
    if account["account_arg"] is not None:
        cmd.extend(["--account", account["account_arg"]])

    # Reply scrape is potentially longer than the brief (60 posts vs. one synthesis call).
    # Use the same timeout for now; bump if we see real-world hits.
    async for chunk in _stream_subprocess(
        kind="replies",
        cmd=cmd,
        timeout_seconds=X_AGENT_BRIEF_TIMEOUT_SECONDS,
        account_display_name=account["display_name"],
        timeout_hint=(
            "Likely Playwright hung on a captcha, login challenge, or X.com layout "
            f"change. If first run on {account['display_name']!r}, you may need to "
            "log in manually in the Playwright browser window."
        ),
    ):
        yield chunk


# ---------------------------------------------------------------------------
# Status for the tile + page header
# ---------------------------------------------------------------------------


def tile_status() -> str:
    """Short human-readable status string for the dashboard tile."""
    if is_run_in_progress():
        if _active_run.kind == "replies":
            who = f" ({_active_run.account})" if _active_run.account else ""
            return f"Scraping replies{who}…"
        return "Generating brief…"
    if today_brief_exists():
        return f"Brief ready ({today_brief_date()})"
    briefs = list_briefs()
    if briefs:
        return f"Last brief: {briefs[0]['date']}"
    return "Ready"
