"""
derivatives_fetcher.py
======================
Fetches real-time derivatives data from Binance Futures public endpoints.
No API key required. Used by Hermes @derivatives-context skill.

Usage:
    python3 scripts/derivatives_fetcher.py             # full report (JSON)
    python3 scripts/derivatives_fetcher.py --summary   # human-readable summary
    python3 scripts/derivatives_fetcher.py --json      # raw JSON (for piping)

Endpoints used (all public, no auth):
    - globalLongShortAccountRatio  → who dominates: longs or shorts
    - topLongShortAccountRatio     → top 20% traders by margin
    - topLongShortPositionRatio    → top traders by position size
    - fundingRate                  → current funding rate
    - openInterest                 → total open interest in USDT
    - allForceOrders               → recent liquidations (last 100)
    - depth                        → order book (top walls)
"""

import requests
import json
import sys
import argparse
from datetime import datetime, timezone

BASE = "https://fapi.binance.com"
SYMBOL = "BTCUSDT"


def fetch(path: str, params: dict) -> dict | list | None:
    """GET request with basic error handling."""
    try:
        r = requests.get(f"{BASE}{path}", params=params, timeout=8)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": str(e)}


def get_long_short_ratios() -> dict:
    """
    Returns three L/S ratio datasets:
      - global_account  : all traders, account-level
      - top_account     : top 20% traders by margin, account-level
      - top_position    : top 20% traders, by position size
    Each has: longAccount, shortAccount, longShortRatio, timestamp
    """
    params = {"symbol": SYMBOL, "period": "5m", "limit": 1}
    return {
        "global_account": fetch("/futures/data/globalLongShortAccountRatio", params),
        "top_account":    fetch("/futures/data/topLongShortAccountRatio", params),
        "top_position":   fetch("/futures/data/topLongShortPositionRatio", params),
    }


def get_funding_rate() -> dict:
    """Current funding rate. Positive = longs pay shorts. Negative = shorts pay longs."""
    data = fetch("/fapi/v1/fundingRate", {"symbol": SYMBOL, "limit": 1})
    if isinstance(data, list) and data:
        return data[0]
    return data


def get_open_interest() -> dict:
    """Total open interest in contracts and USDT value."""
    return fetch("/fapi/v1/openInterest", {"symbol": SYMBOL})


def get_recent_liquidations() -> list:
    """
    Last 100 forced liquidation orders.
    Each has: symbol, side (SELL=long liq, BUY=short liq), price, origQty, time
    """
    data = fetch("/fapi/v1/allForceOrders", {"symbol": SYMBOL, "limit": 100})
    return data if isinstance(data, list) else []


def get_order_book_walls(depth: int = 100) -> dict:
    """
    Returns the top bid/ask walls (largest orders).
    Useful for spotting where institutional orders sit.
    """
    data = fetch("/fapi/v1/depth", {"symbol": SYMBOL, "limit": depth})
    if "error" in (data or {}):
        return data

    def top_walls(side: list, n: int = 5) -> list:
        """Sort by size descending, return top N as [price, qty] dicts."""
        sorted_side = sorted(side, key=lambda x: float(x[1]), reverse=True)
        return [{"price": float(p), "qty": float(q)} for p, q in sorted_side[:n]]

    return {
        "top_bid_walls": top_walls(data.get("bids", [])),
        "top_ask_walls": top_walls(data.get("asks", [])),
        "last_update_id": data.get("lastUpdateId"),
    }


def estimate_liquidation_zones(current_price: float) -> dict:
    """
    Estimates approximate liquidation price zones based on current price
    and standard leverage levels. This is a mathematical approximation —
    not the actual CoinGlass heatmap (which requires paid API).

    Formula: liq_price ≈ entry × (1 ± 1/leverage × safety_margin)
    Assuming average entry near current price and 0.5% maintenance margin.
    """
    zones = {}
    for leverage in [100, 50, 25, 10]:
        # Liq distance from entry = (1/leverage) - maintenance_margin
        maintenance = 0.005  # 0.5% typical on Binance
        distance_pct = (1 / leverage) - maintenance

        long_liq  = round(current_price * (1 - distance_pct), 1)
        short_liq = round(current_price * (1 + distance_pct), 1)
        zones[f"{leverage}x"] = {
            "long_liq_below":  long_liq,
            "short_liq_above": short_liq,
            "distance_pct":    round(distance_pct * 100, 2),
        }
    return zones


