# app_combo.py
import os, asyncio
from typing import Optional
import httpx
from fastapi import FastAPI, Body

app = FastAPI(title="OV0.5 HT – Test")

# --- HOME, per togliere il "Not Found" ---
@app.get("/")
async def home():
    return {"status":"ok","msg":"API attiva","docs":"/docs"}

@app.get("/health")
async def health():
    return {"ok": True}

# --- TEST TELEGRAM ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

async def send_telegram(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text})
    return r.status_code == 200 and r.json().get("ok") is True

@app.post("/notify-test")
async def notify_test(message: Optional[str] = Body("Ciao dal deploy!")):
    ok = await send_telegram(f"🔔 {message}")
    return {"sent": ok}
