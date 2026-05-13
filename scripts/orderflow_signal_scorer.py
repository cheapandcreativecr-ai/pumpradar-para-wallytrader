"""
orderflow_signal_scorer.py  (v2 — snapshot-driven)
===================================================
Lee datos reales de orderflow_snapshot.py (WebSocket Binance+Bybit+OKX)
y produce un score 0-100 para detectar entradas A+.

Objetivo: $50 → $1000 en 15 días. Solo entra cuando hay confirmación real.

Uso:
    python3 scripts/orderflow_signal_scorer.py --side long
    python3 scripts/orderflow_signal_scorer.py --side short
    python3 scripts/orderflow_signal_scorer.py --side long --secs 60
    python3 scripts/orderflow_signal_scorer.py --side long --json

Score ≥ 70  → A+ CONFIRMADO — ENTRAR
Score 50-69 → SEÑAL DÉBIL — esperar o reducir size
Score < 50  → NO ENTRAR — skip aunque el técnico diga GO

Componentes (100 pts):
    C1  Acuerdo Binance+Bybit+OKX en misma dirección   (0-25 pts)
    C2  Delta total + magnitud del flujo                (0-20 pts)
    C3  Flujo institucional OKX huge alerts             (0-20 pts)
    C4  Funding rate no en contra                       (0-15 pts)
    C5  Divergencia entre exchanges + top traders       (0-20 pts)
"""

import subprocess
import sys
import json
import argparse
import requests
from datetime import datetime, timezone
from pathlib import Path

SYMBOL_BINANCE   = "BTCUSDT"
SNAPSHOT_PATH    = "/tmp/wally_orderflow_snapshot.json"
SNAPSHOT_MAX_AGE = 90
DEFAULT_SECS     = 30
TIMEOUT          = 8

# ── Snapshot ──────────────────────────────────────────────────────────────────

def get_snapshot(secs: int = DEFAULT_SECS) -> dict:
    snap_file = Path(SNAPSHOT_PATH)
    if snap_file.exists():
        age = datetime.now().timestamp() - snap_file.stat().st_mtime
        if age < SNAPSHOT_MAX_AGE:
            try:
                with open(snap_file) as f:
                    data = json.load(f)
                print(f"  📂 Snapshot reciente ({int(age)}s) — reutilizando")
                return data
            except Exception:
                pass

    script = Path(__file__).parent / "orderflow_snapshot.py"
    if not script.exists():
        print("  ❌ orderflow_snapshot.py no encontrado")
        return {}

    print(f"  📡 Capturando orderflow {secs}s — Binance + Bybit + OKX...")
    try:
        subprocess.run([sys.executable, str(script), "--secs", str(secs)], timeout=secs + 15)
        if snap_file.exists():
            with open(snap_file) as f:
                return json.load(f)
    except Exception as e:
        print(f"  ⚠️  Error: {e}")
    return {}

# ── REST — solo funding y top traders (no están en WebSocket) ─────────────────

def safe_get(url, params=None):
    try:
        r = requests.get(url, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}

def get_funding() -> dict:
    data = safe_get("https://fapi.binance.com/fapi/v1/fundingRate",
                    {"symbol": SYMBOL_BINANCE, "limit": 1})
    if not isinstance(data, list) or not data:
        return {"rate_pct": 0.0, "bias": "NEUTRAL", "error": True}
    rate     = float(data[0].get("fundingRate", 0))
    rate_pct = round(rate * 100, 5)
    bias = "NEUTRAL" if abs(rate_pct) < 0.005 else "LONG_HEAVY" if rate_pct > 0 else "SHORT_HEAVY"
    return {"rate_pct": rate_pct, "bias": bias, "error": False}

def get_top_traders() -> dict:
    data = safe_get("https://fapi.binance.com/futures/data/topLongShortAccountRatio",
                    {"symbol": SYMBOL_BINANCE, "period": "5m", "limit": 1})
    if not isinstance(data, list) or not data:
        return {"ratio": 1.0, "direction": "NEUTRAL", "error": True}
    ratio = float(data[0].get("longShortRatio", 1.0))
    return {
        "ratio":     round(ratio, 4),
        "direction": "BUY" if ratio > 1.05 else "SELL" if ratio < 0.95 else "NEUTRAL",
        "error":     False,
    }

