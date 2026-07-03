"""Portfolio snapshot loader — the V2 "one real gap" (design/README.md).

Data source: `data/holdings.json`, written by a Claude session from the Robinhood
agentic MCP (which can read the full RH footprint). NOT a live API: the design
deliberately frames these panels as "SNAPSHOT <time> · MANUAL SYNC", and this
loader keeps that honest — it only ever reports what the last session wrote.

Shape of data/holdings.json:
{
  "as_of": "2026-07-02 14:30 ET",
  "account": {"label": "INDIVIDUAL ••••0871", "total_value": 2263.07,
               "cash": 1244.19, "buying_power": 1239.46},
  "holdings": [
    {"symbol": "ONDS", "name": "Ondas", "sector": "Drones / Wireless",
     "qty": 1, "avgCost": 2.64, "lastPrice": 7.77, "prevClose": 7.44},
    ...
  ]
}
"""
from __future__ import annotations

import json

from config import DATA_DIR

HOLDINGS_PATH = DATA_DIR / "holdings.json"


def load_portfolio() -> dict | None:
    """Load + derive. Returns None when no snapshot exists (panels render an
    honest empty state, never fabricated numbers)."""
    if not HOLDINGS_PATH.exists():
        return None
    try:
        raw = json.loads(HOLDINGS_PATH.read_text())
    except Exception:
        return None

    rows = []
    for h in raw.get("holdings", []):
        try:
            qty = float(h["qty"])
            avg = float(h["avgCost"])
            last = float(h["lastPrice"])
            prev = float(h.get("prevClose", last)) or last
        except (KeyError, TypeError, ValueError):
            continue
        cost = qty * avg
        mkt_val = qty * last
        pl = mkt_val - cost
        rows.append({
            "symbol": h.get("symbol", "?"),
            "name": h.get("name", ""),
            "sector": h.get("sector", ""),
            "qty": qty, "avg": avg, "last": last,
            "cost": cost, "mkt_val": mkt_val, "pl": pl,
            "pl_pct": (pl / cost * 100) if cost else 0.0,
            "day_usd": (last - prev) * qty,
            "day_pct": ((last / prev - 1) * 100) if prev else 0.0,
        })
    rows.sort(key=lambda r: -r["pl_pct"])

    acct = raw.get("account", {})
    total_mv = sum(r["mkt_val"] for r in rows)
    total_cost = sum(r["cost"] for r in rows)
    total_pl = total_mv - total_cost
    total_value = acct.get("total_value")
    cash = acct.get("cash")
    deployed_pct = (total_mv / total_value * 100) if total_value else None

    return {
        "as_of": raw.get("as_of", "unknown"),
        "label": acct.get("label", "ACCOUNT"),
        "total_value": total_value,
        "cash": cash,
        "buying_power": acct.get("buying_power"),
        "rows": rows,
        "n_positions": len(rows),
        "total_mv": total_mv,
        "total_cost": total_cost,
        "total_pl": total_pl,
        "total_pl_pct": (total_pl / total_cost * 100) if total_cost else 0.0,
        "day_pl": sum(r["day_usd"] for r in rows),
        "deployed_pct": deployed_pct,
    }
