"""War Room MCP server — exposes the dashboard's state as MCP tools.

Wraps the JSON-API routes in app.py (/api/futures, /api/strategy/{slug}/run,
/api/vault/search, /api/active-positions) so other Claude sessions (work mac,
mobile, Claude Desktop) can query the War Room without going through the
browser UI.

Architecture: thin HTTP wrapper. The MCP server makes HTTP calls to
http://127.0.0.1:8765 — the War Room FastAPI server must be running. This
matches the "always-on dashboard" design from [[project-mac-mini-home-server]].

Why HTTP rather than direct module imports:
  - Single source of truth: browser UI and MCP go through the same code paths
    and reuse the dashboard's caching, locks, and subprocess management
  - Failure isolation: an MCP crash can't take down the dashboard, and a
    dashboard restart doesn't require restarting the MCP server
  - The MCP server stays small (~150 lines) and dependency-light

Registered with:
  claude mcp add --transport stdio --scope user war-room -- \\
    ~/.venvs/war_room/bin/python ~/Desktop/War\\ Room/mcp_server.py
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

# Allow override via env var if War Room ever runs on a different host/port.
WAR_ROOM_URL = os.getenv("WAR_ROOM_URL", "http://127.0.0.1:8765")
HTTP_TIMEOUT_SECONDS = 120.0  # generous: strategy runs can take ~15s + headroom

mcp = FastMCP("war-room")


# ---------------------------------------------------------------------------
# Tool 1: futures map (live quotes for all RH-supported micros)
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_futures_map(refresh: bool = False) -> dict[str, Any]:
    """Return live quotes for all 11 Robinhood-supported futures micros.

    Each contract includes: micro ticker (what you trade), full_symbol (Yahoo
    symbol used for the quote, since micros track full-size 1:1 on price),
    display_name, sector, last price, day change, day change %, and is_active
    flag (true for current open positions).

    Grouped by sector: Equity Indices (MES/MNQ/M2K/MYM), Energy (MCL/MNG),
    Metals (MGC/SIL/MHG), Crypto (MBT/MET). Source: Yahoo Finance, ~15-min
    delayed for equity/commodity futures, near-real-time for crypto.

    Args:
        refresh: If True, bypass the 30-second quote cache and force a fresh
                 Yahoo pull. Default False (use cache).
    """
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        r = await client.get(f"{WAR_ROOM_URL}/api/futures", params={"refresh": int(refresh)})
        r.raise_for_status()
        return r.json()


# ---------------------------------------------------------------------------
# Tool 2: active positions
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_active_positions() -> dict[str, Any]:
    """Return Xander's currently-open futures positions.

    Source: ~/Desktop/War Room/data/active_positions.json. This is the same
    list that drives the ★ highlight on the Futures Map dashboard. Read-only
    here — to update, edit the JSON file directly on the host machine.
    """
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        r = await client.get(f"{WAR_ROOM_URL}/api/active-positions")
        r.raise_for_status()
        return r.json()


# ---------------------------------------------------------------------------
# Tool 3: vault search (FTS5 BM25 over the Obsidian Second Brain)
# ---------------------------------------------------------------------------


@mcp.tool()
async def search_vault(query: str, top_n: int = 5, snippet_chars: int = 400) -> dict[str, Any]:
    """Full-text search across Xander's Second Brain Obsidian vault.

    Returns the top N matching notes ranked by BM25, each with name, vault path,
    rank score (lower = better in SQLite's bm25()), a snippet of the content,
    and an obsidian:// URL the user can click to open the note. Useful for
    answering questions like "what does Xander's vault say about X?" without
    having to read every file.

    For the full content of a specific note after finding it, the calling
    Claude session should do a regular filesystem read of the path. This tool
    is for FINDING relevant notes, not reading them in full.

    Args:
        query: Free-text search. Multi-word queries are OR'd at the token level
               (recall-favoring). Examples: "donchian", "BMW M3 carfax",
               "SAP misericordia".
        top_n: How many results to return (1-50). Default 5.
        snippet_chars: How many leading characters of each note to include in
                       the snippet. Default 400.
    """
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        r = await client.get(
            f"{WAR_ROOM_URL}/api/vault/search",
            params={"q": query, "n": top_n, "snippet_chars": snippet_chars},
        )
        r.raise_for_status()
        return r.json()


# ---------------------------------------------------------------------------
# Tool 4: run a trading strategy synchronously and return the score
# ---------------------------------------------------------------------------


@mcp.tool()
async def list_strategies() -> dict[str, Any]:
    """List the trading strategies registered in the War Room Trading tile.

    Returns slug, display name, description, configurable args (with defaults),
    and metadata about the most recent run (latest_output filename, age in
    seconds). Use the slug values as input to `run_strategy()`.

    Current strategies (as of 2026-05-25): vix-spy-regime, ma-stack,
    donchian-breakout, weighted-score (V3), weighted-score-v5 (Donchian + BB
    + CMF with ADX gate — the newest, recommended for fresh runs).
    """
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        r = await client.get(f"{WAR_ROOM_URL}/api/strategies")
        r.raise_for_status()
        return r.json()


@mcp.tool()
async def run_strategy(
    strategy_slug: str,
    ticker: str = "SPY",
    extra_args: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run a registered trading strategy synchronously and return its score / regime.

    Spawns the underlying strategy subprocess (yfinance pull + scoring + chart
    render), waits for completion (typical 5-15 seconds, hard cap ~120s), and
    returns:
      - status: "done" | "error" | "timeout"
      - summary: parsed score / regime / last bar / ADX / gate status
      - log: full subprocess stdout (for debugging or when the parser misses
        a strategy-specific field)
      - chart_filename + chart_url: the rendered PNG, fetchable from
        WAR_ROOM_URL/trading/output/{chart_filename}

    To see what strategies are available, call list_strategies() first.

    Args:
        strategy_slug: Strategy slug from list_strategies() (e.g.
                       "weighted-score-v5", "vix-spy-regime").
        ticker: Stock ticker to run against. Defaults to SPY. Some strategies
                (like vix-spy-regime) ignore this and always use SPY.
        extra_args: Optional dict of strategy-specific args, e.g.
                    {"donchian-weight": "0.5", "adx-threshold": "15"} for the
                    weighted-score-v5 strategy. Keys must match the arg names
                    from list_strategies().
    """
    body: dict[str, str] = {"ticker": ticker}
    if extra_args:
        body.update({str(k): str(v) for k, v in extra_args.items()})

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        r = await client.post(
            f"{WAR_ROOM_URL}/api/strategy/{strategy_slug}/run",
            json=body,
        )
        if r.status_code == 404:
            raise ValueError(
                f"Unknown strategy slug: {strategy_slug!r}. "
                "Call list_strategies() to see available slugs."
            )
        if r.status_code == 409:
            raise RuntimeError(
                f"Strategy {strategy_slug!r} is already running. "
                "Wait for the in-flight run to finish, then retry."
            )
        r.raise_for_status()
        return r.json()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    # stdio transport — Claude Code launches us as a subprocess and talks
    # over stdin/stdout. No HTTP server on the MCP side.
    mcp.run()