# ── Parsear snapshot ──────────────────────────────────────────────────────────

def parse_snapshot(snap: dict) -> dict:
    if not snap:
        return {}
    exs       = snap.get("exchanges", {})
    agg       = snap.get("aggregate", {})
    huge      = snap.get("huge_alerts", {})
    connected = snap.get("connected_exchanges", [])

    parsed = {
        "connected":     connected,
        "mark_price":    snap.get("mark_price", 0),
        "divergence":    snap.get("divergence", "NONE"),
        "agg_delta_usd": agg.get("delta_usd", 0),
        "agg_buy_pct":   agg.get("buy_pct", 50),
        "agg_bias":      agg.get("bias", "NEUTRAL"),
        "analysis":      snap.get("analysis_hints", {}),
        "huge_buy_usd":  huge.get("total_huge_buy_usd", 0),
        "huge_sell_usd": huge.get("total_huge_sell_usd", 0),
        "exchanges": {},
    }
    for ex in ["binance", "bybit", "okx"]:
        d     = exs.get(ex, {})
        delta = d.get("delta_usd", 0)
        parsed["exchanges"][ex] = {
            "buy_usd":   d.get("buy_usd", 0),
            "sell_usd":  d.get("sell_usd", 0),
            "delta_usd": delta,
            "direction": "BUY" if delta > 0 else "SELL" if delta < 0 else "NEUTRAL",
            "available": ex in connected,
        }
    return parsed

# ── Componentes ───────────────────────────────────────────────────────────────

def c1_cross_exchange(side: str, p: dict) -> dict:
    """C1: Los 3 exchanges apuntan en la misma dirección (0-25 pts)"""
    target  = "BUY" if side == "long" else "SELL"
    weights = {"okx": 12, "binance": 8, "bybit": 5}
    score, notes = 0, []
    for ex, w in weights.items():
        ex_data   = p.get("exchanges", {}).get(ex, {})
        if not ex_data.get("available"):
            notes.append(f"{ex.upper()} no conectado")
            continue
        direction = ex_data["direction"]
        delta     = ex_data["delta_usd"]
        if direction == target:
            score += w
            notes.append(f"{ex.upper()} ✓ {target} Δ${delta:+,.0f}")
        elif direction == "NEUTRAL":
            score += w // 2
            notes.append(f"{ex.upper()} NEUTRAL +{w//2}pts")
        else:
            notes.append(f"{ex.upper()} ✗ contra Δ${delta:+,.0f}")
    return {"score": min(25, score), "detail": " | ".join(notes)}


def c2_delta_magnitude(side: str, p: dict) -> dict:
    """C2: Magnitud del delta — cuánto dinero real está entrando (0-20 pts)"""
    target = "BUY" if side == "long" else "SELL"
    score, notes = 0, []

    agg_delta = p.get("agg_delta_usd", 0)
    if (target == "BUY" and agg_delta > 0) or (target == "SELL" and agg_delta < 0):
        abs_d = abs(agg_delta)
        pts   = 15 if abs_d > 50_000_000 else 10 if abs_d > 5_000_000 else 5 if abs_d > 500_000 else 2 if abs_d > 50_000 else 1
        score += pts
        notes.append(f"Delta agregado ${agg_delta:+,.0f} +{pts}pts")
    else:
        notes.append(f"Delta agregado ${agg_delta:+,.0f} en contra")

    bybit = p.get("exchanges", {}).get("bybit", {})
    if bybit.get("available") and bybit.get("direction") == target:
        score += 5
        notes.append("Bybit confirma ✓ +5pts")
    elif bybit.get("available"):
        notes.append("Bybit ✗ en contra")

    return {"score": min(20, score), "detail": " | ".join(notes)}


