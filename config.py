"""Centralized config — env vars, paths, model choice.

Loads from `~/.config/war_room/.env` (canonical location, outside iCloud sync).
Override any value via environment variable at run time.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# ---- Locations ----
PROJECT_ROOT = Path(__file__).parent.resolve()
DATA_DIR = PROJECT_ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

ENV_PATH = Path.home() / ".config" / "war_room" / ".env"
if ENV_PATH.exists():
    # override=True so the canonical .env file wins over stray empty-string vars
    # in the parent shell (which silently broke boot when ANTHROPIC_API_KEY got
    # set to "" somewhere upstream).
    load_dotenv(ENV_PATH, override=True)

# ---- Deployment mode ----
# RENDER_MODE=1 -> hosted (Render): no vault on disk, no local subprocuns
# (X-Agent Playwright, strategy runners), no Anthropic key required. Local-only
# features render honest "LOCAL ONLY" states instead of crashing.
RENDER_MODE = os.getenv("RENDER_MODE", "0") == "1"

# ---- Required (local) / optional (hosted) ----
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
if not ANTHROPIC_API_KEY and not RENDER_MODE:
    raise RuntimeError(
        f"ANTHROPIC_API_KEY not set.\n"
        f"Create {ENV_PATH} with:\n\n"
        f"    ANTHROPIC_API_KEY=sk-ant-...\n"
    )

# ---- Auth (hosted) ----
# When set, every request must carry HTTP Basic auth with this password.
# Unset locally -> no auth (Tailscale is the local boundary).
WAR_ROOM_PASSWORD = os.getenv("WAR_ROOM_PASSWORD")

# ---- Vault ----
VAULT_PATH = Path(
    os.getenv("VAULT_PATH", "/Users/xandernostrand/Desktop/Second Brain")
).expanduser().resolve()
if not VAULT_PATH.exists():
    if RENDER_MODE:
        # Hosted: no vault on this box. Point at an (empty) data-dir stub so the
        # indexer sees zero notes rather than crashing.
        VAULT_PATH = DATA_DIR / "vault_stub"
        VAULT_PATH.mkdir(exist_ok=True)
    else:
        raise RuntimeError(f"VAULT_PATH does not exist: {VAULT_PATH}")

# ---- Database ----
DB_PATH = DATA_DIR / "war_room.db"

# ---- Model ----
# Per Xander 2026-05-01: use Sonnet 4.6.
MODEL = os.getenv("WAR_ROOM_MODEL", "claude-sonnet-4-6")

# Pricing as of 2026-05-01 — verify current rates at https://www.anthropic.com/pricing
# Used by cost logger; stored per-message in SQLite.
INPUT_PRICE_PER_M_USD = float(os.getenv("INPUT_PRICE_PER_M_USD", "3.00"))
OUTPUT_PRICE_PER_M_USD = float(os.getenv("OUTPUT_PRICE_PER_M_USD", "15.00"))

# ---- Server ----
# Bind 0.0.0.0 so Tailscale can reach the dashboard from the SAP work laptop.
# Render injects PORT; it wins over WAR_ROOM_PORT when present.
HOST = os.getenv("WAR_ROOM_HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", os.getenv("WAR_ROOM_PORT", "8765")))

# ---- Retrieval ----
# Top-N vault notes to pull into Claude's context per query.
TOP_N_RESULTS = int(os.getenv("WAR_ROOM_TOP_N", "5"))
# Per-note content cap when building context (chars). Keeps token cost bounded.
# Notes longer than this get truncated; the citation link points to the full note in Obsidian.
PER_NOTE_CHAR_CAP = int(os.getenv("WAR_ROOM_PER_NOTE_CAP", "4000"))

# ---- Vault scan exclusions ----
# Folders inside the vault that should NOT be indexed (Obsidian internals, archive cruft).
EXCLUDED_DIRS = {".obsidian", ".trash", ".git", "node_modules", "__pycache__"}

# ---- X Agent integration ----
# Subprocess-based wrapper around `trending_main.py` and `main.py` in the X Agent project.
# Override paths via env if you ever move the project or change the venv location.
X_AGENT_PATH = Path(
    os.getenv("X_AGENT_PATH", "/Users/xandernostrand/Desktop/X Agent")
).expanduser()
X_AGENT_PYTHON = Path(
    os.getenv("X_AGENT_PYTHON", str(Path.home() / ".venvs/x_agent/bin/python"))
).expanduser()
X_AGENT_BRIEFS_DIR = X_AGENT_PATH / "briefs"
# Hard cap on a single brief-generation subprocess. If Playwright hangs (captcha, layout
# change, dead session), kill after this many seconds rather than letting the dashboard
# block forever.
X_AGENT_BRIEF_TIMEOUT_SECONDS = int(os.getenv("X_AGENT_BRIEF_TIMEOUT", "300"))

# ---- Trading integration ----
# Subprocess-based wrappers around the Trend Following project's strategy modules
# (`strategies/vix_spy_regime.py`, etc.). Same loose-coupling pattern as the X Agent.
TREND_FOLLOWING_PATH = Path(
    os.getenv("TREND_FOLLOWING_PATH", "/Users/xandernostrand/Desktop/Trend Following")
).expanduser()
TREND_FOLLOWING_PYTHON = Path(
    os.getenv("TREND_FOLLOWING_PYTHON", str(Path.home() / ".venvs/trend_following/bin/python"))
).expanduser()
# Where the dashboard saves rendered charts. Outside the project tree so they get cleaned
# up easily; the route /trading/output/{filename} serves them back.
TRADING_OUTPUT_DIR = DATA_DIR / "trading_outputs"
TRADING_OUTPUT_DIR.mkdir(exist_ok=True)
# Strategy subprocesses (matplotlib + yfinance + pandas) are CPU-bound — should finish in
# a few seconds. If a yfinance download hangs, kill after this many seconds.
TRADING_RUN_TIMEOUT_SECONDS = int(os.getenv("TRADING_RUN_TIMEOUT", "120"))
