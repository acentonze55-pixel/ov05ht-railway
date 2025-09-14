# -*- coding: utf-8 -*-
"""
Monitor HT Over 0.5 (LIVE) + Kelly, ristretto ai soli fixture_id della WHITELIST.
La whitelist può arrivare da:
- WHITELIST_URL  (JSON array oppure CSV con colonna 'fixture_id')
- WHITELIST      (lista di ID separati da virgola)
Se REQUIRE_WHITELIST=1 e la whitelist è vuota, non invia alert.
"""

import os
import time
import json
import math
import csv
import io
import traceback
from datetime import datetime
import requests

API_HOST = "https://v3.football.api-sports.io"
API_KEY = os.getenv("API_FOOTBALL_KEY", "").strip()
HEADERS = {"x-apisports-key": API_KEY}

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "").strip()

DEBUG = int(os.getenv("DEBUG", "0") or 0)

THRESHOLD     = float(os.getenv("THRESHOLD", "1.50") or 1.50)
POLL_SECONDS  = int(os.getenv("POLL_SECONDS", "12") or 12)
PTHRESH       = float(os.getenv("PTHRESH", "0.72") or 0.72)

KELLY_SCALE   = float(os.getenv("KELLY_SCALE", "0.5") or 0.5)
BANKROLL_FILE = os.getenv("BANKROLL_FILE", "/app/bankroll.json")
BANKROLL_START= float(os.getenv("BANKROLL_START", "100.0") or 100.0)
MAX_STAKE_PCT = float(os.getenv("MAX_STAKE_PCT", "0.05") or 0.05)
MIN_STAKE     = float(os.getenv("MIN_STAKE", "1.0") or 1.0)

MINUTE_MIN    = int(os.getenv("MINUTE_MIN", "1") or 1)
MINUTE_MAX    = int(os.getenv("MINUTE_MAX", "45") or 45)

# --- Whitelist settings ---
WHITELIST_URL       = os.getenv("WHITELIST_URL", "").strip()
WHITELIST_INLINE    = os.getenv("WHITELIST", "").strip()  # "123,456,789"
REQUIRE_WHITELIST   = int(os.getenv("REQUIRE_WHITELIST", "1") or 1)
WHITELIST_REFRESH_S = int(os.getenv("WHITELIST_REFRESH_S", "300") or 300)  # 5 minuti


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def tg_send(text: str):
    if not BOT_TOKEN or not CHAT_ID:
        log("[TELEGRAM] BOT_TOKEN o CHAT_ID mancanti")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": CHAT_ID, "text": text}, timeout=20)
    except Exception as e:
        log(f"[TELEGRAM] errore invio: {e}")


def _api_get(path: str, params: dict):
    r = requests.get(f"{API_HOST}{path}", headers=HEADERS, params=params, timeout=20)
    r.raise_for_status()
    return r.json().get("response", [])


def fetch_odds_any(fixture_id: int, debug=False):
    try:
        live = _api_get("/odds/live", {"fixture": fixture_id})
        if debug: log(f"[ODDS] live items={len(live)} for {fixture_id}")
        if live:
            return live
    except Exception as e:
        if debug: log(f"[ODDS] live error: {e}")
    try:
        pre = _api_get("/odds", {"fixture": fixture_id})
        if debug: log(f"[ODDS] prematch items={len(pre)} for {fixture_id}")
        return pre
    except Exception as e:
        if debug: log(f"[ODDS] prematch error: {e}")
        return []


def pick_ht_over05_price(odds_resp, debug=False):
    MARKET_NAMES = {
        "1st Half - Over/Under",
        "Over/Under 1st Half",
        "Over/Under (1st Half)",
        "Goals Over/Under 1st Half",
        "1st Half Goals Over/Under",
        "1st Half Total Goals",
        "1st Half Goals",
        "First Half - Over/Under",
        "Over/Under First Half",
        "1st Half - Total Goals",
        "1st Half Goal Line",
    }
    VALUE_NAMES = {"Over 0.5", "Over0.5", "Over 0.5 Goals"}

    best = None
    best_book = None
    seen_markets = set()

    for item in odds_resp or []:
        for book in item.get("bookmakers", []):
            bname = book.get("name", "")
            for bet in book.get("bets", []):
                mk = (bet.get("name") or "").strip()
                seen_markets.add(mk)
                if mk not in MARKET_NAMES:
                    continue
                for v in bet.get("values", []):
                    val = (v.get("value") or "").strip()
                    if not any(val.startswith(x) for x in VALUE_NAMES):
                        continue
                    try:
                        price = float(v.get("odd"))
                    except Exception:
                        continue
                    if (best is None) or (price > best):
                        best = price
                        best_book = bname

    if debug and best is None:
        markets_sorted = ", ".join(sorted(seen_markets))
        log(f"[NO-ODDS] nessuna HT O0.5 — mercati visti: {markets_sorted}")

    return best, best_book


def load_bankroll():
    try:
        with open(BANKROLL_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"bankroll": BANKROLL_START, "pending": {}, "history": []}


def save_bankroll(data):
    try:
        with open(BANKROLL_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"[BANKROLL] save error: {e}")


def kelly_fraction(p: float, odds: float) -> float:
    b = max(0.0, odds - 1.0)
    if b <= 0:
        return 0.0
    q = 1.0 - p
    f = (b * p - q) / b
    return max(0.0, f)


def suggest_stake(bankroll: float, p_est: float, odds: float) -> float:
    f_full = kelly_fraction(p_est, odds)
    f_scaled = f_full * KELLY_SCALE
    max_cap = bankroll * MAX_STAKE_PCT
    stake = bankroll * f_scaled
    stake = min(max_cap, stake)
    stake = max(0.0, stake)
    if stake < MIN_STAKE:
        return 0.0
    return round(stake, 2)


