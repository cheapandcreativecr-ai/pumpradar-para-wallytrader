#!/usr/bin/env python3
"""
orderflow_snapshot.py
=====================
Corre el aggregator por N segundos y guarda un snapshot JSON
en /tmp/wally_orderflow_snapshot.json para que Hermes lo lea.

Uso:
    python3 scripts/orderflow_snapshot.py          # 60 segundos (default)
    python3 scripts/orderflow_snapshot.py --secs 30
    python3 scripts/orderflow_snapshot.py --print  # imprime el JSON al final
"""

import ssl
import certifi
ssl._create_default_https_context = lambda: ssl.create_default_context(cafile=certifi.where())

import asyncio
import json
import time
import argparse
from datetime import datetime, timezone
import websockets

SYMBOL_BINANCE = "btcusdt"
SYMBOL_BYBIT   = "BTCUSDT"
SYMBOL_OKX     = "BTC-USDT-SWAP"
SNAPSHOT_PATH  = "/tmp/wally_orderflow_snapshot.json"

BIG_TRADE_THRESHOLD  = 100_000
HUGE_TRADE_THRESHOLD = 500_000

state = {
    "binance": {"buy_vol": 0.0, "sell_vol": 0.0, "big_trades": []},
    "bybit":   {"buy_vol": 0.0, "sell_vol": 0.0, "big_trades": []},
    "okx":     {"buy_vol": 0.0, "sell_vol": 0.0, "big_trades": []},
    "last_price": 0.0,
    "alerts": [],
    "connected": [],
}


def register_trade(exchange: str, side: str, price: float, qty: float):
    usd_val = price * qty
    state[exchange][f"{side}_vol"] += usd_val
    state["last_price"] = price

    if usd_val >= BIG_TRADE_THRESHOLD:
        trade = {
            "time":     datetime.now(timezone.utc).strftime("%H:%M:%S"),
            "exchange": exchange,
            "side":     side.upper(),
            "price":    price,
            "qty":      qty,
            "usd":      round(usd_val),
        }
        state[exchange]["big_trades"].append(trade)
        state[exchange]["big_trades"] = state[exchange]["big_trades"][-30:]

        if usd_val >= HUGE_TRADE_THRESHOLD:
            alert = {
                "time":     trade["time"],
                "exchange": exchange,
                "side":     side.upper(),
                "usd":      round(usd_val),
                "price":    price,
            }
            state["alerts"].append(alert)
            state["alerts"] = state["alerts"][-20:]


async def connect_binance(stop_event):
    url = f"wss://stream.binance.com:9443/ws/{SYMBOL_BINANCE}@aggTrade"
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    try:
        async with websockets.connect(url, ssl=ssl_ctx, ping_interval=20) as ws:
            state["connected"].append("binance")
            while not stop_event.is_set():
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    data = json.loads(msg)
                    price = float(data["p"])
                    qty   = float(data["q"])
                    side  = "sell" if data["m"] else "buy"
                    register_trade("binance", side, price, qty)
                except asyncio.TimeoutError:
                    continue
    except Exception:
        pass


async def connect_bybit(stop_event):
    url = "wss://stream.bybit.com/v5/public/linear"
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    try:
        async with websockets.connect(url, ssl=ssl_ctx, ping_interval=20) as ws:
            await ws.send(json.dumps({
                "op": "subscribe",
                "args": [f"publicTrade.{SYMBOL_BYBIT}"]
            }))
            state["connected"].append("bybit")
            while not stop_event.is_set():
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    data = json.loads(msg)
                    if data.get("topic", "").startswith("publicTrade"):
                        for trade in data.get("data", []):
                            price = float(trade["p"])
                            qty   = float(trade["v"])
                            side  = "buy" if trade["S"] == "Buy" else "sell"
                            register_trade("bybit", side, price, qty)
                except asyncio.TimeoutError:
                    continue
    except Exception:
        pass


async def connect_okx(stop_event):
    url = "wss://ws.okx.com:8443/ws/v5/public"
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    try:
        async with websockets.connect(url, ssl=ssl_ctx, ping_interval=20) as ws:
            await ws.send(json.dumps({
                "op": "subscribe",
                "args": [{"channel": "trades", "instId": SYMBOL_OKX}]
            }))
            state["connected"].append("okx")
            while not stop_event.is_set():
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    data = json.loads(msg)
                    for trade in data.get("data", []):
                        price = float(trade["px"])
                        qty   = float(trade["sz"])
                        side  = "buy" if trade["side"] == "buy" else "sell"
                        register_trade("okx", side, price, qty)
                except asyncio.TimeoutError:
                    continue
    except Exception:
        pass


