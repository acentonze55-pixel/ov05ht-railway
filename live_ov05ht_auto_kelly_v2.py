# -*- coding: utf-8 -*-
"""
live_ov05ht_auto_kelly_v2.py
Monitor live per Over 0.5 Primo Tempo con alert Telegram.
Patch: BYPASS_PREFILTER, ricerca robusta mercato HT O0.5, counters diagnostici.

Requisiti (requirements.txt):
    requests
    python-dotenv

Procfile:
    worker: python live_ov05ht_auto_kelly_v2.py
"""

import os
import sys
import time
import math
import json
import traceback
from typing import Dict, Any, List, Optional, Tuple

import requests

# ------------------------ ENV ------------------------
API_HOST = "https://v3.football.api-sports.io"

API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Parametri operativi
POLL_SECONDS   = int(os.getenv("POLL_SECONDS", "12") or 12)     # polling live
THRESHOLD      = float(os.getenv("THRESHOLD", "1.50") or 1.50)  # soglia quota HT O0.5
PTHRESH        = float(os.getenv("PTHRESH", "0.70") or 0.70)    # usato dal prefilter (se attivo)
MIN_PLAYED     = int(os.getenv("MIN_PLAYED", "6") or 6)         # usato dal prefilter (se attivo)
NO_FORM        = int(os.getenv("NO_FORM", "0") or 0)            # se 1, ignora filtro forma
DEBUG          = int(os.getenv("DEBUG", "0") or 0)              # log verboso
BYPASS_PREFILTER = int(os.getenv("BYPASS_PREFILTER", "0") or 0) # se 1, salta completamente il picker

# Book vincolanti (se stringa vuota = usa qualunque)
ONLY_BOOKS_RAW = os.getenv("ONLY_BOOKS", "")
ONLY_BOOKS_SET = {b.strip() for b in ONLY_BOOKS_RAW.split(",") if b.strip()} or None

# Kelly (conservativo)
KELLY_FACTOR   = float(os.getenv("KELLY_FACTOR", "0.25") or 0.25)  # frazione Kelly
BANKROLL       = float(os.getenv("BANKROLL", "1000") or 1000.0)    # bankroll virtuale per suggerire stake

# -------------------- HTTP helpers --------------------
HEADERS = {"x-apisports-key": API_FOOTBALL_KEY}

