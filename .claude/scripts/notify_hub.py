"""Multi-channel notification hub.

Channels:
- macOS (osascript) — immediate, no dependencies
- dashboard.md — human-readable append/rewrite
- notifications.log — append-only audit trail
- Telegram (stub v1 — returns False if no token) — v2 integration
- Email via Resend (stub v1 — returns False if no key) — v3 integration

Urgency tiers control which channels fire.
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from enum import IntEnum
from pathlib import Path


class Urgency(IntEnum):
    HEARTBEAT = 0
    INFO = 1
    WARN = 2
    CRITICAL = 3


def _repo_root() -> Path:
    env = os.environ.get("WALLY_REPO_ROOT")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "CLAUDE.md").exists() and (parent / ".claude").is_dir():
            return parent
    raise RuntimeError("Could not locate wally-trader repo root")


def _now_mx() -> str:
    from datetime import timedelta, timezone as _tz
    mx = datetime.now(_tz.utc) - timedelta(hours=6)
    return mx.strftime("%Y-%m-%d %H:%M:%S CR")


# ---------- Channel: macOS -----------------------------------

def macos_notify(title: str, body: str, sound: str = "Glass") -> bool:
    """osascript display notification. Returns True on success."""
    # escape double quotes
    t = title.replace('"', "'")
    b = body.replace('"', "'")
    cmd = [
        "osascript",
        "-e",
        f'display notification "{b}" with title "{t}" sound name "{sound}"',
    ]
    try:
        subprocess.run(cmd, check=True, timeout=5, capture_output=True)
        return True
    except Exception:
        return False


# ---------- Channel: Telegram --------------------------------

def telegram_send(title: str, body: str, inline_keyboard: list = None) -> bool:
    """Send Telegram message with optional inline keyboard.

    inline_keyboard: list of list of dicts {text, callback_data}
    Example: [[{"text": "Close", "callback_data": "close"}, {"text": "Hold", "callback_data": "hold"}]]
    """
    # Cargar .env si no están en el entorno
    token = "8484825731:AAEVz1QOFnk9EXA61afB0oacpHptMMIPvMI"
    chat_id = "8520996933"
    if not token or not chat_id:
        return False

    try:
        import urllib.request
        import urllib.parse
        import ssl as _ssl
        _ctx=_ssl.create_default_context(); _ctx.check_hostname=False; _ctx.verify_mode=_ssl.CERT_NONE
        text = f"*{title}*\n{body}" if title else body
        params = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
        if inline_keyboard:
            import json as _json
            keyboard = {"inline_keyboard": inline_keyboard}
            params["reply_markup"] = _json.dumps(keyboard)
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=5, context=_ctx) as resp:
            return resp.status == 200
    except Exception as e:
        try:
            from datetime import datetime, timezone
            log_path = _repo_root() / ".claude/cache/notify_errors.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a") as f:
                f.write(f"{datetime.now(timezone.utc).isoformat()} | telegram_send failed: {e}\n")
        except Exception:
            pass
        return False


# ---------- Channel: Email (stub) ----------------------------

def email_send(title: str, body: str) -> bool:
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        return False  # silent no-op (v1 stub)
    # Full implementation in v3
    return False


# ---------- Channel: Dashboard file --------------------------

def write_to_dashboard(urgency: Urgency, event: str, payload: dict) -> None:
    """Append a line to .claude/watcher/dashboard.md events section.

    Full dashboard re-render is done by watcher_tick.py. This only appends to
    the 'Recent events' footer.
    """
    path = _repo_root() / ".claude" / "watcher" / "dashboard.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (
        f"- `{_now_mx()}` [{urgency.name}] **{event}** — "
        f"{payload.get('order_id', '-')} "
        f"({payload.get('profile', '-')}:{payload.get('asset', '-')})"
    )
    # Append at end of file; watcher_tick rewrites the header each run
    with path.open("a") as f:
        f.write(line + "\n")


# ---------- Channel: Log -------------------------------------

def append_to_log(urgency: Urgency, event: str, payload: dict) -> None:
    path = _repo_root() / ".claude" / "scripts" / "notifications.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "at": _now_mx(),
        "urgency": urgency.name,
        "event": event,
        "payload": payload,
    }
    with path.open("a") as f:
        f.write(json.dumps(entry) + "\n")


# ---------- Event formatter ----------------------------------

_SOUNDS = {
    Urgency.HEARTBEAT: None,
    Urgency.INFO: "default",
    Urgency.WARN: "Glass",
    Urgency.CRITICAL: "Submarine",
}


def format_event(event: str, payload: dict) -> tuple[str, str]:
    """Return (title, body) for an event + payload."""
    pid = payload.get("order_id", "-")
    profile = payload.get("profile", "-")
    asset = payload.get("asset", "-")
    side = payload.get("side", "")
    entry = payload.get("entry", "-")

    if event == "triggered_go":
        title = f"TRIGGER GO — {profile} {asset} {side}"
        body = (
            f"Entry {entry} | SL {payload.get('sl','-')} | TP1 {payload.get('tp1','-')} | "
            f"filtros {payload.get('filters_passed','?')}/{payload.get('filters_total','?')} OK"
        )
    elif event == "near_entry":
        title = f"Precio cerca entry — {profile} {asset}"
        body = (
            f"Dist {payload.get('distance_pct','?')}% de {entry} — Claude validando"
        )
    elif event == "invalidated_price":
        title = f"Invalidated — {profile} {asset}"
        body = f"Precio rompió {payload.get('invalidation_price','-')}"
    elif event == "invalidated_stopday":
        title = f"Stop-day — {profile}"
        body = f"2 SLs hoy — pendings del profile canceladas ({pid})"
    elif event in ("expired_ttl", "expired_force_exit"):
        title = f"Expired — {profile} {asset}"
        body = f"Orden {pid} expiró sin fill ({event})"
    elif event == "order_created":
        title = f"Order queued — {profile} {asset} {side}"
        body = f"Entry {entry} — watcher vigilando"
    elif event == "suspended_switch":
        title = f"Suspended — profile switch"
        body = f"Orden {pid} en {profile} pausada (switch)"
    elif event == "re_analysis_suggested":
        title = f"Re-análisis sugerido — {profile}"
        body = f"Próxima revisión: {payload.get('next_recheck_mx','-')}"
    elif event == "degraded_watcher":
        title = f"Watcher degraded — {profile}"
        body = f"Claude validation failed para {pid} — revisa manual"
    elif event == "filled":
        title = f"Filled — {profile} {asset}"
        body = f"Orden {pid} ejecutada @ {payload.get('filled_price', entry)}"
    else:
        title = f"Wally — {event}"
        body = f"{pid} ({profile}:{asset})"
    return title, body


# ---------- Main dispatcher ----------------------------------

def notify(urgency: Urgency, event: str, payload: dict) -> None:
    """Main entry point. Dispatches to channels based on urgency."""
    append_to_log(urgency, event, payload)
    write_to_dashboard(urgency, event, payload)

    if urgency >= Urgency.INFO:
        title, body = format_event(event, payload)
        sound = _SOUNDS.get(urgency, "Glass") or "default"
        macos_notify(title, body, sound=sound)

    if urgency >= Urgency.WARN:
        title, body = format_event(event, payload)
        telegram_send(title, body)

    if urgency >= Urgency.CRITICAL:
        title, body = format_event(event, payload)
        email_send(title, body)


if __name__ == "__main__":
    # CLI: python3 notify_hub.py --test
    import sys
    if "--test" in sys.argv:
        notify(Urgency.INFO, "order_created", {
            "order_id": "test_001",
            "profile": "retail",
            "asset": "BTCUSDT.P",
            "side": "LONG",
            "entry": 77521,
        })
        print("Test notification sent.")



# PumpRadar dispatchers

def _keyboard(tid, action):
    return {"inline_keyboard": [[
        {"text": "OK " + action, "callback_data": "approve:" + tid + ":" + action},
        {"text": "X RECHAZAR",   "callback_data": "reject:"  + tid + ":" + action},
    ]]}

def notify_pump(token):
    append_to_log(Urgency.CRITICAL, "pump_detected", token)
    write_to_dashboard(Urgency.CRITICAL, "pump_detected", token)
    sym   = token.get("symbol", token.get("name", "?"))
    chain = token.get("chain", "?")
    ce    = {"SOL": "OO", "ETH": "ET", "BNB": "BN"}.get(chain, "")
    title = "PUMP -- " + sym
    score = str(token.get("score", "?"))
    price = "{:.8f}".format(token.get("price", 0))
    chg   = "{:+.1f}".format(token.get("change24h", 0))
    pm    = str(token.get("pattern_match", "?"))
    rsi   = str(token.get("rsi", "?"))
    vol   = str(token.get("volume_spike", "?"))
    mw    = ", ".join(token.get("matched_with", ["?"]))
    body  = ("*Chain:* " + ce + chain + " | *Score:* `" + score + "/100`\n"
             + "*Precio:* `$" + price + "` | *24h:* `" + chg + "%`\n"
             + "*Match:* `" + pm + "%` | *RSI:* `" + rsi + "` | *Vol:* `" + vol + "x`\n"
             + "Similar a: _" + mw + "_")
    macos_notify(title, body[:100], sound="Submarine")
    tid = token.get("id", sym)
    kb  = _keyboard(tid, "BUY")
    telegram_send(title, body, inline_keyboard=kb["inline_keyboard"])

def notify_dump(token):
    append_to_log(Urgency.CRITICAL, "dump_detected", token)
    write_to_dashboard(Urgency.CRITICAL, "dump_detected", token)
    sym   = token.get("symbol", token.get("name", "?"))
    chain = token.get("chain", "?")
    ce    = {"SOL": "OO", "ETH": "ET", "BNB": "BN"}.get(chain, "")
    title = "DUMP -- " + sym
    chg   = "{:+.1f}".format(token.get("change24h", 0))
    infl  = str(token.get("exchange_inflow", "?"))
    rsi   = str(token.get("rsi", "?"))
    vol   = str(token.get("volume_spike", "?"))
    usd   = "{:,.0f}".format(token.get("inflow_usd", 0))
    body  = ("*Chain:* " + ce + chain + " | *24h:* `" + chg + "%`\n"
             + "Inflow: `" + infl + "x` | RSI: `" + rsi + "` | Vol: `" + vol + "x`\n"
             + "USD en exchanges: `$" + usd + "`")
    macos_notify(title, body[:100], sound="Submarine")
    tid = token.get("id", sym)
    kb  = _keyboard(tid, "SELL")
    telegram_send(title, body, inline_keyboard=kb["inline_keyboard"])

def notify_inflow(token):
    append_to_log(Urgency.WARN, "inflow_alert", token)
    write_to_dashboard(Urgency.WARN, "inflow_alert", token)
    sym  = token.get("symbol", token.get("name", "?"))
    infl = str(token.get("exchange_inflow", "?"))
    usd  = "{:,.0f}".format(token.get("inflow_usd", 0))
    ww   = str(token.get("whale_wallets", "?"))
    title = "INFLOW -- " + sym
    body  = "Inflow: `" + infl + "x` | `$" + usd + "` USD | Wallets: `" + ww + "`"
    macos_notify(title, body[:100], sound="Glass")
    telegram_send(title, body)

def notify_pattern(token):
    append_to_log(Urgency.CRITICAL, "pattern_matched", token)
    write_to_dashboard(Urgency.CRITICAL, "pattern_matched", token)
    sym  = token.get("symbol", token.get("name", "?"))
    pm   = str(token.get("pattern_match", "?"))
    sc   = str(token.get("score", "?"))
    mw   = ", ".join(token.get("matched_with", ["?"]))
    title = "PATRON -- " + sym
    body  = "*Match:* `" + pm + "%` | *Score:* `" + sc + "/100`\nSimilar a: _" + mw + "_"
    macos_notify(title, body[:100], sound="Submarine")
    tid = token.get("id", sym)
    kb  = _keyboard(tid, "BUY")
    telegram_send(title, body, inline_keyboard=kb["inline_keyboard"])
