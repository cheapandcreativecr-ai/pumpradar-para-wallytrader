"""
orderflow_signal_scorer.py
==========================
Toma el output de orderflow_aggregator.py + derivatives_fetcher.py
y produce un score 0-100 de confirmación de dirección para entradas A+.

Uso:
    python3 scripts/orderflow_signal_scorer.py --side long
    python3 scripts/orderflow_signal_scorer.py --side short
    python3 scripts/orderflow_signal_scorer.py --side long --json

Score ≥ 70 = orderflow confirma entrada A+
Score 50-69 = señal débil, reducir size
Score < 50  = NO entrar aunque técnico diga GO

Lógica de scoring (5 componentes):
    1. Acuerdo cross-exchange OKX+Bybit+Binance  (0-25 pts)
    2. CVD acelerando últimos 5 min               (0-20 pts)
    3. Institucionales del lado del setup          (0-20 pts)
    4. Funding rate no en contra                   (0-15 pts)
    5. Liquidaciones recientes a favor             (0-20 pts)
"""

import requests
import json
import sys
import argparse
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────────
SYMBOL_BINANCE = "BTCUSDT"
SYMBOL_BYBIT   = "BTCUSDT"
SYMBOL_OKX     = "BTC-USDT-SWAP"
TIMEOUT        = 8

# ── Helpers ───────────────────────────────────────────────────────────────────

def safe_get(url: str, params: dict = None, headers: dict = None) -> dict | list | None:
    try:
        r = requests.get(url, params=params, headers=headers, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"_error": str(e)}

def is_error(data) -> bool:
    if data is None:
        return True
    if isinstance(data, dict) and "_error" in data:
        return True
    return False

# ── Fuentes de datos ──────────────────────────────────────────────────────────

def get_binance_cvd_delta() -> dict:
    """
    Aproxima CVD (Cumulative Volume Delta) desde el order book de Binance.
    Bid volume - Ask volume de las top 20 órdenes.
    Positivo = presión compradora. Negativo = presión vendedora.
    """
    data = safe_get(
        "https://fapi.binance.com/fapi/v1/depth",
        params={"symbol": SYMBOL_BINANCE, "limit": 20}
    )
    if is_error(data):
        return {"delta": 0, "source": "binance", "error": True}

    bid_vol = sum(float(b[1]) for b in data.get("bids", []))
    ask_vol = sum(float(a[1]) for a in data.get("asks", []))
    delta   = bid_vol - ask_vol
    return {
        "delta":    round(delta, 4),
        "bid_vol":  round(bid_vol, 4),
        "ask_vol":  round(ask_vol, 4),
        "direction": "BUY" if delta > 0 else "SELL",
        "source":   "binance",
        "error":    False,
    }


def get_binance_ls_ratio() -> dict:
    """L/S ratio global de Binance — quién domina: longs o shorts."""
    data = safe_get(
        "https://fapi.binance.com/futures/data/globalLongShortAccountRatio",
        params={"symbol": SYMBOL_BINANCE, "period": "5m", "limit": 1}
    )
    if is_error(data) or not isinstance(data, list) or not data:
        return {"ratio": 1.0, "direction": "NEUTRAL", "source": "binance", "error": True}

    row   = data[0]
    ratio = float(row.get("longShortRatio", 1.0))
    return {
        "ratio":     round(ratio, 4),
        "long_pct":  round(float(row.get("longAccount", 0.5)) * 100, 2),
        "short_pct": round(float(row.get("shortAccount", 0.5)) * 100, 2),
        "direction": "BUY" if ratio > 1.05 else "SELL" if ratio < 0.95 else "NEUTRAL",
        "source":    "binance",
        "error":     False,
    }


