import ssl
import certifi

# Fix SSL para macOS — debe ir ANTES de importar websockets
ssl._create_default_https_context = lambda: ssl.create_default_context(cafile=certifi.where())

import asyncio
import json
import time
import argparse
from datetime import datetime
import websockets

SYMBOL_BINANCE = "btcusdt"
SYMBOL_BYBIT   = "BTCUSDT"
SYMBOL_OKX     = "BTC-USDT-SWAP"

BIG_TRADE_THRESHOLD  = 100_000
HUGE_TRADE_THRESHOLD = 500_000

state = {
    "binance": {"buy_vol": 0.0, "sell_vol": 0.0, "big_trades": []},
    "bybit":   {"buy_vol": 0.0, "sell_vol": 0.0, "big_trades": []},
    "okx":     {"buy_vol": 0.0, "sell_vol": 0.0, "big_trades": []},
    "last_price": 0.0,
    "start_time": time.time(),
    "alerts": [],
}


def register_trade(exchange: str, side: str, price: float, qty: float):
    usd_val = price * qty
    state[exchange][f"{side}_vol"] += usd_val
    state["last_price"] = price

    if usd_val >= BIG_TRADE_THRESHOLD:
        trade = {
            "time":     datetime.utcnow().strftime("%H:%M:%S"),
            "exchange": exchange,
            "side":     side.upper(),
            "price":    price,
            "qty":      qty,
            "usd":      usd_val,
        }
        state[exchange]["big_trades"].append(trade)
        state[exchange]["big_trades"] = state[exchange]["big_trades"][-20:]

        if usd_val >= HUGE_TRADE_THRESHOLD and exchange != "binance":
            alert = f"🚨 HUGE {side.upper()} ${usd_val/1_000_000:.2f}M en {exchange.upper()} @ ${price:,.0f}"
            state["alerts"].append(alert)
            state["alerts"] = state["alerts"][-10:]
            print(f"\n{'='*60}")
            print(alert)
            print(f"{'='*60}\n")
        else:
            emoji = "🟢" if side == "buy" else "🔴"
            print(f"{emoji} BIG {exchange.upper():8} {side.upper():5} ${usd_val/1_000:>8.0f}k @ ${price:,.0f}")


async def connect_binance():
    url = f"wss://stream.binance.com:9443/ws/{SYMBOL_BINANCE}@aggTrade"
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    while True:
        try:
            async with websockets.connect(url, ssl=ssl_ctx, ping_interval=20) as ws:
                print("✅ Binance conectado")
                async for msg in ws:
                    data = json.loads(msg)
                    price = float(data["p"])
                    qty   = float(data["q"])
                    side  = "sell" if data["m"] else "buy"
                    register_trade("binance", side, price, qty)
        except Exception as e:
            print(f"⚠️  Binance error: {e} — reconectando en 3s")
            await asyncio.sleep(3)


async def connect_bybit():
    url = "wss://stream.bybit.com/v5/public/linear"
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    while True:
        try:
            async with websockets.connect(url, ssl=ssl_ctx, ping_interval=20) as ws:
                await ws.send(json.dumps({
                    "op": "subscribe",
                    "args": [f"publicTrade.{SYMBOL_BYBIT}"]
                }))
                print("✅ Bybit conectado")
                async for msg in ws:
                    data = json.loads(msg)
                    if data.get("topic", "").startswith("publicTrade"):
                        for trade in data.get("data", []):
                            price = float(trade["p"])
                            qty   = float(trade["v"])
                            side  = "buy" if trade["S"] == "Buy" else "sell"
                            register_trade("bybit", side, price, qty)
        except Exception as e:
            print(f"⚠️  Bybit error: {e} — reconectando en 3s")
            await asyncio.sleep(3)


async def connect_okx():
    url = "wss://ws.okx.com:8443/ws/v5/public"
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    while True:
        try:
            async with websockets.connect(url, ssl=ssl_ctx, ping_interval=20) as ws:
                await ws.send(json.dumps({
                    "op": "subscribe",
                    "args": [{"channel": "trades", "instId": SYMBOL_OKX}]
                }))
                print("✅ OKX conectado")
                async for msg in ws:
                    data = json.loads(msg)
                    for trade in data.get("data", []):
                        price = float(trade["px"])
                        qty   = float(trade["sz"])
                        side  = "buy" if trade["side"] == "buy" else "sell"
                        register_trade("okx", side, price, qty)
        except Exception as e:
            print(f"⚠️  OKX error: {e} — reconectando en 3s")
            await asyncio.sleep(3)


