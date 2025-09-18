# app_combo.py
import os
import threading
from typing import Optional, List
from pathlib import Path

import httpx
from fastapi import FastAPI, Body

# importa il tuo script (NON esegue nulla perché ha il guard __main__)
import live_ov05ht_auto_kelly_v2 as worker

app = FastAPI(title="OV0.5 HT – Live Alerts (Railway)")

# --- piccoli helper Telegram per un test manuale ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

async def send_telegram(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text})
    return (r.status_code == 200) and (r.json().get("ok") is True)

@app.get("/")
async def home():
    return {"status": "ok", "msg": "API attiva su Railway", "docs": "/docs"}

@app.get("/health")
async def health():
    return {"ok": True}

@app.get("/__routes")
async def routes():
    return [r.path for r in app.router.routes]

@app.post("/notify-test")
async def notify_test(message: str = Body("Test alert da Railway")):
    ok = await send_telegram(f"🔔 {message}")
    return {"sent": ok}

# ---- avvio del worker in background ----
_worker_thread = None

def _start_worker():
    global _worker_thread
    if _worker_thread and _worker_thread.is_alive():
        return  # già avviato

    # leggo ENV e costruisco gli argomenti per worker.run(...)
    api_key = os.getenv("API_FOOTBALL_KEY", "").strip()
    tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    tg_chat  = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not api_key or not tg_token or not tg_chat:
        print("⚠️ Mancano credenziali API/Telegram: worker NON avviato.")
        return

    def parse_list_int(s: str) -> Optional[List[int]]:
        s = (s or "").strip()
        return [int(x) for x in s.split(",") if x.strip().isdigit()] if s else None

    def parse_list_str(s: str) -> Optional[List[str]]:
        s = (s or "").strip()
        return [x.strip() for x in s.split(",") if x.strip()] if s else None

    thread_args = dict(
        api_key=api_key,
        tg_token=tg_token,
        tg_chat=tg_chat,
        tz_name=os.getenv("TZ", "Europe/Rome"),
        threshold=float(os.getenv("THRESHOLD", "1.50")),
        poll_seconds=float(os.getenv("POLL_SECONDS", "8")),
        max_ht_minute=int(os.getenv("MAX_HT_MINUTE", "50")),
        only_books=parse_list_str(os.getenv("ONLY_BOOKS", "")),
        only_leagues=parse_list_int(os.getenv("ONLY_LEAGUES", "")),
        kelly_mult=float(os.getenv("KELLY_MULT", "0.5")),
        min_played=int(os.getenv("MIN_PLAYED", "8")),
        pthresh=float(os.getenv("PTHRESH", "0.75")),
        start_bankroll=float(os.getenv("BANKROLL_START", "100.0")),
        state_file=Path(os.getenv("STATE_FILE", "output/bankroll_state.json")),
        bet_log_csv=Path(os.getenv("BET_LOG", "output/bet_log.csv")),
        stop_after_hit=os.getenv("STOP_AFTER_HIT", "").lower() in {"1","true","yes"},
    )

    def _target():
        worker.run(**thread_args)

    _worker_thread = threading.Thread(target=_target, name="ov05ht-worker", daemon=True)
    _worker_thread.start()
    print("✅ Worker OV0.5 HT avviato.")

@app.on_event("startup")
async def on_startup():
    _start_worker()
