"""Subprocess wrapper around the Trend Following project's strategy modules.

Loose coupling pattern (same as agents/x_agent.py): the dashboard never imports
strategy code. It only invokes the existing scripts via the trend_following venv.
That means:
  - Strategy code stays editable in `~/Desktop/Trend Following/` — refinements
    appear automatically on the next dashboard run.
  - Dashboard keeps using its own venv (war_room) without dependency conflicts.
  - If a strategy CLI shape changes, the dashboard breaks at the call site only —
    fixable in one place here (the STRATEGIES registry).

This module:
  - Maintains a registry of trading strategies that produce visual chart outputs.
  - Runs a strategy as a subprocess with `--save <out_path>` to capture the PNG.
  - Streams stdout via SSE while the subprocess runs.
  - Caches the most recent output per strategy so re-opening the tile shows the
    last result without re-running.
  - Prevents concurrent runs of the SAME strategy (matplotlib processes can fight
    over the figure backend if multiple are active). Different strategies can run
    in parallel — they have no shared resource.

NOT in v1:
  - Live trade execution. Read/visualize only. Standing rule per Stream 3 gating.
  - Generic "any python script" runner. Each strategy is explicitly registered.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator

from config import (
    TRADING_OUTPUT_DIR,
    TRADING_RUN_TIMEOUT_SECONDS,
    TREND_FOLLOWING_PATH,
    TREND_FOLLOWING_PYTHON,
)


# ---------------------------------------------------------------------------
# Strategy registry — what the Trading tile shows
# ---------------------------------------------------------------------------
# Each entry registers ONE strategy. To add a new strategy:
#   1. Add an entry below.
#   2. (Optional) add a UI for the strategy's args in templates/trading.html.
# That's it — routes + streaming + image display work generically.

STRATEGIES: list[dict] = [
    {
        "slug": "vix-spy-regime",
        "display_name": "VIX/SPY Regime",
        "icon": "📊",
        "description": (
            "VIX vs 30-EMA(VIX) regime filter. Bullish when VIX < EMA, bearish when "
            "VIX > EMA. SPY line color-coded by regime — glance at the chart, see the "
            "regime as a color band."
        ),
        # `python -m <module>` invocation — relative to TREND_FOLLOWING_PATH cwd
        "module": "strategies.vix_spy_regime",
        # User-configurable args exposed in the page form. Each entry becomes a form input.
        # The handler resolves them into `--<name> <value>` flags appended to the command.
        "args": [
            {
                "name": "start",
                "label": "Start date",
                "type": "date",
                "default": "",          # blank = strategy default (2-yr window)
                "placeholder": "YYYY-MM-DD",
                "help": "Optional. Default = 2 years back.",
            },
            {
                "name": "end",
                "label": "End date",
                "type": "date",
                "default": "",          # blank = today
                "placeholder": "YYYY-MM-DD",
                "help": "Optional. Default = today.",
            },
        ],
    },
    {
        "slug": "ma-stack",
        "display_name": "MA Stack (20 / 50 / 200 SMA)",
        "icon": "📚",
        "description": (
            "O'Neil / CAN SLIM multi-timeframe trend filter using three SIMPLE moving averages. "
            "BULLISH when Close > 20-SMA > 50-SMA > 200-SMA (the \"perfect stack\"). BEARISH when "
            "Close < 20-SMA < 50-SMA < 200-SMA (inverse stack). NEUTRAL otherwise. "
            "Slower to flip than a single MA crossover — that's the point: requires confirmation "
            "across three timeframes, ignores chop. See [[can-slim-methodology]] for the "
            "O'Neil-flavored framing."
        ),
        "module": "strategies.ma_stack",
        "args": [
            {
                "name": "ticker",
                "label": "Ticker",
                "type": "text",
                "default": "NVDA",
                "placeholder": "e.g. NVDA, AAPL, SPY",
                "help": "Required for chart output. Leave defaults to see the perfect stack on NVDA.",
            },
            {
                "name": "start",
                "label": "Start date",
                "type": "date",
                "default": "",
                "placeholder": "YYYY-MM-DD",
                "help": "Optional. Default = 3 years back (200-MA needs warmup).",
            },
            {
                "name": "end",
                "label": "End date",
                "type": "date",
                "default": "",
                "placeholder": "YYYY-MM-DD",
                "help": "Optional. Default = today.",
            },
        ],
    },
    {
        "slug": "donchian-breakout",
        "display_name": "Donchian Breakout",
        "icon": "📐",
        "description": (
            "Turtle Traders System 1 — 20-day high entry / 10-day low exit, applied to a single "
            "stock. Long when most recent breakout was a 20-day high close; flat when it was a "
            "10-day low close. Asymmetric windows keep you in winners longer than they force "
            "you out of losers. (Dennis: \"trade with the trend until proven otherwise.\")"
        ),
        "module": "strategies.donchian_breakout",
        "args": [
            {
                "name": "ticker",
                "label": "Ticker",
                "type": "text",
                "default": "NVDA",
                "placeholder": "e.g. NVDA, AAPL, SPY",
                "help": "Required for chart output. Leave defaults to see Turtle System 1 on NVDA.",
            },
            {
                "name": "start",
                "label": "Start date",
                "type": "date",
                "default": "",
                "placeholder": "YYYY-MM-DD",
                "help": "Optional. Default = 2 years back.",
            },
            {
                "name": "end",
                "label": "End date",
                "type": "date",
                "default": "",
                "placeholder": "YYYY-MM-DD",
                "help": "Optional. Default = today.",
            },
            {
                "name": "entry-window",
                "label": "Entry window",
                "type": "number",
                "default": "20",
                "placeholder": "20",
                "help": "Days. Turtle rule = 20.",
            },
            {
                "name": "exit-window",
                "label": "Exit window",
                "type": "number",
                "default": "10",
                "placeholder": "10",
                "help": "Days. Turtle rule = 10. Asymmetric vs entry is the point.",
            },
        ],
    },
    {
        "slug": "weighted-score",
        "display_name": "Weighted Indicator Score",
        "icon": "⚖️",
        "description": (
            "Composite indicator score on a 1–5 scale (1: Very Bearish, 5: Very Bullish). "
            "Combines RSI, MACD, MA Stack, Bollinger Bands, Chaikin Money Flow (CMF), and ADX using user-defined weights. "
            "Glance at the price chart color-coded by composite score, and track the historical scoring trend below."
        ),
        "module": "strategies.weighted_score_v3",
        "args": [
            {
                "name": "ticker",
                "label": "Ticker",
                "type": "text",
                "default": "AAPL",
                "placeholder": "e.g. AAPL, NVDA, SPY",
                "help": "Stock ticker symbol.",
            },
            {
                "name": "start",
                "label": "Start date",
                "type": "date",
                "default": "",
                "placeholder": "YYYY-MM-DD",
                "help": "Optional. Default = 2 years back.",
            },
            {
                "name": "end",
                "label": "End date",
                "type": "date",
                "default": "",
                "placeholder": "YYYY-MM-DD",
                "help": "Optional. Default = today.",
            },
            {
                "name": "rsi-weight",
                "label": "RSI Weight",
                "type": "number",
                "default": "0.3",
                "placeholder": "0.3",
                "help": "Relative weight of RSI (momentum).",
            },
            {
                "name": "macd-weight",
                "label": "MACD Weight",
                "type": "number",
                "default": "0.25",
                "placeholder": "0.25",
                "help": "Relative weight of MACD (crossover/trend).",
            },
            {
                "name": "ma-weight",
                "label": "MA Stack Weight",
                "type": "number",
                "default": "0.25",
                "placeholder": "0.25",
                "help": "Relative weight of SMA Stack (structural trend).",
            },
            {
                "name": "bb-weight",
                "label": "Bollinger Bands Weight",
                "type": "number",
                "default": "0.2",
                "placeholder": "0.2",
                "help": "Relative weight of Bollinger Bands (%B / volatility-adjusted price position).",
            },
            {
                "name": "cmf-weight",
                "label": "CMF Weight",
                "type": "number",
                "default": "0.15",
                "placeholder": "0.15",
                "help": "Relative weight of Chaikin Money Flow (volume).",
            },
            {
                "name": "adx-weight",
                "label": "ADX Weight",
                "type": "number",
                "default": "0.15",
                "placeholder": "0.15",
                "help": "Relative weight of ADX (trend strength).",
            },
        ],
    },
    {
        "slug": "weighted-score-v5",
        "display_name": "Weighted Score V5 (Donchian + BB + CMF, ADX Gated)",
        "icon": "🧪",
        "description": (
            "Refined composite score — drops RSI / MACD / MA Stack from V4. Three directional indicators on a 1–5 scale: "
            "Donchian Breakout (20-day high/low channel — primary breakout signal), Bollinger Bands %B (mean-reversion counterweight), "
            "and Chaikin Money Flow (volume confirmation). ADX hard gate preserved from V4: regime forced to Neutral (0) when ADX is below the threshold."
        ),
        "module": "strategies.weighted_score_v5",
        "args": [
            {
                "name": "ticker",
                "label": "Ticker",
                "type": "text",
                "default": "SPY",
                "placeholder": "e.g. SPY, NVDA, AAPL",
                "help": "Stock ticker symbol.",
            },
            {
                "name": "start",
                "label": "Start date",
                "type": "date",
                "default": "",
                "placeholder": "YYYY-MM-DD",
                "help": "Optional. Default = 2 years back.",
            },
            {
                "name": "end",
                "label": "End date",
                "type": "date",
                "default": "",
                "placeholder": "YYYY-MM-DD",
                "help": "Optional. Default = today.",
            },
            {
                "name": "donchian-weight",
                "label": "Donchian Weight",
                "type": "number",
                "default": "0.4",
                "placeholder": "0.4",
                "help": "Relative weight of Donchian Breakout (20-day channel — primary breakout signal).",
            },
            {
                "name": "bb-weight",
                "label": "Bollinger Bands Weight",
                "type": "number",
                "default": "0.3",
                "placeholder": "0.3",
                "help": "Relative weight of Bollinger Bands %B (mean-reversion counterweight).",
            },
            {
                "name": "cmf-weight",
                "label": "CMF Weight",
                "type": "number",
                "default": "0.3",
                "placeholder": "0.3",
                "help": "Relative weight of Chaikin Money Flow (volume).",
            },
            {
                "name": "adx-threshold",
                "label": "ADX Gate Threshold",
                "type": "number",
                "default": "12.0",
                "placeholder": "12.0",
                "help": "If ADX is below this, signal is gated to Neutral. Optimizer-suggested default = 12 (V4 used 20 conventionally). Set to 0.0 to disable the gate.",
            },
        ],
    },
]


def get_strategy(slug: str) -> dict | None:
    """Look up a strategy by slug. Returns None if unknown."""
    for s in STRATEGIES:
        if s["slug"] == slug:
            return s
    return None


# ---------------------------------------------------------------------------
# Per-strategy run state
# ---------------------------------------------------------------------------


@dataclass
class StrategyRun:
    """Tracks a single in-flight strategy subprocess."""
    slug: str
    started_at: float
    status: str                    # 'running' | 'done' | 'error' | 'timeout'
    output_buffer: str = ""
    output_image: str | None = None  # filename in TRADING_OUTPUT_DIR (no path prefix)
    error_message: str | None = None
    args_resolved: list[str] = field(default_factory=list)  # args passed to subprocess


# Per-strategy run state. Different strategies can run concurrently (no shared
# resource), so we track them independently keyed by slug.
_active_runs: dict[str, StrategyRun] = {}
_strategy_locks: dict[str, asyncio.Lock] = {}


def _lock_for(slug: str) -> asyncio.Lock:
    """Return (creating if needed) the per-strategy lock."""
    if slug not in _strategy_locks:
        _strategy_locks[slug] = asyncio.Lock()
    return _strategy_locks[slug]


def is_strategy_running(slug: str) -> bool:
    run = _active_runs.get(slug)
    return run is not None and run.status == "running"


def get_active_run(slug: str) -> StrategyRun | None:
    return _active_runs.get(slug)


def clear_active_run(slug: str) -> None:
    """Reset state so the next SSE connection actually runs a fresh subprocess
    (rather than the auto-reconnect guard short-circuiting against done state)."""
    run = _active_runs.get(slug)
    if run is not None and run.status == "running":
        # Don't clobber an in-flight run.
        return
    _active_runs.pop(slug, None)


# ---------------------------------------------------------------------------
# Output image management
# ---------------------------------------------------------------------------


def output_path_for(slug: str) -> tuple[str, Path]:
    """Generate a unique output path for a strategy run.

    Returns (filename, full_path). Filename includes a timestamp so old runs
    don't get clobbered (the latest is always the most recent). Filenames are
    served back via /trading/output/{filename}.
    """
    timestamp = int(time.time())
    filename = f"{slug}-{timestamp}.png"
    return filename, TRADING_OUTPUT_DIR / filename


def latest_output(slug: str) -> str | None:
    """Return the filename of the most recent output for a strategy, or None."""
    matches = sorted(TRADING_OUTPUT_DIR.glob(f"{slug}-*.png"), reverse=True)
    return matches[0].name if matches else None


def latest_output_age_seconds(slug: str) -> int | None:
    """Age in seconds of the most recent output. None if no output exists."""
    latest = latest_output(slug)
    if latest is None:
        return None
    return int(time.time() - (TRADING_OUTPUT_DIR / latest).stat().st_mtime)


# ---------------------------------------------------------------------------
# Subprocess streaming
# ---------------------------------------------------------------------------


async def stream_strategy_run(
    slug: str,
    user_args: dict[str, str] | None = None,
) -> AsyncIterator[str]:
    """Run a strategy subprocess and yield each stdout chunk as it arrives.

    `user_args` is a dict of form-input values keyed by arg name. Empty values
    are skipped (let the strategy use its default).

    Subprocess: `python -m <module> --save <output_path> [--<arg> <value>...]`
    Output PNG goes to TRADING_OUTPUT_DIR/{slug}-{timestamp}.png and is recorded
    on the StrategyRun so the SSE handler can swap an <img> in on done.
    """
    strategy = get_strategy(slug)
    if strategy is None:
        raise ValueError(f"Unknown strategy: {slug!r}")

    lock = _lock_for(slug)
    if lock.locked():
        raise RuntimeError(f"Strategy {slug!r} is already running")

    user_args = user_args or {}
    output_filename, output_path = output_path_for(slug)

    # Build command: python -m <module> --save <out> [--<arg> <value>...]
    cmd: list[str] = [
        str(TREND_FOLLOWING_PYTHON),
        "-W", "ignore",
        "-m", strategy["module"],
        "--save", str(output_path),
    ]
    args_resolved = ["--save", str(output_path)]
    for arg_def in strategy.get("args", []):
        name = arg_def["name"]
        value = (user_args.get(name) or "").strip()
        if value:
            cmd.extend([f"--{name}", value])
            args_resolved.extend([f"--{name}", value])

    async with lock:
        run = StrategyRun(
            slug=slug,
            started_at=time.time(),
            status="running",
            output_image=output_filename,
            args_resolved=args_resolved,
        )
        _active_runs[slug] = run

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(TREND_FOLLOWING_PATH),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env={
                    "PYTHONUNBUFFERED": "1",
                    # Force matplotlib's headless backend — subprocess has no display.
                    "MPLBACKEND": "Agg",
                    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                },
            )

            try:
                async with asyncio.timeout(TRADING_RUN_TIMEOUT_SECONDS):
                    while True:
                        line = await proc.stdout.readline()
                        if not line:
                            break
                        text = line.decode("utf-8", errors="replace")
                        run.output_buffer += text
                        yield text
                    await proc.wait()

                if proc.returncode == 0 and output_path.exists():
                    run.status = "done"
                elif proc.returncode == 0:
                    run.status = "error"
                    run.error_message = "subprocess exited cleanly but no PNG was written"
                    run.output_image = None
                    yield "\n[ERROR — no chart file produced]\n"
                else:
                    run.status = "error"
                    run.error_message = f"subprocess exited with code {proc.returncode}"
                    run.output_image = None
                    yield f"\n[exit code: {proc.returncode}]\n"

            except TimeoutError:
                proc.kill()
                await proc.wait()
                run.status = "timeout"
                run.error_message = f"Process killed after {TRADING_RUN_TIMEOUT_SECONDS}s timeout"
                run.output_image = None
                yield (
                    f"\n[TIMEOUT — killed after {TRADING_RUN_TIMEOUT_SECONDS}s. "
                    f"Likely yfinance download hung or matplotlib stalled.]\n"
                )

        except Exception as exc:
            run.status = "error"
            run.error_message = str(exc)
            run.output_image = None
            yield f"\n[ERROR — {exc}]\n"
            raise


# ---------------------------------------------------------------------------
# Status for the tile
# ---------------------------------------------------------------------------


def tile_status() -> str:
    """Short human-readable status for the dashboard tile.

    Reports state of any in-flight strategy, otherwise the most-recent output age.
    """
    running = [s for s in STRATEGIES if is_strategy_running(s["slug"])]
    if running:
        names = ", ".join(s["display_name"] for s in running)
        return f"Running: {names}"
    # Find the freshest output across all strategies
    freshest_age = None
    freshest_name = None
    for s in STRATEGIES:
        age = latest_output_age_seconds(s["slug"])
        if age is None:
            continue
        if freshest_age is None or age < freshest_age:
            freshest_age = age
            freshest_name = s["display_name"]
    if freshest_age is None:
        return "Ready"
    if freshest_age < 3600:
        return f"Last: {freshest_name} ({freshest_age // 60}m ago)"
    if freshest_age < 86400:
        return f"Last: {freshest_name} ({freshest_age // 3600}h ago)"
    return f"Last: {freshest_name} ({freshest_age // 86400}d ago)"
