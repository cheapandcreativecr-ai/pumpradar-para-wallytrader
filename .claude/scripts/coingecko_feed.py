"""CoinGecko feed — nuevos listings, pump candidates, exchange inflows.

Endpoints usados (free tier, sin API key):
- /coins/markets        — top tokens por volumen/mcap
- /coins/{id}           — detalle: holders, liquidity, exchanges
- /search/trending      — tokens trending en las últimas 24h
- /coins/{id}/market_chart — histórico de precio para pattern matching

Con API key (pro): más rate limit y endpoints de on-chain.
Configura COINGECKO_API_KEY en .claude/.env para activar pro.
"""
from __future__ import annotations

import os
import time
import requests
from typing import Optional

DEFAULT_TIMEOUT = 10
BASE_URL = "https://api.coingecko.com/api/v3"
PRO_URL  = "https://pro-api.coingecko.com/api/v3"


class CoinGeckoError(RuntimeError):
    pass


def _api_key() -> Optional[str]:
    return os.environ.get("COINGECKO_API_KEY")


def _base() -> str:
    return PRO_URL if _api_key() else BASE_URL


def _get(endpoint: str, params: dict | None = None) -> dict | list:
    headers = {}
    if _api_key():
        headers["x-cg-pro-api-key"] = _api_key()
    url = _base() + endpoint
    try:
        r = requests.get(url, params=params, headers=headers, timeout=DEFAULT_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        raise CoinGeckoError(f"CoinGecko {endpoint} failed: {e}") from e


# ── Trending ──────────────────────────────────────────────────────────────────

def get_trending() -> list[dict]:
    """Tokens trending en las últimas 24h según CoinGecko."""
    data = _get("/search/trending")
    coins = data.get("coins", [])
    result = []
    for c in coins:
        item = c.get("item", {})
        result.append({
            "id":     item.get("id", ""),
            "symbol": item.get("symbol", "").upper(),
            "name":   item.get("name", ""),
            "rank":   item.get("market_cap_rank"),
            "score":  item.get("score", 0),
            "thumb":  item.get("thumb", ""),
        })
    return result


# ── Markets ───────────────────────────────────────────────────────────────────

def get_new_listings(limit: int = 50) -> list[dict]:
    """Tokens ordenados por fecha de listing (más nuevos primero)."""
    data = _get("/coins/markets", {
        "vs_currency":           "usd",
        "order":                 "id_asc",
        "per_page":              limit,
        "page":                  1,
        "sparkline":             "true",
        "price_change_percentage": "1h,24h,7d",
    })
    return [_normalize_market(c) for c in data]


def get_top_movers(limit: int = 50) -> list[dict]:
    """Tokens con mayor cambio de precio en 24h."""
    data = _get("/coins/markets", {
        "vs_currency":           "usd",
        "order":                 "volume_desc",
        "per_page":              limit,
        "page":                  1,
        "sparkline":             "true",
        "price_change_percentage": "1h,24h,7d",
    })
    coins = [_normalize_market(c) for c in data]
    # Ordenar por cambio 24h descendente
    return sorted(coins, key=lambda x: abs(x.get("change24h", 0)), reverse=True)


def get_small_caps(min_mcap: int = 100_000, max_mcap: int = 5_000_000,
                   limit: int = 100) -> list[dict]:
    """Tokens de baja capitalización — zona de pumps."""
    data = _get("/coins/markets", {
        "vs_currency":           "usd",
        "order":                 "volume_desc",
        "per_page":              limit,
        "page":                  1,
        "sparkline":             "true",
        "price_change_percentage": "1h,24h,7d",
    })
    coins = [_normalize_market(c) for c in data]
    return [
        c for c in coins
        if c.get("mcap", 0) and min_mcap <= c["mcap"] <= max_mcap
    ]


def _normalize_market(c: dict) -> dict:
    return {
        "id":         c.get("id", ""),
        "symbol":     c.get("symbol", "").upper(),
        "name":       c.get("name", ""),
        "price":      c.get("current_price", 0) or 0,
        "mcap":       c.get("market_cap", 0) or 0,
        "volume24h":  c.get("total_volume", 0) or 0,
        "change1h":   c.get("price_change_percentage_1h_in_currency", 0) or 0,
        "change24h":  c.get("price_change_percentage_24h", 0) or 0,
        "change7d":   c.get("price_change_percentage_7d_in_currency", 0) or 0,
        "sparkline":  c.get("sparkline_in_7d", {}).get("price", []),
        "ath":        c.get("ath", 0) or 0,
        "ath_pct":    c.get("ath_change_percentage", 0) or 0,
        "chain":      "unknown",  # se enriquece con get_coin_detail
    }


# ── Coin detail ───────────────────────────────────────────────────────────────

def get_coin_detail(coin_id: str) -> dict:
    """Detalle completo: plataformas, exchanges, comunidad."""
    data = _get(f"/coins/{coin_id}", {
        "localization":   "false",
        "tickers":        "true",
        "market_data":    "true",
        "community_data": "true",
        "developer_data": "false",
    })

    # Detectar chain principal
    platforms = data.get("asset_platform_id") or ""
    chain = _detect_chain(platforms, data.get("platforms", {}))

    market = data.get("market_data", {})
    community = data.get("community_data", {})

    # Exchange inflow proxy: número de exchanges donde cotiza
    tickers = data.get("tickers", [])
    exchange_count = len(set(t.get("market", {}).get("name", "") for t in tickers))

    return {
        "id":             coin_id,
        "symbol":         data.get("symbol", "").upper(),
        "name":           data.get("name", ""),
        "chain":          chain,
        "price":          market.get("current_price", {}).get("usd", 0) or 0,
        "mcap":           market.get("market_cap", {}).get("usd", 0) or 0,
        "volume24h":      market.get("total_volume", {}).get("usd", 0) or 0,
        "change24h":      market.get("price_change_percentage_24h", 0) or 0,
        "change7d":       market.get("price_change_percentage_7d", 0) or 0,
        "ath":            market.get("ath", {}).get("usd", 0) or 0,
        "holders":        community.get("twitter_followers", 0) or 0,  # proxy
        "exchange_count": exchange_count,
        "exchange_inflow": round(exchange_count / 10, 1),  # normalizado
        "platforms":      data.get("platforms", {}),
        "genesis_date":   data.get("genesis_date", ""),
        "categories":     data.get("categories", []),
    }


def _detect_chain(platform_id: str, platforms: dict) -> str:
    mapping = {
        "solana":           "SOL",
        "ethereum":         "ETH",
        "binance-smart-chain": "BNB",
        "polygon-pos":      "MATIC",
        "avalanche":        "AVAX",
        "base":             "BASE",
        "arbitrum-one":     "ARB",
    }
    if platform_id in mapping:
        return mapping[platform_id]
    for k in platforms:
        if k in mapping:
            return mapping[k]
    return "OTHER"


# ── Price history para pattern matching ───────────────────────────────────────

def get_price_history(coin_id: str, days: int = 30) -> list[float]:
    """Histórico de precios de cierre para pattern matching."""
    data = _get(f"/coins/{coin_id}/market_chart", {
        "vs_currency": "usd",
        "days":        days,
        "interval":    "daily",
    })
    prices = data.get("prices", [])
    return [p[1] for p in prices]


def get_volume_history(coin_id: str, days: int = 30) -> list[float]:
    """Histórico de volumen para detectar spikes."""
    data = _get(f"/coins/{coin_id}/market_chart", {
        "vs_currency": "usd",
        "days":        days,
        "interval":    "daily",
    })
    volumes = data.get("total_volumes", [])
    return [v[1] for v in volumes]


# ── Scanner principal ─────────────────────────────────────────────────────────

def scan_pump_candidates(
    min_change24h: float = 20.0,
    max_mcap: int = 10_000_000,
    min_volume: int = 50_000,
) -> list[dict]:
    """
    Escanea el mercado y retorna candidatos a pump basado en:
    - Cambio 24h > min_change24h%
    - McAp < max_mcap (small caps)
    - Volumen > min_volume (hay liquidez)
    """
    try:
        movers = get_top_movers(limit=100)
    except CoinGeckoError as e:
        print(f"CoinGecko error: {e}")
        return []

    candidates = []
    for coin in movers:
        if (coin.get("change24h", 0) >= min_change24h
                and coin.get("mcap", 0) <= max_mcap
                and coin.get("volume24h", 0) >= min_volume):
            candidates.append(coin)

    return sorted(candidates, key=lambda x: x.get("change24h", 0), reverse=True)


def scan_dump_signals(
    max_change24h: float = -20.0,
    min_volume: int = 100_000,
) -> list[dict]:
    """
    Detecta tokens con señales de dump:
    - Caída 24h > 20%
    - Volumen alto (hay actividad de venta)
    """
    try:
        movers = get_top_movers(limit=100)
    except CoinGeckoError as e:
        print(f"CoinGecko error: {e}")
        return []

    return [
        c for c in movers
        if c.get("change24h", 0) <= max_change24h
        and c.get("volume24h", 0) >= min_volume
    ]


# ── Volume spike detector ─────────────────────────────────────────────────────

def volume_spike(coin_id: str, days: int = 14) -> float:
    """
    Retorna cuántas veces el volumen de hoy supera el promedio histórico.
    > 3x = anómalo, > 5x = muy anómalo.
    """
    try:
        vols = get_volume_history(coin_id, days=days)
        if len(vols) < 2:
            return 1.0
        avg = sum(vols[:-1]) / len(vols[:-1])
        current = vols[-1]
        return round(current / avg, 2) if avg > 0 else 1.0
    except CoinGeckoError:
        return 1.0


# ── CLI de prueba ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys, json

    cmd = sys.argv[1] if len(sys.argv) > 1 else "--trending"

    if cmd == "--trending":
        print("=== TRENDING ===")
        for c in get_trending():
            print(f"  {c['symbol']:10} {c['name']}")

    elif cmd == "--pumps":
        print("=== PUMP CANDIDATES ===")
        coins = scan_pump_candidates()
        for c in coins[:10]:
            print(f"  {c['symbol']:10} +{c['change24h']:.1f}%  mcap=${c['mcap']:,.0f}")

    elif cmd == "--dumps":
        print("=== DUMP SIGNALS ===")
        for c in scan_dump_signals():
            print(f"  {c['symbol']:10} {c['change24h']:.1f}%  vol=${c['volume24h']:,.0f}")

    elif cmd == "--detail" and len(sys.argv) > 2:
        coin_id = sys.argv[2]
        d = get_coin_detail(coin_id)
        print(json.dumps(d, indent=2))

    elif cmd == "--spike" and len(sys.argv) > 2:
        coin_id = sys.argv[2]
        spike = volume_spike(coin_id)
        print(f"Volume spike {coin_id}: {spike}x")

    else:
        print("Uso:")
        print("  python3 coingecko_feed.py --trending")
        print("  python3 coingecko_feed.py --pumps")
        print("  python3 coingecko_feed.py --dumps")
        print("  python3 coingecko_feed.py --detail bitcoin")
        print("  python3 coingecko_feed.py --spike bonk")
