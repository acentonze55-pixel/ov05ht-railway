# app_combo.py
import os
import asyncio
from typing import Optional

import httpx
from fastapi import FastAPI, Body

app = FastAPI(title="API Football – Telegram Alerts")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Configura l'intervallo tra un controllo e l'altro (minuti)
CHECK_INTERVAL_MIN = int(os.getenv("CHECK_INTERVAL_MIN", "30"))

# ---------- Utility Telegram ----------
async def send_telegram(text: str) -> bool:
    """
    Invia un messaggio testuale su Telegram.
    Ritorna True/False in base all'esito.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram non configurato: variabili mancanti.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(url, data=payload)
            ok = r.status_code == 200 and r.json().get("ok") is True
            if not ok:
                print("Errore Telegram:", r.status_code, r.text)
            return ok
    except Exception as e:
        print("Eccezione Telegram:", e)
        return False

# ---------- Rotte base ----------
@app.get("/")
async def home():
    return {"status": "ok", "msg": "API attiva su Railway", "docs": "/docs"}

@app.get("/health")
async def health():
    return {"ok": True}

# ---------- Rotta di test per invio manuale ----------
@app.post("/notify-test")
async def notify_test(message: Optional[str] = Body(default="Test alert da Railway")):
    """
    POST /notify-test con body JSON {"message":"qualcosa"} invia un messaggio su Telegram.
    """
    text = message or "Test alert da Railway"
    ok = await send_telegram(f"🔔 {text}")
    return {"sent": ok}

# ---------- Worker in background ----------
async def worker_loop():
    """
    Qui metti la tua logica che interroga API-Football e decide quando inviare alert.
    Per esempio:
      - scarica le partite del giorno
      - applica i tuoi filtri
      - se ci sono condizioni -> send_telegram(...)
    Al momento invia solo un 'ping' ogni intervallo per mostrare che funziona.
    """
    while True:
        try:
            # TODO: sostituisci con la tua logica reale (API-Football + filtri + messaggio)
            await send_telegram("⏱️ Worker attivo. (Sostituisci con la logica API-Football)")
        except Exception as e:
            print("Errore nel worker:", e)
        await asyncio.sleep(CHECK_INTERVAL_MIN * 60)

@app.on_event("startup")
async def on_startup():
    # Avvia il worker solo se le variabili Telegram sono presenti
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        asyncio.create_task(worker_loop())
    else:
        print("Worker NON avviato: manca configurazione Telegram.")