def get_current_price() -> float:
    """Latest mark price for BTC."""
    data = fetch("/fapi/v1/premiumIndex", {"symbol": SYMBOL})
    if data and "markPrice" in data:
        return float(data["markPrice"])
    # fallback: ticker
    data = fetch("/fapi/v1/ticker/price", {"symbol": SYMBOL})
    return float(data.get("price", 0)) if data else 0.0


def summarize_liquidations(liq_list: list) -> dict:
    """Aggregate recent liquidations into long vs short totals."""
    long_liq_usd  = 0.0
    short_liq_usd = 0.0
    long_liq_count  = 0
    short_liq_count = 0

    for liq in liq_list:
        try:
            val = float(liq.get("price", 0)) * float(liq.get("origQty", 0))
            # SELL side = long position liquidated
            if liq.get("side") == "SELL":
                long_liq_usd   += val
                long_liq_count += 1
            else:
                short_liq_usd   += val
                short_liq_count += 1
        except Exception:
            continue

    return {
        "longs_liquidated_usd":   round(long_liq_usd),
        "shorts_liquidated_usd":  round(short_liq_usd),
        "long_liq_count":         long_liq_count,
        "short_liq_count":        short_liq_count,
        "dominant_side_liqd":     "LONGS" if long_liq_usd > short_liq_usd else "SHORTS",
    }


def build_report() -> dict:
    """Full derivatives context snapshot."""
    price = get_current_price()
    ls    = get_long_short_ratios()
    fr    = get_funding_rate()
    oi    = get_open_interest()
    liqs  = get_recent_liquidations()
    book  = get_order_book_walls()

    # Parse L/S ratio safely
    def safe_ratio(dataset):
        if isinstance(dataset, list) and dataset:
            return dataset[0]
        return dataset or {}

    g = safe_ratio(ls["global_account"])
    t = safe_ratio(ls["top_account"])
    p = safe_ratio(ls["top_position"])

    funding_rate = float(fr.get("fundingRate", 0)) if fr else 0
    funding_pct  = round(funding_rate * 100, 4)
    funding_bias = (
        "NEUTRAL" if abs(funding_pct) < 0.005 else
        "LONG_HEAVY (longs paying)"  if funding_pct > 0 else
        "SHORT_HEAVY (shorts paying)"
    )

    oi_val = float(oi.get("openInterest", 0)) if oi else 0

    liq_summary = summarize_liquidations(liqs)
    liq_zones   = estimate_liquidation_zones(price) if price else {}

    # Determine L/S dominance
    g_ratio = float(g.get("longShortRatio", 1))
    dominance = "LONGS" if g_ratio > 1 else "SHORTS"

    return {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "symbol": SYMBOL,
        "mark_price_usd": price,
        "long_short": {
            "global_accounts": {
                "long_pct":   float(g.get("longAccount",  0.5)) * 100,
                "short_pct":  float(g.get("shortAccount", 0.5)) * 100,
                "ratio":      g_ratio,
                "dominance":  dominance,
            },
            "top_traders_accounts": {
                "long_pct":  float(t.get("longAccount",  0.5)) * 100,
                "short_pct": float(t.get("shortAccount", 0.5)) * 100,
                "ratio":     float(t.get("longShortRatio", 1)),
            },
            "top_traders_positions": {
                "long_pct":  float(p.get("longAccount",  0.5)) * 100,
                "short_pct": float(p.get("shortAccount", 0.5)) * 100,
                "ratio":     float(p.get("longShortRatio", 1)),
            },
        },
        "funding_rate": {
            "rate_pct":  funding_pct,
            "bias":      funding_bias,
            "annualized_pct": round(funding_pct * 3 * 365, 2),
        },
        "open_interest": {
            "contracts": oi_val,
            "note": "multiply by mark_price for USD value",
        },
        "recent_liquidations_100": liq_summary,
        "estimated_liq_zones": liq_zones,
        "order_book_walls": book,
    }