def get_binance_top_trader_ls() -> dict:
    """L/S de los top traders (top 20% por margin) — mejor señal institucional."""
    data = safe_get(
        "https://fapi.binance.com/futures/data/topLongShortAccountRatio",
        params={"symbol": SYMBOL_BINANCE, "period": "5m", "limit": 1}
    )
    if is_error(data) or not isinstance(data, list) or not data:
        return {"ratio": 1.0, "direction": "NEUTRAL", "source": "binance_top", "error": True}

    row   = data[0]
    ratio = float(row.get("longShortRatio", 1.0))
    return {
        "ratio":     round(ratio, 4),
        "direction": "BUY" if ratio > 1.05 else "SELL" if ratio < 0.95 else "NEUTRAL",
        "source":    "binance_top",
        "error":     False,
    }


def get_binance_funding() -> dict:
    """Funding rate actual. Positivo = longs pagan. Negativo = shorts pagan."""
    data = safe_get(
        "https://fapi.binance.com/fapi/v1/fundingRate",
        params={"symbol": SYMBOL_BINANCE, "limit": 1}
    )
    if is_error(data) or not isinstance(data, list) or not data:
        return {"rate_pct": 0.0, "bias": "NEUTRAL", "error": True}

    rate     = float(data[0].get("fundingRate", 0))
    rate_pct = round(rate * 100, 5)
    bias = (
        "NEUTRAL"    if abs(rate_pct) < 0.005 else
        "LONG_HEAVY" if rate_pct > 0 else
        "SHORT_HEAVY"
    )
    return {"rate_pct": rate_pct, "bias": bias, "error": False}


def get_binance_recent_liquidations() -> dict:
    """Resumen de las últimas 100 liquidaciones. Indica qué lado está siendo barrido."""
    data = safe_get(
        "https://fapi.binance.com/fapi/v1/allForceOrders",
        params={"symbol": SYMBOL_BINANCE, "limit": 100}
    )
    if is_error(data) or not isinstance(data, list):
        return {"dominant": "NEUTRAL", "long_usd": 0, "short_usd": 0, "error": True}

    long_usd = short_usd = 0.0
    for liq in data:
        try:
            val = float(liq.get("price", 0)) * float(liq.get("origQty", 0))
            if liq.get("side") == "SELL":  # long position liquidated
                long_usd  += val
            else:
                short_usd += val
        except Exception:
            continue

    dominant = "LONGS" if long_usd > short_usd else "SHORTS" if short_usd > long_usd else "NEUTRAL"
    return {
        "long_usd":  round(long_usd),
        "short_usd": round(short_usd),
        "dominant":  dominant,   # quién está siendo BARRIDO (contrarian)
        "error":     False,
    }


def get_bybit_orderbook_delta() -> dict:
    """
    Delta de order book de Bybit (tu exchange de ejecución).
    Endpoint público v5.
    """
    data = safe_get(
        "https://api.bybit.com/v5/market/orderbook",
        params={"category": "linear", "symbol": SYMBOL_BYBIT, "limit": 25}
    )
    if is_error(data) or data.get("retCode") != 0:
        return {"delta": 0, "direction": "NEUTRAL", "source": "bybit", "error": True}

    book    = data.get("result", {})
    bid_vol = sum(float(b[1]) for b in book.get("b", []))
    ask_vol = sum(float(a[1]) for a in book.get("a", []))
    delta   = bid_vol - ask_vol
    return {
        "delta":     round(delta, 4),
        "direction": "BUY" if delta > 0 else "SELL",
        "source":    "bybit",
        "error":     False,
    }


def get_okx_taker_volume() -> dict:
    """
    Taker buy/sell volume de OKX — proxy de flujo institucional.
    Endpoint público de OKX.
    """
    data = safe_get(
        "https://www.okx.com/api/v5/rubik/stat/taker-volume",
        params={"instType": "CONTRACTS", "instId": SYMBOL_OKX, "period": "5m"}
    )
    if is_error(data) or data.get("code") != "0":
        return {"direction": "NEUTRAL", "source": "okx", "error": True}

    rows = data.get("data", [])
    if not rows:
        return {"direction": "NEUTRAL", "source": "okx", "error": True}

    # rows[0] = más reciente: [timestamp, buy_vol, sell_vol]
    try:
        row      = rows[0]
        buy_vol  = float(row[1])
        sell_vol = float(row[2])
        ratio    = buy_vol / sell_vol if sell_vol > 0 else 1.0
        direction = "BUY" if ratio > 1.05 else "SELL" if ratio < 0.95 else "NEUTRAL"
        return {
            "buy_vol":   round(buy_vol, 2),
            "sell_vol":  round(sell_vol, 2),
            "ratio":     round(ratio, 4),
            "direction": direction,
            "source":    "okx",
            "error":     False,
        }
    except Exception as e:
        return {"direction": "NEUTRAL", "source": "okx", "error": True, "_msg": str(e)}


