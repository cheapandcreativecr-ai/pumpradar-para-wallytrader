import os, sys, time, requests
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))
from notify_hub import notify_pump, notify_dump

API_KEY  = "CG-bBRxPDEiRPDVxw2Zvd3WXwMo"
BASE_URL = "https://api.coingecko.com/api/v3"
HEADERS  = {"x-cg-demo-api-key": API_KEY}
PAGES    = [3, 4, 5, 6, 7]
MIN_CHANGE = 15.0
MAX_MCAP   = 50_000_000
MIN_VOL    = 10_000

_seen = set()

def scan(dry_run=False):
    now = datetime.now().strftime("%H:%M:%S")
    print(f"[{now}] Escaneando mercado...")
    candidates = []
    for page in PAGES:
        try:
            r = requests.get(f"{BASE_URL}/coins/markets", params={
                "vs_currency": "usd", "order": "volume_desc",
                "per_page": 50, "page": page,
                "price_change_percentage": "24h",
            }, headers=HEADERS, timeout=10)
            r.raise_for_status()
            for c in r.json():
                chg  = c.get("price_change_percentage_24h", 0) or 0
                mcap = c.get("market_cap", 0) or 0
                vol  = c.get("total_volume", 0) or 0
                if abs(chg) >= MIN_CHANGE and mcap <= MAX_MCAP and vol >= MIN_VOL:
                    candidates.append({
                        "id": c["id"], "symbol": c["symbol"].upper(),
                        "name": c["name"], "chain": "unknown",
                        "price": c.get("current_price", 0) or 0,
                        "mcap": mcap, "volume24h": vol, "change24h": chg,
                        "score": min(100, int(abs(chg) / 2)),
                        "pattern_match": 0, "rsi": 0,
                        "volume_spike": round(vol / max(mcap * 0.01, 1), 1),
                        "exchange_inflow": 1.0, "matched_with": [], "inflow_usd": 0,
                    })
            time.sleep(0.5)
        except Exception as e:
            print(f"  Pagina {page} error: {e}")

    candidates.sort(key=lambda x: x["change24h"], reverse=True)
    print(f"  Encontrados: {len(candidates)} candidatos")

    sent = 0
    for token in candidates:
        key = f"{token['id']}:{int(token['change24h'])}"
        if key in _seen:
            continue
        _seen.add(key)
        sig = "PUMP" if token["change24h"] >= MIN_CHANGE else "DUMP"
        print(f"  [{sig}] {token['symbol']:10} {token['change24h']:+.1f}%")
        if not dry_run:
            if sig == "PUMP":
                notify_pump(token)
            else:
                notify_dump(token)
            time.sleep(1)
            sent += 1

    print(f"  Alertas enviadas: {sent}")
    return candidates

if __name__ == "__main__":
    dry_run = "--test" in sys.argv
    if dry_run:
        print("MODO TEST")
    scan(dry_run=dry_run)
