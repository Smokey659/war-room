"""Futures quotes + sparklines for the Trading tile's Futures Map view.

Data source: Yahoo Finance via `yfinance`. Free, no auth, ~15-min delayed for
the equity/commodity futures; near-real-time for crypto. Sufficient for a
situational-awareness dashboard (NOT for execution decisions).

Universe: only Robinhood-supported micros (verified 2026-05-24). Micros track
their full-size symbols 1:1 on price, so quotes come from the full-size symbol
even though the trade is the micro contract.

In-process call rather than the subprocess pattern used by `agents/trading.py`:
- quotes need snappy refresh; spawning a subprocess adds 1-2s of latency
- `yfinance` is a single well-maintained dep; war_room owning it is fine
- the trading.py subprocess pattern is for matplotlib-rendered charts which
  benefit from process isolation and a separate venv. Quotes don't.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Optional

import yfinance as yf

from config import DATA_DIR


# ---------------------------------------------------------------------------
# Universe — Robinhood-supported micros only (verified 2026-05-24)
# ---------------------------------------------------------------------------
# `micro` is what we trade. `full` is the yfinance symbol we quote against
# (since micros track full-size 1:1 on price and Yahoo's micro coverage is
# unreliable). `sector` drives grid grouping in the template.

FUTURES_UNIVERSE: list[dict] = [
    # Equity indices
    {"micro": "MES", "full": "ES=F",   "display": "Micro S&P 500",      "sector": "Equity Indices"},
    {"micro": "MNQ", "full": "NQ=F",   "display": "Micro Nasdaq-100",   "sector": "Equity Indices"},
    {"micro": "M2K", "full": "RTY=F",  "display": "Micro Russell 2000", "sector": "Equity Indices"},
    {"micro": "MYM", "full": "YM=F",   "display": "Micro Dow",          "sector": "Equity Indices"},
    # Energy
    {"micro": "MCL", "full": "CL=F",   "display": "Micro WTI Crude",    "sector": "Energy"},
    {"micro": "MNG", "full": "NG=F",   "display": "Micro Natural Gas",  "sector": "Energy"},
    # Metals
    {"micro": "MGC", "full": "GC=F",   "display": "Micro Gold",         "sector": "Metals"},
    {"micro": "SIL", "full": "SI=F",   "display": "Micro Silver",       "sector": "Metals"},
    {"micro": "MHG", "full": "HG=F",   "display": "Micro Copper",       "sector": "Metals"},
    # Crypto
    {"micro": "MBT", "full": "BTC-USD","display": "Micro Bitcoin",      "sector": "Crypto"},
    {"micro": "MET", "full": "ETH-USD","display": "Micro Ether",        "sector": "Crypto"},
]

SECTOR_ORDER = ["Equity Indices", "Energy", "Metals", "Crypto"]


# ---------------------------------------------------------------------------
# Active positions config — editable JSON file
# ---------------------------------------------------------------------------
# Edit `data/active_positions.json` to flip the star highlight on the cards.
# Format: {"active": ["MHG", "MCL", ...]}

ACTIVE_POSITIONS_PATH = DATA_DIR / "active_positions.json"


def load_active_positions() -> set[str]:
    """Read active positions from the JSON config. Returns the set of MICRO tickers
    flagged as actively held. Missing/malformed file → empty set (no highlight).
    """
    if not ACTIVE_POSITIONS_PATH.exists():
        return set()
    try:
        data = json.loads(ACTIVE_POSITIONS_PATH.read_text())
        return set(data.get("active", []))
    except (json.JSONDecodeError, OSError):
        return set()


# ---------------------------------------------------------------------------
# In-memory quote cache — prevents Refresh-hammering from getting rate-limited
# ---------------------------------------------------------------------------

QUOTE_CACHE_TTL_SECONDS = 30
_QUOTE_CACHE: dict = {"timestamp": 0.0, "quotes": []}


# ---------------------------------------------------------------------------
# Sparkline — pure-SVG, no matplotlib, no extra deps
# ---------------------------------------------------------------------------

SPARK_WIDTH = 80
SPARK_HEIGHT = 22
SPARK_DAYS = 5


def _sparkline_svg(closes: list[float]) -> str:
    """Return an inline SVG <svg> for the price series.

    Uses `currentColor` for the stroke so green/red color cascades from the
    card's class (.up / .down). vector-effect=non-scaling-stroke keeps the
    line at 1.5px regardless of the viewBox normalization.
    """
    if not closes or len(closes) < 2:
        return ""
    lo, hi = min(closes), max(closes)
    rng = hi - lo
    n = len(closes)
    if rng == 0:
        # Flat line through the middle
        path = f"M 0 {SPARK_HEIGHT / 2:.1f} L {SPARK_WIDTH} {SPARK_HEIGHT / 2:.1f}"
    else:
        pts = []
        for i, c in enumerate(closes):
            x = (i / (n - 1)) * SPARK_WIDTH
            # SVG Y increases downward, so invert
            y = SPARK_HEIGHT - ((c - lo) / rng) * SPARK_HEIGHT
            pts.append(f"{x:.1f} {y:.1f}")
        path = "M " + " L ".join(pts)
    return (
        f'<svg viewBox="0 0 {SPARK_WIDTH} {SPARK_HEIGHT}" '
        f'width="{SPARK_WIDTH}" height="{SPARK_HEIGHT}" '
        'preserveAspectRatio="none" class="spark">'
        f'<path d="{path}" stroke="currentColor" stroke-width="1.5" '
        'fill="none" vector-effect="non-scaling-stroke"/>'
        "</svg>"
    )


# ---------------------------------------------------------------------------
# Quote DTO + fetch
# ---------------------------------------------------------------------------


@dataclass
class FuturesQuote:
    micro: str
    full: str
    display: str
    sector: str
    last: Optional[float]
    change: Optional[float]
    change_pct: Optional[float]
    spark_svg: Optional[str]
    is_active: bool
    error: Optional[str] = None

    @property
    def direction(self) -> str:
        """For CSS class: 'up' if positive, 'down' if negative, 'flat' if zero/none."""
        if self.change_pct is None:
            return "flat"
        if self.change_pct > 0:
            return "up"
        if self.change_pct < 0:
            return "down"
        return "flat"

    @property
    def price_display(self) -> str:
        """Human-formatted price. Smaller values get more decimal places."""
        if self.last is None:
            return "—"
        if self.last >= 1000:
            return f"{self.last:,.2f}"
        if self.last >= 10:
            return f"{self.last:,.2f}"
        return f"{self.last:.3f}"

    @property
    def change_pct_display(self) -> str:
        if self.change_pct is None:
            return "—"
        sign = "+" if self.change_pct > 0 else ""
        return f"{sign}{self.change_pct:.2f}%"


def fetch_quotes(force_refresh: bool = False) -> list[FuturesQuote]:
    """Fetch quotes + sparklines for the entire RH-supported universe.

    Batches into a single `yf.download()` call so it's one HTTP round trip.
    Cached for QUOTE_CACHE_TTL_SECONDS to defend against Refresh-spamming.
    On hard fetch failure, returns error-flagged quotes (per-symbol if possible,
    else uniformly).
    """
    now = time.time()
    if not force_refresh and (now - _QUOTE_CACHE["timestamp"]) < QUOTE_CACHE_TTL_SECONDS:
        return _QUOTE_CACHE["quotes"]

    active = load_active_positions()
    symbols = [f["full"] for f in FUTURES_UNIVERSE]

    quotes: list[FuturesQuote] = []
    data = None
    fetch_error: Optional[str] = None
    try:
        # Pull a few extra calendar days to ensure enough trading bars after
        # weekends/holidays. group_by='ticker' gives us a MultiIndex column structure.
        data = yf.download(
            tickers=" ".join(symbols),
            period=f"{SPARK_DAYS + 4}d",
            interval="1d",
            group_by="ticker",
            progress=False,
            auto_adjust=False,
            threads=True,
        )
    except Exception as exc:  # pragma: no cover — network failure path
        fetch_error = f"yfinance.download failed: {exc}"

    for f in FUTURES_UNIVERSE:
        sym = f["full"]
        is_active = f["micro"] in active
        try:
            if data is None or data.empty:
                raise ValueError(fetch_error or "no data returned")
            # Multi-symbol response → MultiIndex columns (ticker, field)
            if hasattr(data.columns, "levels") and sym in data.columns.levels[0]:
                df = data[sym].dropna(subset=["Close"])
            else:
                raise ValueError(f"symbol {sym} missing from response")
            if len(df) < 2:
                raise ValueError(f"insufficient bars for {sym}")
            closes = df["Close"].tolist()
            last = float(closes[-1])
            prev = float(closes[-2])
            change = last - prev
            change_pct = (change / prev * 100) if prev != 0 else 0.0
            spark = _sparkline_svg(closes[-SPARK_DAYS:])
            quotes.append(
                FuturesQuote(
                    micro=f["micro"],
                    full=f["full"],
                    display=f["display"],
                    sector=f["sector"],
                    last=last,
                    change=change,
                    change_pct=change_pct,
                    spark_svg=spark,
                    is_active=is_active,
                )
            )
        except Exception as exc:
            quotes.append(
                FuturesQuote(
                    micro=f["micro"],
                    full=f["full"],
                    display=f["display"],
                    sector=f["sector"],
                    last=None,
                    change=None,
                    change_pct=None,
                    spark_svg=None,
                    is_active=is_active,
                    error=str(exc),
                )
            )

    _QUOTE_CACHE["timestamp"] = now
    _QUOTE_CACHE["quotes"] = quotes
    return quotes


def quotes_by_sector(force_refresh: bool = False) -> list[tuple[str, list[FuturesQuote]]]:
    """Returns [(sector_name, [quote, ...]), ...] in canonical sector order.

    Template iterates over this list directly — avoids dict-order surprises in
    Jinja and keeps the visual layout deterministic.
    """
    quotes = fetch_quotes(force_refresh=force_refresh)
    by_sector: dict[str, list[FuturesQuote]] = {s: [] for s in SECTOR_ORDER}
    for q in quotes:
        if q.sector in by_sector:
            by_sector[q.sector].append(q)
    return [(s, by_sector[s]) for s in SECTOR_ORDER]


def cache_age_seconds() -> int:
    """How long ago the cache was last refreshed (for the 'Updated Xs ago' label)."""
    if _QUOTE_CACHE["timestamp"] == 0:
        return 0
    return int(time.time() - _QUOTE_CACHE["timestamp"])