def print_summary(report: dict):
    """Human-readable summary for Hermes / Telegram output."""
    ls  = report["long_short"]
    g   = ls["global_accounts"]
    top = ls["top_traders_accounts"]
    fr  = report["funding_rate"]
    liq = report["recent_liquidations_100"]
    book = report["order_book_walls"]
    zones = report["estimated_liq_zones"]
    price = report["mark_price_usd"]

    print(f"""
╔══════════════════════════════════════════════╗
║  📊 BTC DERIVATIVES CONTEXT  (5m snapshot)  ║
╚══════════════════════════════════════════════╝

💰 Mark Price: ${price:,.1f}

─── LONG/SHORT RATIO ─────────────────────────
🌍 All traders:    LONG {g['long_pct']:.1f}% │ SHORT {g['short_pct']:.1f}%
   Ratio: {g['ratio']:.3f}  →  {'🟢 LONGS dominate' if g['ratio'] > 1 else '🔴 SHORTS dominate'}

👑 Top traders (acct): LONG {top['long_pct']:.1f}% │ SHORT {top['short_pct']:.1f}%
   Ratio: {top['ratio']:.3f}  →  {'🟢 LONGS dominate' if top['ratio'] > 1 else '🔴 SHORTS dominate'}

─── FUNDING RATE ──────────────────────────────
💸 Rate: {fr['rate_pct']:+.4f}%  ({fr['bias']})
   Annualized: {fr['annualized_pct']:+.1f}%

─── RECENT LIQUIDATIONS (last 100 orders) ─────
🔥 Longs liqd:  ${liq['longs_liquidated_usd']:,}  ({liq['long_liq_count']} orders)
💧 Shorts liqd: ${liq['shorts_liquidated_usd']:,}  ({liq['short_liq_count']} orders)
➤  Dominant side being wrecked: {liq['dominant_side_liqd']}

─── ESTIMATED LIQ ZONES (approx, not CoinGlass) ──
  100x: LONG liq ≈ ${zones.get('100x', {}).get('long_liq_below', 'N/A'):,}  │  SHORT liq ≈ ${zones.get('100x', {}).get('short_liq_above', 'N/A'):,}
   50x: LONG liq ≈ ${zones.get('50x',  {}).get('long_liq_below', 'N/A'):,}  │  SHORT liq ≈ ${zones.get('50x',  {}).get('short_liq_above', 'N/A'):,}
   25x: LONG liq ≈ ${zones.get('25x',  {}).get('long_liq_below', 'N/A'):,}  │  SHORT liq ≈ ${zones.get('25x',  {}).get('short_liq_above', 'N/A'):,}
   10x: LONG liq ≈ ${zones.get('10x',  {}).get('long_liq_below', 'N/A'):,}  │  SHORT liq ≈ ${zones.get('10x',  {}).get('short_liq_above', 'N/A'):,}

─── ORDER BOOK WALLS (top 5 by size) ──────────
🟩 TOP BID WALLS (support):""")

    for w in book.get("top_bid_walls", []):
        print(f"   ${w['price']:>10,.1f}  →  {w['qty']:>8.3f} BTC")

    print("🟥 TOP ASK WALLS (resistance):")
    for w in book.get("top_ask_walls", []):
        print(f"   ${w['price']:>10,.1f}  →  {w['qty']:>8.3f} BTC")

    print(f"\n⏱  Timestamp UTC: {report['timestamp_utc']}")
    print("─" * 48)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BTC Derivatives Context Fetcher")
    parser.add_argument("--summary", action="store_true", help="Human-readable output")
    parser.add_argument("--json",    action="store_true", help="Raw JSON output")
    args = parser.parse_args()

    report = build_report()

    if args.json or not args.summary:
        # default: JSON (for Hermes skill to parse)
        print(json.dumps(report, indent=2))
    else:
        print_summary(report)