def print_summary():
    elapsed = time.time() - state["start_time"]
    price   = state["last_price"]

    total_buy  = sum(state[ex]["buy_vol"]  for ex in ["binance","bybit","okx"])
    total_sell = sum(state[ex]["sell_vol"] for ex in ["binance","bybit","okx"])
    total      = total_buy + total_sell or 1

    buy_pct  = total_buy  / total * 100
    sell_pct = total_sell / total * 100
    delta    = total_buy - total_sell
    bias     = "🟢 BUY PRESSURE" if delta > 0 else "🔴 SELL PRESSURE"

    bin_delta   = state["binance"]["buy_vol"] - state["binance"]["sell_vol"]
    bybit_delta = state["bybit"]["buy_vol"]   - state["bybit"]["sell_vol"]
    okx_delta   = state["okx"]["buy_vol"]     - state["okx"]["sell_vol"]

    divergence = ""
    if bin_delta > 0 and (bybit_delta < 0 or okx_delta < 0):
        divergence = "⚠️  DIVERGENCIA: Binance compra pero Bybit/OKX venden"
    elif bin_delta < 0 and (bybit_delta > 0 or okx_delta > 0):
        divergence = "⚠️  DIVERGENCIA: Binance vende pero Bybit/OKX compran"

    print(f"""
╔══════════════════════════════════════════════════════╗
║  📊 ORDERFLOW AGREGADO CROSS-EXCHANGE  ({elapsed/60:.1f} min)  ║
╚══════════════════════════════════════════════════════╝

💰 Precio actual: ${price:,.1f}

─── VOLUMEN POR EXCHANGE ─────────────────────────────
           BUY ($)          SELL ($)        DELTA
BINANCE  ${state['binance']['buy_vol']/1e6:>7.2f}M      ${state['binance']['sell_vol']/1e6:>7.2f}M    {'+' if bin_delta>=0 else ''}{bin_delta/1e6:.2f}M
BYBIT    ${state['bybit']['buy_vol']/1e6:>7.2f}M      ${state['bybit']['sell_vol']/1e6:>7.2f}M    {'+' if bybit_delta>=0 else ''}{bybit_delta/1e6:.2f}M
OKX      ${state['okx']['buy_vol']/1e6:>7.2f}M      ${state['okx']['sell_vol']/1e6:>7.2f}M    {'+' if okx_delta>=0 else ''}{okx_delta/1e6:.2f}M

─── TOTAL AGREGADO ───────────────────────────────────
BUY:  ${total_buy/1e6:.2f}M  ({buy_pct:.1f}%)
SELL: ${total_sell/1e6:.2f}M  ({sell_pct:.1f}%)
DELTA: {'+' if delta>=0 else ''}{delta/1e6:.2f}M  →  {bias}

{divergence}

─── ÚLTIMAS ALERTAS CROSS-EXCHANGE ──────────────────""")

    if state["alerts"]:
        for a in state["alerts"][-5:]:
            print(f"  {a}")
    else:
        print("  Sin alertas de volumen enorme")

    non_binance_bigs = []
    for ex in ["bybit", "okx"]:
        non_binance_bigs.extend(state[ex]["big_trades"][-5:])
    non_binance_bigs.sort(key=lambda x: x["usd"], reverse=True)

    if non_binance_bigs:
        print("\n─── TRADES GRANDES FUERA DE BINANCE (últimos) ──────")
        for t in non_binance_bigs[:8]:
            emoji = "🟢" if t["side"] == "BUY" else "🔴"
            print(f"  {emoji} {t['time']} {t['exchange'].upper():6} {t['side']:5} ${t['usd']/1e3:.0f}k @ ${t['price']:,.0f}")

    print("─" * 54)


async def summary_loop():
    await asyncio.sleep(10)
    while True:
        print_summary()
        await asyncio.sleep(30)


async def main(mode: str):
    print("🚀 Iniciando OrderFlow Aggregator — BTC/USDT")
    print("   Exchanges: Binance Futures | Bybit | OKX")
    print(f"   Umbral alerta: ${BIG_TRADE_THRESHOLD/1e3:.0f}k+ | Crítico: ${HUGE_TRADE_THRESHOLD/1e3:.0f}k+")
    print("   Ctrl+C para salir\n")

    tasks = [
        connect_binance(),
        connect_bybit(),
        connect_okx(),
    ]

    if mode == "summary":
        tasks.append(summary_loop())

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--summary", action="store_true",
                        help="Imprime resumen cada 30s para Hermes")
    args = parser.parse_args()

    mode = "summary" if args.summary else "live"
    try:
        asyncio.run(main(mode))
    except KeyboardInterrupt:
        print("\n\n📊 Sesión terminada. Resumen final:")
        print_summary()