# ── Motor de scoring ──────────────────────────────────────────────────────────

def score_component_1_cross_exchange_agreement(side: str, binance_cvd, bybit_delta, okx_taker) -> dict:
    """
    Componente 1: Acuerdo cross-exchange (0-25 pts)
    Los tres exchanges apuntan en la misma dirección que el setup.
    """
    target   = "BUY" if side == "long" else "SELL"
    sources  = [binance_cvd, bybit_delta, okx_taker]
    agreeing = sum(1 for s in sources if not s.get("error") and s.get("direction") == target)
    total    = sum(1 for s in sources if not s.get("error"))

    if total == 0:
        return {"score": 0, "detail": "sin datos de exchanges", "agreeing": 0, "total": 0}

    pct   = agreeing / total
    score = round(pct * 25)

    detail = f"{agreeing}/{total} exchanges confirman {target}"
    return {"score": score, "detail": detail, "agreeing": agreeing, "total": total}


def score_component_2_cvd_acceleration(side: str, binance_cvd, bybit_delta) -> dict:
    """
    Componente 2: CVD acelerando en dirección del setup (0-20 pts)
    Usa magnitud del delta como proxy de aceleración.
    """
    target = "BUY" if side == "long" else "SELL"
    score  = 0
    notes  = []

    if not binance_cvd.get("error"):
        delta = binance_cvd.get("delta", 0)
        if target == "BUY" and delta > 0:
            pts = min(10, round(abs(delta) / 10))
            score += pts
            notes.append(f"Binance CVD +{round(delta,1)} BTC ({pts}pts)")
        elif target == "SELL" and delta < 0:
            pts = min(10, round(abs(delta) / 10))
            score += pts
            notes.append(f"Binance CVD {round(delta,1)} BTC ({pts}pts)")
        else:
            notes.append("Binance CVD en contra (0pts)")

    if not bybit_delta.get("error"):
        d = bybit_delta.get("delta", 0)
        if (target == "BUY" and d > 0) or (target == "SELL" and d < 0):
            pts = min(10, round(abs(d) / 10))
            score += pts
            notes.append(f"Bybit delta +{round(abs(d),1)} ({pts}pts)")
        else:
            notes.append("Bybit delta en contra (0pts)")

    score = min(20, score)
    return {"score": score, "detail": " | ".join(notes) or "sin datos CVD"}


def score_component_3_institutional(side: str, top_ls, okx_taker) -> dict:
    """
    Componente 3: Institucionales del mismo lado (0-20 pts)
    Top traders Binance + OKX taker volume.
    """
    target = "BUY" if side == "long" else "SELL"
    score  = 0
    notes  = []

    if not top_ls.get("error"):
        if top_ls.get("direction") == target:
            score += 10
            notes.append(f"Top traders Binance {target} (ratio {top_ls.get('ratio')}) +10pts")
        elif top_ls.get("direction") == "NEUTRAL":
            score += 5
            notes.append("Top traders Binance NEUTRAL +5pts")
        else:
            notes.append(f"Top traders Binance en contra 0pts")

    if not okx_taker.get("error"):
        if okx_taker.get("direction") == target:
            score += 10
            notes.append(f"OKX taker {target} (ratio {okx_taker.get('ratio')}) +10pts")
        elif okx_taker.get("direction") == "NEUTRAL":
            score += 5
            notes.append("OKX taker NEUTRAL +5pts")
        else:
            notes.append("OKX taker en contra 0pts")

    score = min(20, score)
    return {"score": score, "detail": " | ".join(notes) or "sin datos institucionales"}