def estimate_p_ht_over05(_fixture) -> float:
    return PTHRESH


def get_live_fixtures():
    try:
        resp = _api_get("/fixtures", {"live": "all"})
    except Exception as e:
        if DEBUG: log(f"[LIVE] error: {e}")
        return []
    out = []
    for item in resp:
        fx = item.get("fixture", {})
        st = fx.get("status", {}) or {}
        short = st.get("short", "")
        elapsed = st.get("elapsed", 0) or 0
        if short != "1H":
            continue
        if not (MINUTE_MIN <= int(elapsed) <= MINUTE_MAX):
            continue
        out.append(item)
    return out


# ---------- WHITELIST ----------
_last_wl_load = 0
_whitelist_ids = set()

def parse_ids_from_inline(text: str):
    out = set()
    for tok in text.replace(" ", "").split(","):
        if tok.isdigit():
            out.add(int(tok))
    return out

def load_whitelist(force=False):
    global _last_wl_load, _whitelist_ids
    now = time.time()
    if not force and (now - _last_wl_load < WHITELIST_REFRESH_S):
        return _whitelist_ids
    ids = set()

    # 1) URL
    if WHITELIST_URL:
        try:
            r = requests.get(WHITELIST_URL, timeout=20)
            r.raise_for_status()
            ct = (r.headers.get("Content-Type") or "").lower()
            if "json" in ct or r.text.strip().startswith("["):
                data = r.json()
                for x in data:
                    try:
                        ids.add(int(x))
                    except: pass
            else:
                # prova CSV
                f = io.StringIO(r.text)
                rd = csv.DictReader(f)
                if "fixture_id" in (rd.fieldnames or []):
                    for row in rd:
                        try:
                            ids.add(int(row.get("fixture_id")))
                        except: pass
        except Exception as e:
            log(f"[WHITELIST] URL error: {e}")

    # 2) INLINE (env)
    if WHITELIST_INLINE:
        ids |= parse_ids_from_inline(WHITELIST_INLINE)

    _whitelist_ids = ids
    _last_wl_load = now
    if DEBUG:
        log(f"[WHITELIST] loaded {len(ids)} ids")
    return _whitelist_ids


def main():
    if not API_KEY:
        log("⚠️ API_FOOTBALL_KEY mancante")
        return

    data = load_bankroll()
    notified = set()

    log(f"Avvio monitor (whitelist-mode). THRESH={THRESHOLD} P~{PTHRESH} Kelly={KELLY_SCALE} BR={data['bankroll']:.2f}")

    while True:
        try:
            wl = load_whitelist()
            if REQUIRE_WHITELIST and not wl:
                log("[SAFE] Whitelist vuota → nessun alert (REQUIRE_WHITELIST=1)")
                time.sleep(POLL_SECONDS)
                continue

            lives = get_live_fixtures()
            if DEBUG: log(f"[HB] live_1H={len(lives)} wl={len(wl)}")

            for it in lives:
                fx = it.get("fixture", {})
                fid = int(fx.get("id"))
                if wl and fid not in wl:
                    # fuori dal tuo metodo → skip
                    continue

                st = fx.get("status", {}) or {}
                elapsed = st.get("elapsed", 0) or 0

                league = it.get("league", {}) or {}
                teams  = it.get("teams", {}) or {}
                home   = teams.get("home", {}).get("name", "Home")
                away   = teams.get("away", {}).get("name", "Away")
                lname  = league.get("name", "League")

                key = f"{fid}_{elapsed}"
                if key in notified:
                    continue

                p_est = estimate_p_ht_over05(it)

                odds_resp = fetch_odds_any(fid, debug=DEBUG)
                price, book = pick_ht_over05_price(odds_resp, debug=DEBUG)
                if price is None:
                    if DEBUG: log(f"[NO-ODDS] {fid} nessuna quota HT 0.5")
                    continue

                if DEBUG:
                    log(f"[ODDS] {fid} {home}-{away} {lname} m{elapsed}  best={price:.2f}@{book} p~{p_est:.2f}")

                if price >= THRESHOLD:
                    bankroll = float(data.get("bankroll", BANKROLL_START))
                    stake = suggest_stake(bankroll, p_est, price)

                    lines = [
                        f"▶️ OV 0.5 HT @ {price:.2f} ({book})",
                        f"{home} — {away}",
                        f"{lname} | min {elapsed}",
                        f"p≈{p_est:.2f}  bankroll={bankroll:.2f}",
                    ]
                    if stake > 0:
                        lines.append(f"💰 Stake (Kelly {KELLY_SCALE:g}x, cap {int(MAX_STAKE_PCT*100)}%): {stake:.2f}")
                        data["pending"][str(fid)] = {
                            "ts": int(time.time()),
                            "stake": stake,
                            "odds": price,
                            "p": p_est,
                            "home": home,
                            "away": away,
                            "league": lname
                        }
                        save_bankroll(data)
                    else:
                        lines.append("💰 Stake molto piccolo → non suggerito")

                    tg_send("\n".join(lines))
                    log(f"[ALERT] {fid} {home}-{away} @{price:.2f} ({book}) stake={stake:.2f}")
                    notified.add(key)

            time.sleep(POLL_SECONDS)

        except KeyboardInterrupt:
            log("Stop richiesto.")
            break
        except Exception as e:
            log(f"[ERR] {e}")
            if DEBUG:
                traceback.print_exc()
            time.sleep(3)


if __name__ == "__main__":
    main()
