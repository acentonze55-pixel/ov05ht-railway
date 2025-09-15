# app_combo.py
import os, asyncio
from typing import Optional
import httpx
from fastapi import FastAPI, Body

app = FastAPI(title="API Football – Telegram Alerts")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
CHECK_INTERVAL_MIN = int(os.getenv("CHECK_INTERVAL_MIN", "30"))

@app.get("/")
async def home():
    return {"status": "ok", "msg": "API attiva su Railway", "docs": "/docs"}

@app.get("/health")
async def health():
    return {"ok": True}

@app.get("/__routes")
async def routes():
    # Per debug: elenca tutte le route registrate
    return [r.path for r in app.router.routes]

async def send_telegram(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(url, data=payload)
            return r.status_code == 200 and r.json().get("ok") is True
    except Exception:
        return False

@app.post("/notify-test")
async def notify_test(message: Optional[str] = Body(default="Test alert da Railway")):
    ok = await send_telegram(f"🔔 {message or 'Test alert'}")
    return {"sent": ok}

async def worker_loop():
    while True:
        try:
            await send_telegram("⏱️ Worker attivo. (Sostituisci con logica reale)")
        except Exception:
            pass
        await asyncio.sleep(CHECK_INTERVAL_MIN * 60)

@app.on_event("startup")
async def on_startup():
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        asyncio.create_task(worker_loop())