def score_component_4_funding(side: str, funding: dict) -> dict:
    """
    Componente 4: Funding rate no va en contra del setup (0-15 pts)
    Funding muy positivo = mercado sobre-apalancado en longs → mala entrada long.
    Funding muy negativo = sobre-apalancado en shorts → mala entrada short.
    """
    if funding.get("error"):
        return {"score": 7, "detail": "sin datos de funding (neutro)"}

    rate = funding.get("rate_pct", 0)
    score = 0
    detail = ""

    if side == "long":
        if rate < -0.01:       # shorts pagando → bueno para long
            score = 15
            detail = f"Funding {rate}% SHORT_HEAVY → favorece LONG +15pts"
        elif abs(rate) < 0.005:  # neutral
            score = 12
            detail = f"Funding {rate}% NEUTRAL +12pts"
        elif rate < 0.02:      # longs pagando poco
            score = 8
            detail = f"Funding {rate}% leve LONG_HEAVY +8pts"
        else:                  # longs pagando mucho → precaución
            score = 3
            detail = f"Funding {rate}% LONG_HEAVY alto → cuidado con long +3pts"
    else:  # short
        if rate > 0.01:        # longs pagando → bueno para short
            score = 15
            detail = f"Funding {rate}% LONG_HEAVY → favorece SHORT +15pts"
        elif abs(rate) < 0.005:
            score = 12
            detail = f"Funding {rate}% NEUTRAL +12pts"
        elif rate > -0.02:
            score = 8
            detail = f"Funding {rate}% leve SHORT_HEAVY +8pts"
        else:
            score = 3
            detail = f"Funding {rate}% SHORT_HEAVY alto → cuidado con short +3pts"

    return {"score": score, "detail": detail}


def score_component_5_liquidations(side: str, liqs: dict) -> dict:
    """
    Componente 5: Liquidaciones recientes favorecen el setup (0-20 pts)
    Si están barriendo longs → mercado limpiando para subir → favorable para long.
    Si están barriendo shorts → mercado limpiando para bajar → favorable para short.
    """
    if liqs.get("error"):
        return {"score": 10, "detail": "sin datos de liquidaciones (neutro)"}

    dominant = liqs.get("dominant", "NEUTRAL")
    long_usd  = liqs.get("long_usd", 0)
    short_usd = liqs.get("short_usd", 0)
    total_usd = long_usd + short_usd

    if total_usd == 0:
        return {"score": 10, "detail": "Sin liquidaciones recientes (neutro)"}

    score  = 0
    detail = ""

    # Longs liquidados = precio bajó = si seguís long, mercado limpiando debajo
    # Shorts liquidados = precio subió = si seguís short, mercado limpiando arriba
    if side == "long" and dominant == "LONGS":
        # Barriendo longs → suele preceder rebote
        pct   = long_usd / total_usd
        score = round(pct * 20)
        detail = f"Barriendo LONGS ${long_usd:,} ({round(pct*100)}%) → posible rebote +{score}pts"
    elif side == "short" and dominant == "SHORTS":
        pct   = short_usd / total_usd
        score = round(pct * 20)
        detail = f"Barriendo SHORTS ${short_usd:,} ({round(pct*100)}%) → posible caída +{score}pts"
    elif dominant == "NEUTRAL":
        score  = 10
        detail = "Liquidaciones balanceadas (neutro) +10pts"
    else:
        score  = 5
        detail = f"Liquidaciones en contra del setup ({dominant} dominante) +5pts"

    return {"score": min(20, score), "detail": detail}


# ── Función principal ─────────────────────────────────────────────────────────

