"""
aplus_monitor.py
================
Corre el scorer cada N minutos y notifica a Telegram via notify_hub
cuando detecta una entrada A+ (score >= 70).

Uso:
    python3 scripts/aplus_monitor.py --side long
    python3 scripts/aplus_monitor.py --both          # monitorea long Y short
    python3 scripts/aplus_monitor.py --both --interval 300

Se queda corriendo en background. Usar con nohup:
    nohup python3 scripts/aplus_monitor.py --both > logs/aplus_monitor.log 2>&1 &
"""

import sys
import time
import argparse
from pathlib import Path
from datetime import datetime, timezone

# Agregar el repo al path para importar notify_hub
repo_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo_root / ".claude" / "scripts"))

from notify_hub import telegram_send, macos_notify

# Agregar scripts/ al path para importar el scorer
sys.path.insert(0, str(repo_root / "scripts"))
from orderflow_signal_scorer import compute_score

DEFAULT_INTERVAL = 300   # 5 minutos
SNAPSHOT_SECS    = 30    # segundos de captura WebSocket

def now_str():
    return datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

def check_side(side: str) -> dict:
    """Corre el scorer para un lado y retorna el reporte."""
    print(f"\n[{now_str()}] Chequeando {side.upper()}...")
    try:
        report = compute_score(side, SNAPSHOT_SECS)
        score  = report.get("total_score", 0)
        verdict= report.get("verdict", "?")
        price  = report.get("mark_price", 0)
        print(f"  Score {side.upper()}: {score}/100 — {verdict} — ${price:,.1f}")
        return report
    except Exception as e:
        print(f"  ❌ Error: {e}")
        return {}

def notify_aplus(report: dict, side: str):
    """Manda alerta A+ a Telegram y macOS."""
    score  = report.get("total_score", 0)
    price  = report.get("mark_price", 0)
    b      = report.get("breakdown", {})
    snap   = report.get("snapshot_summary", {})
    connected = ", ".join(report.get("connected", [])).upper()

    # Breakdown resumido
    c1 = b.get("c1_cross_exchange", {})
    c2 = b.get("c2_delta_magnitude", {})
    c3 = b.get("c3_institutional", {})
    c4 = b.get("c4_funding", {})
    c5 = b.get("c5_divergence_toptraders", {})

    delta_m = snap.get("agg_delta_usd", 0) / 1_000_000
    huge_b  = snap.get("huge_buy_usd", 0) / 1_000_000
    huge_s  = snap.get("huge_sell_usd", 0) / 1_000_000

    emoji   = "🟢🚀" if side == "long" else "🟢🔻"
    title   = f"A+ {side.upper()} DETECTADO — Score {score}/100"
    body    = (
        f"{emoji} *ENTRADA A+ {side.upper()}* — BTC ${price:,.0f}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"*Score:* `{score}/100` ✅\n"
        f"*Exchanges:* `{connected}`\n"
        f"*Delta:* `${delta_m:+.1f}M`\n"
        f"*Huge BUY:* `${huge_b:.1f}M` | *Huge SELL:* `${huge_s:.1f}M`\n"
        f"*Divergencia:* `{snap.get('divergence','?')}`\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"C1 Exchanges: `{c1.get('score',0)}/25`\n"
        f"C2 Delta:     `{c2.get('score',0)}/20`\n"
        f"C3 Inst OKX:  `{c3.get('score',0)}/20`\n"
        f"C4 Funding:   `{c4.get('score',0)}/15`\n"
        f"C5 Diverg:    `{c5.get('score',0)}/20`\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⚡ *Validar setup técnico en TradingView y ejecutar en Bybit*"
    )

    # Botones de acción en Telegram
    keyboard = [[
        {"text": "✅ ENTRAR", "callback_data": f"enter:{side}:{price}"},
        {"text": "⏭ SKIP",   "callback_data": f"skip:{side}:{price}"},
    ]]

    print(f"\n  🚨 ALERTA A+ {side.upper()} — Notificando Telegram...")
    telegram_send(title, body, inline_keyboard=keyboard)
    macos_notify(title, f"Score {score}/100 — BTC ${price:,.0f}", sound="Submarine")

def notify_signal_weak(report: dict, side: str):
    """Manda alerta de señal débil (score 65-69) — cerca del A+."""
    score = report.get("total_score", 0)
    price = report.get("mark_price", 0)
    title = f"⚠️ CASI A+ {side.upper()} — Score {score}/100"
    body  = (
        f"*Score:* `{score}/100` — falta poco para A+\n"
        f"*BTC:* `${price:,.0f}`\n"
        f"Monitorear de cerca — puede llegar a 70 pronto"
    )
    print(f"  ⚠️  Señal débil cerca de A+ ({score}/100) — notificando")
    telegram_send(title, body)
    macos_notify(title, f"Score {score}/100", sound="Glass")

def run_monitor(sides: list, interval: int):
    print(f"""
╔══════════════════════════════════════════════════════╗
║   A+ MONITOR — Wally Trader                        ║
╚══════════════════════════════════════════════════════╝
  Monitoreando: {', '.join(s.upper() for s in sides)}
  Intervalo:    cada {interval//60} minutos
  Umbral A+:    score ≥ 70/100
  Umbral warn:  score ≥ 65/100

  Ctrl+C para detener
""")

    # Enviar mensaje de inicio a Telegram
    telegram_send(
        "🤖 A+ Monitor iniciado",
        f"Monitoreando BTC {'/'.join(s.upper() for s in sides)} cada {interval//60} min\nUmbral A+: 70/100"
    )

    last_alert = {}   # evitar spam — no alertar el mismo lado dos veces seguidas

    while True:
        for side in sides:
            report = check_side(side)
            if not report:
                continue

            score = report.get("total_score", 0)

            if score >= 70:
                # Solo alertar si no alertamos el mismo lado en el ciclo anterior
                if last_alert.get(side) != "aplus":
                    notify_aplus(report, side)
                    last_alert[side] = "aplus"
                else:
                    print(f"  (A+ {side.upper()} ya notificado — esperando cambio)")

            elif score >= 65:
                if last_alert.get(side) != "weak":
                    notify_signal_weak(report, side)
                    last_alert[side] = "weak"

            else:
                # Reset — si baja del umbral, permitir nueva alerta
                if last_alert.get(side):
                    last_alert[side] = None

        print(f"\n  ⏳ Próxima revisión en {interval//60} min ({now_str()})")
        time.sleep(interval)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--side",  choices=["long", "short"], help="Monitorear un solo lado")
    group.add_argument("--both",  action="store_true", help="Monitorear long Y short")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                        help=f"Intervalo en segundos (default {DEFAULT_INTERVAL})")
    args = parser.parse_args()

    sides = ["long", "short"] if args.both else [args.side]

    try:
        run_monitor(sides, args.interval)
    except KeyboardInterrupt:
        print("\n\n  Monitor detenido.")
        telegram_send("🛑 A+ Monitor detenido", "El monitor fue detenido manualmente.")
