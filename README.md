# ov05HT-railway

Script per monitorare in tempo reale le partite candidate all'Over 0.5 HT,
con segnalazioni su Telegram e gestione dello stake tramite criterio di Kelly.

## File inclusi
- `live_ov05ht_auto_kelly_v2.py` → Script principale
- `requirements.txt` → Librerie Python necessarie
- `Procfile.txt` → Avvio automatico su Railway

## Deploy
Questo progetto è pensato per girare su [Railway](https://railway.app/) con un container Python 3.10.
Le chiavi API (API_FOOTBALL_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID) vanno inserite come variabili di ambiente.
