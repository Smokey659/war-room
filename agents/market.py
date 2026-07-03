"""Tiny market-context helpers for the V2 Overview (VIX for the KPI strip /
regime panel). Cached so the Overview render never hammers yfinance."""
from __future__ import annotations

import time

import yfinance as yf

_CACHE: dict = {"ts": 0.0, "vix": None, "vix_chg": None}
_TTL_SECONDS = 300  # VIX for a status chip doesn't need to be fresher than 5 min


def get_vix() -> tuple[float | None, float | None]:
    """(last VIX, change vs prior close). (None, None) when unavailable —
    callers render an honest placeholder, never a fabricated number."""
    now = time.time()
    if now - _CACHE["ts"] < _TTL_SECONDS:
        return _CACHE["vix"], _CACHE["vix_chg"]
    try:
        v = yf.download("^VIX", period="5d", interval="1d", progress=False,
                        auto_adjust=False)["Close"].dropna()
        v = v.iloc[:, 0] if hasattr(v, "columns") else v
        vix = float(v.iloc[-1])
        chg = vix - float(v.iloc[-2]) if len(v) >= 2 else None
        _CACHE.update(ts=now, vix=vix, vix_chg=chg)
    except Exception:
        _CACHE.update(ts=now)  # cache the failure briefly too; avoid retry storms
    return _CACHE["vix"], _CACHE["vix_chg"]