def api_get(path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{API_HOST}{path}"
    r = requests.get(url, headers=HEADERS, params=params, timeout=30)
    r.raise_for_status()
    return r.json()

def get_live_fixtures() -> List[Dict[str, Any]]:
    # Tutte le partite live
    data = api_get("/fixtures", {"live": "all"})
    return data.get("response", [])

def get_odds_for_fixture(fixture_id: int) -> List[Dict[str, Any]]:
    # Odds (pre + event. live dove disponibile)
    data = api_get("/odds", {"fixture": fixture_id})
    return data.get("response", [])

# ----------------- Odds market picker -----------------
def pick_ht_over05_price(odds_resp: list, only_books: Optional[set]) -> Optional[Tuple[str, float]]:
    """
    Trova (book_name, price) per Over 0.5 1st Half, scorrendo più varianti di mercato
    che API-Football può usare.
    Ritorna la miglior quota fra i book ammessi (o tutti, se only_books=None).
    """
    MARKET_NAMES = {
        "1st Half - Over/Under",
        "Over/Under 1st Half",
        "Goals Over/Under - 1st Half",
        "1st Half Goals Over/Under",
        "First Half Goals Over/Under",
        "First Half - Total Goals",
        "1st Half Total Goals",
    }

    best = None  # (book_name, price)
    for item in odds_resp:
        for book in item.get("bookmakers", []):
            bname = str(book.get("name") or "").strip()
            if only_books and bname not in only_books:
                continue
            for market in book.get("bets", []):
                mname = str(market.get("name") or "").strip()
                if mname not in MARKET_NAMES:
                    continue
                for val in market.get("values", []):
                    vname = str(val.get("value") or "").strip()
                    # Varianti: "Over 0.5", "Over 0.5 (HT)", ecc.
                    if not vname.lower().startswith("over 0.5"):
                        continue
                    odd = val.get("odd")
                    try:
                        price = float(odd)
                    except Exception:
                        continue
                    if (best is None) or (price > best[1]):
                        best = (bname, price)
    return best

# ------------------ Prefilter (picker) ----------------
def prefilter_fixture(f: Dict[str, Any]) -> bool:
    """
    Qui ci andrebbe la logica del "picker" (metodo che seleziona i match con pHT alto).
    Per ora:
      - se BYPASS_PREFILTER=1 => True
      - se NO_FORM=1 => True
      - altrimenti True (placeholder)
    Così possiamo testare a pieno la catena live-odds.
    """
    if BYPASS_PREFILTER:
        return True
    if NO_FORM:
        return True

    # TODO: inserire i tuoi criteri reali (es. pHT stimata >= PTHRESH, MIN_PLAYED, ecc.)
    # Per ora non scartiamo nulla:
    return True

# ---------------------- Telegram ----------------------
def tg_send(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Telegram non configurato (TOKEN/CHAT_ID mancanti)")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        r = requests.post(url, data=payload, timeout=15)
        if r.status_code != 200:
            print(f"[TG] HTTP {r.status_code} -> {r.text}")
    except Exception as e:
        print(f"[TG] errore: {e}")

# -------------------- Kelly (conservativo) ------------
def kelly_stake(bankroll: float, price: float, p_est: float, kfactor: float = 0.25) -> float:
    """
    Kelly frazionario: f* = [(p * (odds-1)) - (1-p)] / (odds-1)
    Usiamo una p_est conservativa = max(1/odds + piccolo margine, 0).
    Se edge <= 0 => stake 0.
    """
    b = price - 1.0
    edge = (p_est * b) - (1 - p_est)
    if b <= 0 or edge <= 0:
        return 0.0
    f_star = edge / b
    return max(0.0, kfactor * f_star * bankroll)

def conservative_p_est(price: float) -> float:
    # Senza modello: prendiamo la probabilità implicita del mercato e aggiungiamo
    # un micro-edge di test (qui zero). Puoi spingere +0.02 se vuoi “aprire” qualche pick.
    base = 1.0 / max(1e-9, price)
    p = base  # + 0.00
    return min(0.99, max(0.01, p))

# ----------------------- Main loop --------------------
def main():
    if not API_FOOTBALL_KEY:
        print("⚠️  API_FOOTBALL_KEY mancante")
        sys.exit(1)

    open_bets = set()  # fixture_id già notificati (evita duplicati nella stessa fase)
    print("[INIT] live monitor avviato "
          f"(THRESHOLD={THRESHOLD}, POLL={POLL_SECONDS}s, BYPASS_PREFILTER={BYPASS_PREFILTER}, "
          f"ONLY_BOOKS={','.join(ONLY_BOOKS_SET or []) or 'ANY'})")

    while True:
        try:
            fixtures = get_live_fixtures()
        except Exception as e:
            print(f"[ERR] get_live_fixtures: {e}")
            time.sleep(POLL_SECONDS)
            continue

        # Filtra solo partite nel 1° tempo (o appena HT)
        live_first_half: List[Dict[str, Any]] = []
        for f in fixtures:
            fs = f["fixture"]["status"]["short"]  # '1H', 'HT', '2H', ecc.
            minute = int(f["fixture"]["status"].get("elapsed") or 0)
            if fs in {"1H", "HT"} and 0 <= minute <= 46:
                live_first_half.append(f)

        # Counters diagnostici
        cnt_scanned = 0
        cnt_pref_ok = 0
        cnt_with_odds = 0
        cnt_ge_thr = 0

        for f in live_first_half:
            cnt_scanned += 1

            # Prefilter (picker)
            if not prefilter_fixture(f):
                if DEBUG:
                    print(f"[SKIP] prefilter KO: {f['teams']['home']['name']} - {f['teams']['away']['name']}")
                continue
            cnt_pref_ok += 1

            fid = f["fixture"]["id"]
            if fid in open_bets:
                # Già notificato in questa sessione
                continue

            # Odds
            try:
                odds_resp = get_odds_for_fixture(fid)
            except Exception as e:
                if DEBUG:
                    print(f"[NO-ODDS] fixture {fid} errore odds: {e}")
                continue

            found = pick_ht_over05_price(odds_resp, ONLY_BOOKS_SET)
            if not found:
                if DEBUG:
                    print(f"[NO-ODDS] {fid} nessuna quota HT O0.5 trovata")
                continue

            book, price = found
            cnt_with_odds += 1

            if price >= THRESHOLD:
                cnt_ge_thr += 1

                # Stima p e stake Kelly conservativo
                p_est = conservative_p_est(price)
                stake = kelly_stake(BANKROLL, price, p_est, KELLY_FACTOR)

                home = f["teams"]["home"]["name"]
                away = f["teams"]["away"]["name"]
                league = f["league"]["name"]
                minute = int(f["fixture"]["status"].get("elapsed") or 0)

                msg = (
                    f"⚽️ <b>OV 0.5 HT LIVE</b>\n"
                    f"{home} – {away}\n"
                    f"🏆 {league} | ⏱ {minute}'\n"
                    f"📈 Quota: <b>{price:.2f}</b>  ({book})\n"
                    f"💡 Kelly {KELLY_FACTOR:.2f}x: stake ≈ <b>{stake:.2f}</b> u "
                    f"(p≈{p_est:.02f})"
                )
                print(f"[PICK] fid={fid} {home}-{away} @ {price:.2f} ({book}) minute={minute}")
                tg_send(msg)

                open_bets.add(fid)

        # Heartbeat + stats
        print(f"[HB] 1H={len(live_first_half)} — scanned={cnt_scanned} pre_ok={cnt_pref_ok} "
              f"with_odds={cnt_with_odds} >=thr={cnt_ge_thr}")

        time.sleep(POLL_SECONDS)

# ------------------------ Run -------------------------
if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Bye!")
    except Exception:
        traceback.print_exc()
        sys.exit(1)