def c3_institutional_okx(side: str, p: dict) -> dict:
    """C3: Huge alerts OKX — flujo institucional real (0-20 pts)"""
    target = "BUY" if side == "long" else "SELL"
    huge_buy  = p.get("huge_buy_usd", 0)
    huge_sell = p.get("huge_sell_usd", 0)
    total     = huge_buy + huge_sell

    if total == 0:
        return {"score": 10, "detail": "Sin huge alerts — neutro +10pts"}

    pct   = (huge_buy / total) if target == "BUY" else (huge_sell / total)
    score = round(pct * 20)
    favor = huge_buy if target == "BUY" else huge_sell
    contra= huge_sell if target == "BUY" else huge_buy
    detail= f"Inst. {target}: ${favor:,.0f} vs contra ${contra:,.0f} ({round(pct*100)}% a favor)"
    return {"score": min(20, score), "detail": detail}


def c4_funding(side: str, funding: dict) -> dict:
    """C4: Funding rate no va en contra (0-15 pts)"""
    if funding.get("error"):
        return {"score": 8, "detail": "sin datos funding — neutro"}
    rate = funding["rate_pct"]
    if side == "long":
        if rate < -0.01:      score, detail = 15, f"Funding {rate}% SHORT_HEAVY → favorece LONG"
        elif abs(rate)<0.005: score, detail = 12, f"Funding {rate}% NEUTRAL"
        elif rate < 0.02:     score, detail = 8,  f"Funding {rate}% leve LONG_HEAVY"
        else:                 score, detail = 3,  f"Funding {rate}% LONG_HEAVY alto — cuidado"
    else:
        if rate > 0.01:       score, detail = 15, f"Funding {rate}% LONG_HEAVY → favorece SHORT"
        elif abs(rate)<0.005: score, detail = 12, f"Funding {rate}% NEUTRAL"
        elif rate > -0.02:    score, detail = 8,  f"Funding {rate}% leve SHORT_HEAVY"
        else:                 score, detail = 3,  f"Funding {rate}% SHORT_HEAVY alto — cuidado"
    return {"score": score, "detail": detail}


def c5_divergence_toptraders(side: str, p: dict, top_tr: dict) -> dict:
    """C5: Divergencia entre exchanges + top traders Binance (0-20 pts)"""
    target = "BUY" if side == "long" else "SELL"
    score, notes = 0, []

    div = p.get("divergence") or "NONE"
    if side == "long" and "OTHERS_BUY" in div:
        score += 10
        notes.append(f"Divergencia {div} → institucionales comprando ✓ +10pts")
    elif side == "short" and "OTHERS_SELL" in div:
        score += 10
        notes.append(f"Divergencia {div} → institucionales vendiendo ✓ +10pts")
    elif div in ("NONE", "ALIGNED", ""):
        score += 5
        notes.append("Exchanges alineados +5pts")
    else:
        notes.append(f"Divergencia {div} en contra")

    if not top_tr.get("error"):
        direction = top_tr["direction"]
        ratio     = top_tr["ratio"]
        if direction == target:
            score += 10
            notes.append(f"Top traders {target} ratio={ratio} +10pts")
        elif direction == "NEUTRAL":
            score += 5
            notes.append(f"Top traders NEUTRAL +5pts")
        else:
            notes.append(f"Top traders contra ratio={ratio}")
    else:
        score += 5
        notes.append("Top traders sin datos — neutro +5pts")

    return {"score": min(20, score), "detail": " | ".join(notes)}

# ── Score principal ───────────────────────────────────────────────────────────