def compute_score(side: str) -> dict:
    """
    Computa el score final 0-100 para el lado dado ('long' o 'short').
    Retorna el reporte completo con breakdown por componente.
    """
    assert side in ("long", "short"), "side debe ser 'long' o 'short'"

    # Fetch datos
    binance_cvd = get_binance_cvd_delta()
    bybit_delta = get_bybit_orderbook_delta()
    okx_taker   = get_okx_taker_volume()
    top_ls      = get_binance_top_trader_ls()
    ls_ratio    = get_binance_ls_ratio()
    funding     = get_binance_funding()
    liqs        = get_binance_recent_liquidations()

    # Scores por componente
    c1 = score_component_1_cross_exchange_agreement(side, binance_cvd, bybit_delta, okx_taker)
    c2 = score_component_2_cvd_acceleration(side, binance_cvd, bybit_delta)
    c3 = score_component_3_institutional(side, top_ls, okx_taker)
    c4 = score_component_4_funding(side, funding)
    c5 = score_component_5_liquidations(side, liqs)

    total = c1["score"] + c2["score"] + c3["score"] + c4["score"] + c5["score"]
    total = min(100, total)

    # Veredicto
    if total >= 70:
        verdict = "A+ CONFIRMADO"
        action  = "ENTRAR — orderflow confirma el setup técnico"
        size    = "full size ($50 / 25x)"
    elif total >= 50:
        verdict = "SEÑAL DÉBIL"
        action  = "ESPERAR setup más claro o reducir size 50%"
        size    = "half size ($25 / 25x)"
    else:
        verdict = "NO ENTRAR"
        action  = "Orderflow contradice el setup técnico — SKIP"
        size    = "—"

    return {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "side":          side.upper(),
        "total_score":   total,
        "verdict":       verdict,
        "action":        action,
        "recommended_size": size,
        "breakdown": {
            "c1_cross_exchange": c1,
            "c2_cvd_acceleration": c2,
            "c3_institutional": c3,
            "c4_funding": c4,
            "c5_liquidations": c5,
        },
        "raw_data": {
            "binance_cvd":  binance_cvd,
            "bybit_delta":  bybit_delta,
            "okx_taker":    okx_taker,
            "top_ls":       top_ls,
            "ls_ratio":     ls_ratio,
            "funding":      funding,
            "liquidations": liqs,
        }
    }


def print_summary(report: dict):
    total   = report["total_score"]
    verdict = report["verdict"]
    b       = report["breakdown"]
    bar     = "█" * (total // 5) + "░" * (20 - total // 5)

    print(f"""
╔══════════════════════════════════════════════════╗
║   ORDERFLOW SIGNAL SCORER — {report['side']:<6}              ║
╚══════════════════════════════════════════════════╝

  Score: {total}/100  [{bar}]
  Veredicto: {verdict}
  Acción:    {report['action']}
  Size:      {report['recommended_size']}

  ── Breakdown ───────────────────────────────────
  C1 Cross-exchange agreement: {b['c1_cross_exchange']['score']:>3}/25  — {b['c1_cross_exchange']['detail']}
  C2 CVD acceleration:         {b['c2_cvd_acceleration']['score']:>3}/20  — {b['c2_cvd_acceleration']['detail']}
  C3 Institutional flow:       {b['c3_institutional']['score']:>3}/20  — {b['c3_institutional']['detail']}
  C4 Funding rate:             {b['c4_funding']['score']:>3}/15  — {b['c4_funding']['detail']}
  C5 Liquidation sweep:        {b['c5_liquidations']['score']:>3}/20  — {b['c5_liquidations']['detail']}
  ────────────────────────────────────────────────
  TOTAL:                       {total:>3}/100

  Timestamp UTC: {report['timestamp_utc']}
""")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Orderflow Signal Scorer para entradas A+")
    parser.add_argument("--side",    required=True, choices=["long", "short"], help="Dirección del setup técnico")
    parser.add_argument("--json",    action="store_true", help="Output JSON crudo")
    parser.add_argument("--summary", action="store_true", help="Output legible (default)")
    args = parser.parse_args()

    report = compute_score(args.side)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_summary(report)