def build_snapshot(duration_secs: float) -> dict:
    price = state["last_price"]

    exchanges_data = {}
    for ex in ["binance", "bybit", "okx"]:
        buy  = state[ex]["buy_vol"]
        sell = state[ex]["sell_vol"]
        exchanges_data[ex] = {
            "buy_usd":   round(buy),
            "sell_usd":  round(sell),
            "delta_usd": round(buy - sell),
            "big_trades": state[ex]["big_trades"][-10:],
        }

    total_buy  = sum(state[ex]["buy_vol"]  for ex in ["binance","bybit","okx"])
    total_sell = sum(state[ex]["sell_vol"] for ex in ["binance","bybit","okx"])
    total      = total_buy + total_sell or 1
    delta      = total_buy - total_sell

    bin_delta   = state["binance"]["buy_vol"] - state["binance"]["sell_vol"]
    bybit_delta = state["bybit"]["buy_vol"]   - state["bybit"]["sell_vol"]
    okx_delta   = state["okx"]["buy_vol"]     - state["okx"]["sell_vol"]

    divergence = None
    if bin_delta > 0 and (bybit_delta < 0 or okx_delta < 0):
        divergence = "BINANCE_BUYS_OTHERS_SELL"
    elif bin_delta < 0 and (bybit_delta > 0 or okx_delta > 0):
        divergence = "BINANCE_SELLS_OTHERS_BUY"

    # Clasificar alertas por lado
    huge_buys  = [a for a in state["alerts"] if a["side"] == "BUY"]
    huge_sells = [a for a in state["alerts"] if a["side"] == "SELL"]

    # Detectar patrón de barrida: orden enorme aislada seguida de calma
    possible_sweep = False
    if state["alerts"]:
        last_alert_usd = state["alerts"][-1]["usd"]
        if last_alert_usd > 2_000_000:
            possible_sweep = True

    return {
        "timestamp_utc":   datetime.now(timezone.utc).isoformat(),
        "duration_secs":   round(duration_secs),
        "symbol":          "BTCUSDT",
        "mark_price":      price,
        "connected_exchanges": state["connected"],
        "exchanges":       exchanges_data,
        "aggregate": {
            "total_buy_usd":   round(total_buy),
            "total_sell_usd":  round(total_sell),
            "delta_usd":       round(delta),
            "buy_pct":         round(total_buy / total * 100, 1),
            "sell_pct":        round(total_sell / total * 100, 1),
            "bias":            "BUY_PRESSURE" if delta > 0 else "SELL_PRESSURE",
        },
        "divergence":      divergence,
        "huge_alerts": {
            "all":        state["alerts"][-10:],
            "buys":       huge_buys[-5:],
            "sells":      huge_sells[-5:],
            "total_huge_buy_usd":  sum(a["usd"] for a in huge_buys),
            "total_huge_sell_usd": sum(a["usd"] for a in huge_sells),
        },
        "possible_sweep":  possible_sweep,
        "analysis_hints": {
            "for_long_entry": (
                "FAVORABLE"   if delta > 0 and not divergence == "BINANCE_BUYS_OTHERS_SELL"
                else "CAUTION" if divergence
                else "NEUTRAL"
            ),
            "for_short_entry": (
                "FAVORABLE"   if delta < 0 and not divergence == "BINANCE_SELLS_OTHERS_BUY"
                else "CAUTION" if divergence
                else "NEUTRAL"
            ),
            "sweep_risk": "HIGH" if possible_sweep else "NORMAL",
        },
    }


async def main(secs: int):
    stop_event = asyncio.Event()
    t0 = time.time()

    print(f"📡 Capturando orderflow por {secs}s — Binance + Bybit + OKX...")

    tasks = [
        asyncio.create_task(connect_binance(stop_event)),
        asyncio.create_task(connect_bybit(stop_event)),
        asyncio.create_task(connect_okx(stop_event)),
    ]

    await asyncio.sleep(secs)
    stop_event.set()

    for t in tasks:
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass

    duration = time.time() - t0
    snapshot = build_snapshot(duration)

    with open(SNAPSHOT_PATH, "w") as f:
        json.dump(snapshot, f, indent=2)

    print(f"✅ Snapshot guardado en {SNAPSHOT_PATH}")
    return snapshot


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--secs",  type=int, default=60, help="Segundos de captura")
    parser.add_argument("--print", action="store_true",  help="Imprimir JSON al final")
    args = parser.parse_args()

    snapshot = asyncio.run(main(args.secs))

    if args.print:
        print(json.dumps(snapshot, indent=2))
    else:
        # Resumen corto para Hermes
        agg  = snapshot["aggregate"]
        div  = snapshot.get("divergence") or "ninguna"
        hint = snapshot["analysis_hints"]
        huge = snapshot["huge_alerts"]

        print(f"""
💰 Precio: ${snapshot['mark_price']:,.1f}
📊 Bias: {agg['bias']} ({agg['buy_pct']}% buy / {agg['sell_pct']}% sell)
⚡ Delta: ${agg['delta_usd']/1e6:+.2f}M
🔀 Divergencia: {div}
🎯 Para LONG: {hint['for_long_entry']} | Para SHORT: {hint['for_short_entry']}
🚨 Riesgo barrida: {hint['sweep_risk']}
📦 Trades HUGE — BUY: ${huge['total_huge_buy_usd']/1e6:.2f}M | SELL: ${huge['total_huge_sell_usd']/1e6:.2f}M
Exchanges conectados: {', '.join(snapshot['connected_exchanges'])}
""")