def compute_score(side: str, secs: int = DEFAULT_SECS) -> dict:
    assert side in ("long", "short")

    snap    = get_snapshot(secs)
    parsed  = parse_snapshot(snap)
    funding = get_funding()
    top_tr  = get_top_traders()

    if not parsed:
        return {"error": "No se pudo obtener snapshot", "total_score": 0}

    C1 = c1_cross_exchange(side, parsed)
    C2 = c2_delta_magnitude(side, parsed)
    C3 = c3_institutional_okx(side, parsed)
    C4 = c4_funding(side, funding)
    C5 = c5_divergence_toptraders(side, parsed, top_tr)

    total = min(100, C1["score"] + C2["score"] + C3["score"] + C4["score"] + C5["score"])

    if total >= 70:
        verdict, action, size, alert = "🟢 A+ CONFIRMADO", "ENTRAR — orderflow confirma el setup técnico", "Full size (según fase)", True
    elif total >= 50:
        verdict, action, size, alert = "🟡 SEÑAL DÉBIL", "ESPERAR setup más claro o reducir size 50%", "Half size", False
    else:
        verdict, action, size, alert = "🔴 NO ENTRAR", "Orderflow contradice el setup — SKIP", "—", False

    return {
        "timestamp_utc":    datetime.now(timezone.utc).isoformat(),
        "side":             side.upper(),
        "total_score":      total,
        "verdict":          verdict,
        "action":           action,
        "recommended_size": size,
        "alert":            alert,
        "mark_price":       parsed.get("mark_price", 0),
        "connected":        parsed.get("connected", []),
        "breakdown": {
            "c1_cross_exchange":      C1,
            "c2_delta_magnitude":     C2,
            "c3_institutional":       C3,
            "c4_funding":             C4,
            "c5_divergence_toptraders": C5,
        },
        "snapshot_summary": {
            "agg_delta_usd":  parsed.get("agg_delta_usd", 0),
            "agg_buy_pct":    parsed.get("agg_buy_pct", 50),
            "divergence":     parsed.get("divergence", "NONE"),
            "huge_buy_usd":   parsed.get("huge_buy_usd", 0),
            "huge_sell_usd":  parsed.get("huge_sell_usd", 0),
            "funding":        funding,
            "top_traders":    top_tr,
        }
    }

# ── Output ────────────────────────────────────────────────────────────────────

def print_summary(r: dict):
    if "error" in r:
        print(f"\n❌ Error: {r['error']}\n")
        return

    total = r["total_score"]
    b     = r["breakdown"]
    bar   = "█" * (total // 5) + "░" * (20 - total // 5)
    snap  = r["snapshot_summary"]
    conns = ", ".join(r.get("connected", [])).upper() or "ninguno"

    print(f"""
╔══════════════════════════════════════════════════════╗
║   ORDERFLOW SIGNAL SCORER v2 — {r['side']:<6}              ║
╚══════════════════════════════════════════════════════╝

  Exchanges conectados: {conns}
  Precio actual:        ${r.get('mark_price', 0):,.1f}

  Score: {total}/100  [{bar}]
  {r['verdict']}
  Acción: {r['action']}
  Size:   {r['recommended_size']}

  ── Data WebSocket Real ────────────────────────────
  Delta agregado 3 exchanges: ${snap['agg_delta_usd']:+,.0f}
  Buy pressure:               {snap['agg_buy_pct']:.1f}%
  Divergencia:                {snap['divergence']}
  Huge BUY institucional:     ${snap['huge_buy_usd']:,.0f}
  Huge SELL institucional:    ${snap['huge_sell_usd']:,.0f}
  Funding:                    {snap['funding'].get('rate_pct','N/A')}% ({snap['funding'].get('bias','N/A')})
  Top traders dirección:      {snap['top_traders'].get('direction','N/A')} (ratio={snap['top_traders'].get('ratio','N/A')})

  ── Breakdown ──────────────────────────────────────
  C1 Cross-exchange (3 exchanges): {b['c1_cross_exchange']['score']:>3}/25  — {b['c1_cross_exchange']['detail']}
  C2 Delta magnitude:              {b['c2_delta_magnitude']['score']:>3}/20  — {b['c2_delta_magnitude']['detail']}
  C3 Institucional OKX:            {b['c3_institutional']['score']:>3}/20  — {b['c3_institutional']['detail']}
  C4 Funding rate:                 {b['c4_funding']['score']:>3}/15  — {b['c4_funding']['detail']}
  C5 Divergencia + Top traders:    {b['c5_divergence_toptraders']['score']:>3}/20  — {b['c5_divergence_toptraders']['detail']}
  ──────────────────────────────────────────────────
  TOTAL:                           {total:>3}/100

  Timestamp UTC: {r['timestamp_utc']}
""")
    if r.get("alert"):
        print("  ⚡ ALERTA A+ — Validar setup técnico en TradingView y ejecutar en Bybit\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--side",    required=True, choices=["long", "short"])
    parser.add_argument("--secs",    type=int, default=DEFAULT_SECS)
    parser.add_argument("--json",    action="store_true")
    args = parser.parse_args()

    report = compute_score(args.side, args.secs)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_summary(report)
