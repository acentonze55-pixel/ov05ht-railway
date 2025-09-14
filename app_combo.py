# -*- coding: utf-8 -*-
"""
Railway COMBO service:
- Picker: costruisce una whitelist di fixture_id per oggi (criteri "strict" semplificati)
- Monitor: osserva SOLO i fixture della whitelist e manda alert Telegram su HT Over 0.5 >= THRESHOLD
- Web: FastAPI /health /whitelist.json /status

NOTE: il picker stima p(OV0.5 HT) da storico recente (ultime LOOKBACK gare) delle squadre,
conteggiando i match con almeno 1 gol nel 1° tempo. Filtra con PTHRESH e MIN_PLAYED.
"""

import os
import time
import json
import math
import threading
import traceback
from datetime import datetime, date, timedelta

import requests
from fastapi import FastAPI
from fastapi.responses import JSONResponse

# ------------- ENV -------------
API_HOST = "https://v3.football.api-sports.io"
API_KEY  = os.getenv("API_FOOTBALL_KEY", "").strip()
HEADERS  = {"x-apisports-key": API_KEY}

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Picker
PTHRESH       = float(os.getenv("PTHRESH", "0.72") or 0.72)
MIN_PLAYED    = int(os.getenv("MIN_PLAYED", "6") or 6)
LOOKBACK      = int(os.getenv("LOOKBACK", "8") or 8)      # quante gare recenti per stimare p
PAST_WINDOW_D = int(os.getenv("PAST_WINDOW_D", "90") or 90)  # fino a N gg indietro per trovare gare recenti
PICKER_EVERY_S= int(os.getenv("PICKER_EVERY_S", "900") or 900) # ogni 15'
LEAGUES       = os.getenv("LEAGUES", "").strip()  # es "39,78,135" (vuoto = tutte)

# Monitor
THRESHOLD    = float(os.getenv("THRESHOLD", "1.50") or 1.50)
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "12") or 12)
MINUTE_MIN   = int(os.getenv("MINUTE_MIN", "1") or 1)
MINUTE_MAX   = int(os.getenv("MINUTE_MAX", "45") or 45)

# Kelly / bankroll
KELLY_SCALE    = float(os.getenv("KELLY_SCALE", "0.5") or 0.5)  # frazione di Kelly
BANKROLL_FILE  = os.getenv("BANKROLL_FILE", "/app/bankroll.json")
BANKROLL_START = float(os.getenv("BANKROLL_START", "100.0") or 100.0)
MAX_STAKE_PCT  = float(os.getenv("MAX_STAKE_PCT", "0.05") or 0.05)
MIN_STAKE      = float(os.getenv("MIN_STAKE", "1.0") or 1.0)

DEBUG = int(os.getenv("DEBUG", "0") or 0)

# ---------------- STATE ----------------
app = FastAPI()
WL_LOCK = threading.Lock()
WHITELIST = set()                # fixture_id candidati oggi
WL_META = {"last_build": None, "count": 0, "log": []}

STATE = {
    "bankroll": BANKROLL_START,
    "pending": {},              # fixture_id -> {stake, odds, p, ...}
    "last_alerts": []           # ring buffer
}

def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

# ---------------- COMMON API-Football helpers ----------------
def _api(path: str, params: dict):
    r = requests.get(f"{API_HOST}{path}", headers=HEADERS, params=params, timeout=30)
    r.raise_for_status()
    return r.json().get("response", [])

def tg_send(text: str):
    if not BOT_TOKEN or not CHAT_ID:
        log("[TG] BOT_TOKEN/CHAT_ID mancanti")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text},
            timeout=20
        )
    except Exception as e:
        log(f"[TG] error: {e}")

# ---------------- Bankroll / Kelly ----------------
def load_bankroll():
    try:
        with open(BANKROLL_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data
    except Exception:
        return {"bankroll": BANKROLL_START, "pending": {}, "history": []}

def save_bankroll(data):
    try:
        with open(BANKROLL_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"[BANKROLL] save error: {e}")

def kelly_fraction(p, odds):
    b = max(0.0, odds - 1.0)
    if b <= 0: return 0.0
    q = 1.0 - p
    f = (b*p - q) / b
    return max(0.0, f)

def suggest_stake(bankroll, p_est, odds):
    f = kelly_fraction(p_est, odds) * KELLY_SCALE
    cap = bankroll * MAX_STAKE_PCT
    stake = max(0.0, bankroll * f)
    stake = min(stake, cap)
    if stake < MIN_STAKE:
        return 0.0
    return round(stake, 2)

# ---------------- Odds (HT Over 0.5) ----------------
HT_MARKETS = {
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
HT_VALUES = {"Over 0.5", "Over0.5", "Over 0.5 Goals"}

def fetch_odds_any(fixture_id):
    # prefer live
    try:
        live = _api("/odds/live", {"fixture": fixture_id})
        if live: return live
    except: pass
    try:
        pre = _api("/odds", {"fixture": fixture_id})
        return pre
    except:
        return []

def pick_ht_over05(odds_resp):
    best, book = None, None
    for it in odds_resp or []:
        for bk in it.get("bookmakers", []):
            bname = bk.get("name", "")
            for bet in bk.get("bets", []):
                mk = (bet.get("name") or "").strip()
                if mk not in HT_MARKETS: continue
                for v in bet.get("values", []):
                    val = (v.get("value") or "").strip()
                    if not any(val.startswith(x) for x in HT_VALUES): continue
                    try:
                        price = float(v.get("odd"))
                    except: continue
                    if (best is None) or (price > best):
                        best, book = price, bname
    return best, book

# ---------------- Picker (strict semplificato) ----------------
def fixtures_by_date_day(iso, leagues=None):
    params = {"date": iso}
    if leagues:
        params["league"] = leagues
    return _api("/fixtures", params)

def team_last_fixtures(team_id, from_date):
    """Ultime gare di questa squadra da 'from_date' (solo chiuse)."""
    resp = _api("/fixtures", {"team": team_id, "from": from_date.isoformat(), "to": date.today().isoformat()})
    # ordina recenti
    resp.sort(key=lambda x: x.get("fixture", {}).get("date", ""), reverse=True)
    # solo chiuse
    out = [x for x in resp if x.get("fixture", {}).get("status", {}).get("short") in {"FT","AET","PEN"}]
    return out[:LOOKBACK]

def count_ht_over05(fixtures):
    """Quante di queste gare hanno avuto ≥1 gol nel primo tempo (eventi)."""
    c, n = 0, 0
    for fx in fixtures:
        fid = fx.get("fixture", {}).get("id")
        try:
            events = _api("/fixtures/events", {"fixture": fid})
            n += 1
            # “Goal” nel 1° tempo
            has = False
            for ev in events:
                if ev.get("type") == "Goal":
                    t = ev.get("time", {}) or {}
                    m = (t.get("elapsed") or 0) + (t.get("extra") or 0)
                    if m <= 45:
                        has = True
                        break
            if has: c += 1
        except:
            pass
    return c, n

def estimate_p_from_history(team_id):
    """stima p(OV0.5 HT) della squadra dalle sue ultime gare (non dipende dall'avversario)."""
    since = date.today() - timedelta(days=PAST_WINDOW_D)
    hist = team_last_fixtures(team_id, since)
    if len(hist) < MIN_PLAYED:
        return None, 0
    hits, n = count_ht_over05(hist)
    p = hits / n if n else 0.0
    return p, n

def picker_build_whitelist():
    """Costruisce whitelist per OGGI. Ritorna set di fixture_id candidati."""
    leagues = LEAGUES if LEAGUES else None
    iso = date.today().isoformat()
    fx_today = fixtures_by_date_day(iso, leagues)

    wl = set()
    for it in fx_today:
        fid = it.get("fixture", {}).get("id")
        st  = it.get("fixture", {}).get("status", {}).get("short")
        if st in {"FT","AET","PEN"}:  # salta già chiuse
            continue
        home_id = it.get("teams", {}).get("home", {}).get("id")
        away_id = it.get("teams", {}).get("away", {}).get("id")
        if not home_id or not away_id:
            continue

        pH, nH = estimate_p_from_history(home_id)
        pA, nA = estimate_p_from_history(away_id)
        if pH is None or pA is None:
            continue
        # media semplice (puoi pesare home/away diversamente se vuoi)
        p = (pH + pA) / 2.0
        if p >= PTHRESH:
            wl.add(fid)

        if DEBUG:
            log(f"[PICK] f={fid} pH={pH if pH is not None else -1:.2f}({nH}) pA={pA if pA is not None else -1:.2f}({nA}) -> p={p:.2f} {'OK' if fid in wl else 'NO'}")

    return wl

def picker_loop():
    """Thread: aggiorna periodicamente la whitelist odierna."""
    while True:
        try:
            wl = picker_build_whitelist()
            with WL_LOCK:
                WHITELIST.clear()
                WHITELIST.update(wl)
                WL_META["last_build"] = datetime.now().isoformat(timespec="seconds")
                WL_META["count"] = len(WHITELIST)
                WL_META["log"].append({"ts": WL_META["last_build"], "count": len(WHITELIST)})
                WL_META["log"] = WL_META["log"][-50:]
            log(f"[PICKER] whitelist aggiornata: {len(wl)} partite")
        except Exception as e:
            log(f"[PICKER] errore: {e}")
            if DEBUG: traceback.print_exc()
        time.sleep(PICKER_EVERY_S)

# ---------------- Monitor live (usa SOLO whitelist) ----------------
def get_live_1H():
    resp = _api("/fixtures", {"live": "all"})
    out = []
    for it in resp:
        st = it.get("fixture", {}).get("status", {}) or {}
        if st.get("short") != "1H": continue
        elapsed = int(st.get("elapsed") or 0)
        if MINUTE_MIN <= elapsed <= MINUTE_MAX:
            out.append(it)
    return out

def monitor_loop():
    data = load_bankroll()
    STATE["bankroll"] = data.get("bankroll", BANKROLL_START)
    STATE["pending"] = data.get("pending", {})

    notified = set()

    log(f"[MONITOR] start THRESH={THRESHOLD} Kelly={KELLY_SCALE}")

    while True:
        try:
            lives = get_live_1H()
            with WL_LOCK:
                wl = set(WHITELIST)

            for it in lives:
                fid = int(it.get("fixture", {}).get("id"))
                if wl and (fid not in wl):
                    continue   # FUORI METODO

                st = it.get("fixture", {}).get("status", {}) or {}
                m  = int(st.get("elapsed") or 0)
                league = it.get("league", {}) or {}
                teams  = it.get("teams", {}) or {}
                home   = teams.get("home", {}).get("name", "Home")
                away   = teams.get("away", {}).get("name", "Away")
                lname  = league.get("name", "League")

                key = f"{fid}_{m}"
                if key in notified:
                    continue

                # stima p dalla media squadre usata dal picker, ricalcolo veloce
                pH, _ = estimate_p_from_history(teams.get("home", {}).get("id"))
                pA, _ = estimate_p_from_history(teams.get("away", {}).get("id"))
                if pH is None or pA is None:
                    continue
                p_est = (pH + pA) / 2.0

                odds = fetch_odds_any(fid)
                price, book = pick_ht_over05(odds)
                if price is None:
                    if DEBUG: log(f"[NO-ODDS] {fid} nessuna HT O0.5")
                    continue

                if DEBUG:
                    log(f"[ODDS] {fid} {home}-{away} {lname} m{m}  best={price:.2f}@{book} p~{p_est:.2f}")

                if price >= THRESHOLD:
                    bankroll = float(data.get("bankroll", BANKROLL_START))
                    stake = suggest_stake(bankroll, p_est, price)

                    lines = [
                        f"▶️ OV 0.5 HT @ {price:.2f} ({book})",
                        f"{home} — {away}",
                        f"{lname} | min {m}",
                        f"p≈{p_est:.2f}  bankroll={bankroll:.2f}",
                    ]
                    if stake > 0:
                        lines.append(f"💰 Stake (Kelly {KELLY_SCALE:g}x, cap {int(MAX_STAKE_PCT*100)}%): {stake:.2f}")
                        data["pending"][str(fid)] = {
                            "ts": int(time.time()), "stake": stake, "odds": price, "p": p_est,
                            "home": home, "away": away, "league": lname
                        }
                        data["bankroll"] = bankroll  # (bankroll si aggiorna quando registri un risultato)
                        save_bankroll(data)
                        STATE["bankroll"] = data["bankroll"]
                        STATE["pending"]  = data["pending"]
                    else:
                        lines.append("💰 Stake molto piccolo → non suggerito")

                    tg_send("\n".join(lines))
                    log(f"[ALERT] {fid} {home}-{away} @{price:.2f} ({book}) stake={stake:.2f}")
                    STATE["last_alerts"] = (STATE["last_alerts"] + [{
                        "ts": datetime.now().isoformat(timespec="seconds"),
                        "fixture_id": fid, "home": home, "away": away,
                        "league": lname, "minute": m, "price": price,
                        "p": round(p_est, 3), "stake": stake, "book": book
                    }])[-30:]
                    notified.add(key)

            time.sleep(POLL_SECONDS)

        except Exception as e:
            log(f"[MON] err: {e}")
            if DEBUG: traceback.print_exc()
            time.sleep(3)

# ---------------- FastAPI (web) ----------------
@app.get("/health")
def health():
    return {"ok": True, "time": datetime.utcnow().isoformat()+"Z"}

@app.get("/whitelist.json")
def whitelist_json():
    with WL_LOCK:
        wl = sorted(list(WHITELIST))
        meta = dict(WL_META)
    return JSONResponse({"meta": meta, "fixture_ids": wl})

@app.get("/status")
def status():
    with WL_LOCK:
        wl_count = len(WHITELIST)
        last_build = WL_META["last_build"]
    return JSONResponse({
        "bankroll": STATE["bankroll"],
        "pending": STATE["pending"],
        "last_alerts": STATE["last_alerts"],
        "wl_count": wl_count,
        "wl_last_build": last_build
    })

# ---------------- Bootstrap ----------------
def _boot():
    # carica bankroll su STATE
    data = load_bankroll()
    STATE["bankroll"] = data.get("bankroll", BANKROLL_START)
    STATE["pending"]  = data.get("pending", {})

    # thread picker + monitor
    threading.Thread(target=picker_loop, daemon=True).start()
    threading.Thread(target=monitor_loop, daemon=True).start()
    log("[BOOT] picker+monitor avviati")

_boot()
