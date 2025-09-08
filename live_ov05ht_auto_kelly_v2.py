# -*- coding: utf-8 -*-
"""
live_ov05ht_auto_kelly_v2.py
Monitor LIVE Over 0.5 HT con logica del picker integrata:
- Considera TUTTE le partite live ma filtra SOLO quelle che superano i gate del picker:
    • partite giocate minime per squadra (min_played)
    • probabilità stimata p_over05_ht >= pthresh (calcolata da team stats)
- Se la partita ammessa raggiunge in live quota >= threshold (es. 1.50) sul mercato
  "1st Half - Over/Under" (Over 0.5), invia ALERT Telegram con:
    lega, squadre, minuto, quota migliore, bookmaker, stake Kelly, bankroll.

- Settlement automatico a fine 1° tempo:
    WIN se c'è almeno un goal DOPO il minuto di ingresso e prima/fine HT;
    LOSS altrimenti. Aggiorna bankroll e logga su CSV.

Dipendenze: requests, python-dotenv, pandas
"""

import os
import json
import math
import time
import argparse
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

import requests
import pandas as pd
from dotenv import load_dotenv

API_HOST = "https://v3.football.api-sports.io"

# ---------------- Telegram ----------------
def tg_send(token: str, chat_id: str, text: str) -> None:
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=15,
        ).raise_for_status()
    except Exception:
        pass

# ---------------- API-Football helpers ----------------
def api_get(path: str, headers: Dict[str, str], params: Dict[str, Any]) -> Dict[str, Any]:
    r = requests.get(f"{API_HOST}{path}", headers=headers, params=params, timeout=15)
    r.raise_for_status()
    return r.json()

def fixtures_live(headers: Dict[str, str]) -> List[Dict[str, Any]]:
    data = api_get("/fixtures", headers, {"live": "all"})
    return data.get("response", [])

def odds_live(fixture_id: int, headers: Dict[str, str]) -> Dict[str, Any]:
    return api_get("/odds/live", headers, {"fixture": fixture_id})

def fixture_events(fixture_id: int, headers: Dict[str, str]) -> List[Dict[str, Any]]:
    data = api_get("/fixtures/events", headers, {"fixture": fixture_id})
    return data.get("response", [])

def team_statistics(league_id: int, season: int, team_id: int, headers: Dict[str, str]) -> Dict[str, Any]:
    params = {"league": league_id, "season": season, "team": team_id}
    data = api_get("/teams/statistics", headers, params)
    return data.get("response", {})  # contiene goals.for/against, fixtures.played, ecc.

# ---------------- Odds parsing ----------------
def market_ov05_ht(odds_resp: Dict[str, Any]) -> List[Tuple[str, float]]:
    """Ritorna [(book_name, odd)] per Over 0.5 1° tempo, migliore per book."""
    targets = {"1st Half - Over/Under", "Over/Under 1st Half"}
    out: List[Tuple[str, float]] = []
    for item in odds_resp.get("response", []):
        for bk in item.get("bookmakers", []):
            bname = bk.get("name", "UNKNOWN")
            best = None
            for bet in bk.get("bets", []):
                if bet.get("name", "") not in targets:
                    continue
                for v in bet.get("values", []):
                    if str(v.get("value", "")).startswith("Over 0.5"):
                        try:
                            odd = float(v.get("odd"))
                            if best is None or odd > best:
                                best = odd
                        except Exception:
                            pass
            if best is not None:
                out.append((bname, best))
    return out

# ---------------- Utils ----------------
def status_elapsed(fx: Dict[str, Any]) -> Tuple[str, Optional[int]]:
    st = fx.get("fixture", {}).get("status", {}).get("short", "")
    el = fx.get("fixture", {}).get("status", {}).get("elapsed", None)
    try:
        el = int(el) if el is not None else None
    except Exception:
        el = None
    return st, el

def fmt_local(dt_iso: str, tz_name: str) -> str:
    try:
        ts = pd.to_datetime(dt_iso, utc=True, errors="coerce")
        if ts is pd.NaT:  # type: ignore
            return dt_iso or ""
        return ts.tz_convert(tz_name).strftime("%d/%m/%Y %H:%M")
    except Exception:
        return dt_iso or ""

def fmt_match(fx: Dict[str, Any]) -> Tuple[str, str, str]:
    lg = fx.get("league", {}).get("name", "") or ""
    h = fx.get("teams", {}).get("home", {}).get("name", "") or ""
    a = fx.get("teams", {}).get("away", {}).get("name", "") or ""
    return lg, h, a

def goal_minutes_first_half(events: List[Dict[str, Any]]) -> List[int]:
    mins: List[int] = []
    for ev in events:
        if ev.get("type") == "Goal":
            t = ev.get("time", {}) or {}
            m = t.get("elapsed", None)
            et = t.get("extra", None)
            if m is None:
                continue
            try:
                mm = int(m) + (int(et) if et is not None else 0)
            except Exception:
                continue
            if mm <= 45:
                mins.append(mm)
    return sorted(mins)

# ---------------- Picker-like probability ----------------
def p_over05_ht_from_stats(stats_home: Dict[str, Any], stats_away: Dict[str, Any]) -> Tuple[Optional[float], int, int]:
    """
    Stima p(>=1 gol nel 1° tempo) con una logica semplice e robusta:
      - ricava goals.first_half_for/against e partite giocate per ciascuna squadra
      - stima lambda_ht = (gH_for+gH_against)/played_H + (gA_for+gA_against)/played_A
      - p = 1 - exp(-lambda_ht)  (Poisson 0+)
    Ritorna: (p, played_home, played_away)
    """
    try:
        played_h = int(stats_home.get("fixtures", {}).get("played", {}).get("total", 0) or 0)
        played_a = int(stats_away.get("fixtures", {}).get("played", {}).get("total", 0) or 0)

        gfh_h = int(stats_home.get("goals", {}).get("for", {}).get("total", {}).get("first", 0) or 0)
        gah_h = int(stats_home.get("goals", {}).get("against", {}).get("total", {}).get("first", 0) or 0)

        gfh_a = int(stats_away.get("goals", {}).get("for", {}).get("total", {}).get("first", 0) or 0)
        gah_a = int(stats_away.get("goals", {}).get("against", {}).get("total", {}).get("first", 0) or 0)

        lam_h = (gfh_h + gah_h) / played_h if played_h > 0 else 0.0
        lam_a = (gfh_a + gah_a) / played_a if played_a > 0 else 0.0
        lam = max(0.0, lam_h + lam_a)

        p = 1.0 - math.exp(-lam) if lam > 0 else 0.0
        return p, played_h, played_a
    except Exception:
        return None, 0, 0

# ---------------- Kelly & stato ----------------
def kelly_fraction(p: float, odds: float, mult: float) -> float:
    b = max(0.0, odds - 1.0)
    q = 1.0 - p
    f = (b * p - q) / b if b > 0 else 0.0
    f = max(0.0, f)
    return f * max(0.0, mult)

def load_state(path: Path, start_bankroll: float) -> Dict[str, Any]:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"bankroll": float(start_bankroll), "open_bets": {}}

def save_state(path: Path, state: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

# ---------------- Core ----------------
def run(
    api_key: str,
    tg_token: str,
    tg_chat: str,
    tz_name: str,
    threshold: float,
    poll_seconds: float,
    max_ht_minute: int,
    only_books: Optional[List[str]],
    only_leagues: Optional[List[int]],
    kelly_mult: float,
    min_played: int,
    pthresh: float,
    start_bankroll: float,
    state_file: Path,
    bet_log_csv: Path,
    stop_after_hit: bool,
):
    headers = {"x-apisports-key": api_key}

    # stato bankroll/bets
    state = load_state(state_file, start_bankroll)
    bankroll = float(state.get("bankroll", start_bankroll))
    open_bets: Dict[str, Any] = state.get("open_bets", {})

    # cache stats team -> p_over05
    # key: (league_id, season, home_id, away_id) -> dict { 'p': float, 'ok': bool }
    p_cache: Dict[Tuple[int, int, int, int], Dict[str, Any]] = {}
    team_stats_cache: Dict[Tuple[int, int, int], Dict[str, Any]] = {}  # (league, season, team) -> stats

    bet_log_csv.parent.mkdir(parents=True, exist_ok=True)
    if not bet_log_csv.exists():
        bet_log_csv.write_text(
            "ts,fid,league,match,placed_minute,odds,book,stake,result,bankroll_after\n",
            encoding="utf-8",
        )

    tg_send(tg_token, tg_chat, f"Monitor LIVE OV0.5 HT (picker-logic) avviato — soglia {threshold:.2f}, pthresh {pthresh:.2f}, min_played {min_played}, Kelly x{kelly_mult}, bankroll {bankroll:.2f}")

    try:
        while True:
            # 1) live fixtures
            try:
                fx_list = fixtures_live(headers)
            except requests.HTTPError as e:
                code = e.response.status_code if e.response is not None else None
                if code == 429:
                    time.sleep(2.0); continue
                time.sleep(0.5); continue
            except requests.RequestException:
                time.sleep(0.5); continue

            # 2) filtra per 1° tempo e per leghe (opzionale)
            fx_1h: List[Dict[str, Any]] = []
            for fx in fx_list:
                st, el = status_elapsed(fx)
                if st not in {"1H", "LIVE", "ET"}:
                    continue
                if el is None or el <= 0:
                    continue
                if el >= max_ht_minute:
                    continue
                if only_leagues and int(fx.get("league", {}).get("id", 0)) not in only_leagues:
                    continue
                fx_1h.append(fx)

            # 3) per ciascun match, applica GATE picker e poi controlla odds live
            for fx in fx_1h:
                fid = int(fx.get("fixture", {}).get("id"))
                lg_id = int(fx.get("league", {}).get("id", 0) or 0)
                season = int(fx.get("league", {}).get("season", 0) or 0)
                home_id = int(fx.get("teams", {}).get("home", {}).get("id", 0) or 0)
                away_id = int(fx.get("teams", {}).get("away", {}).get("id", 0) or 0)
                st, el = status_elapsed(fx)
                key_match = (lg_id, season, home_id, away_id)
                key_bet = str(fid)

                if stop_after_hit and key_bet in open_bets:
                    continue

                # ---- calcolo/lookup p_over05_ht con logica picker ----
                if key_match not in p_cache:
                    # prendi team stats (cache per ridurre chiamate)
                    def get_team_stats_cached(team_id: int) -> Dict[str, Any]:
                        tkey = (lg_id, season, team_id)
                        if tkey not in team_stats_cache:
                            team_stats_cache[tkey] = team_statistics(lg_id, season, team_id, headers)
                            time.sleep(0.05)
                        return team_stats_cache[tkey]

                    try:
                        stats_h = get_team_stats_cached(home_id)
                        stats_a = get_team_stats_cached(away_id)
                    except requests.HTTPError as e:
                        code = e.response.status_code if e.response is not None else None
                        if code == 429:
                            time.sleep(2.0)
                            continue
                        time.sleep(0.3)
                        continue
                    except requests.RequestException:
                        time.sleep(0.3)
                        continue

                    p_est, played_h, played_a = p_over05_ht_from_stats(stats_h, stats_a)

                    ok = True
                    if played_h < min_played or played_a < min_played:
                        ok = False
                    if p_est is None or p_est < pthresh:
                        ok = False

                    p_cache[key_match] = {"p": p_est, "ok": ok, "played_h": played_h, "played_a": played_a}

                pc = p_cache[key_match]
                if not pc.get("ok", False):
                    continue  # non supera i gate del picker

                # ---- odds live per Over 0.5 HT ----
                try:
                    ol = odds_live(fid, headers)
                except requests.HTTPError as e:
                    code = e.response.status_code if e.response is not None else None
                    if code == 429:
                        time.sleep(2.0); continue
                    time.sleep(0.3); continue
                except requests.RequestException:
                    time.sleep(0.3); continue

                books = market_ov05_ht(ol)
                if not books:
                    continue
                if only_books:
                    books = [(b, q) for (b, q) in books if b in only_books]
                    if not books:
                        continue

                books_sorted = sorted(books, key=lambda x: x[1], reverse=True)
                best_book, best_odd = books_sorted[0]
                if best_odd < threshold:
                    continue

                # ---- stake Kelly, registra bet se non già aperta ----
                if key_bet in open_bets:
                    continue

                p_used = float(pc.get("p") or 0.0)
                frac = kelly_fraction(p_used, best_odd, kelly_mult)
                if frac <= 0:
                    continue
                stake = round(bankroll * frac, 2)
                if stake <= 0:
                    continue

                lg_name, home_name, away_name = fmt_match(fx)
                when_local = fmt_local(fx.get("fixture", {}).get("date", ""), tz_name)

                open_bets[key_bet] = {
                    "fixture_id": fid,
                    "league": lg_name,
                    "match": f"{home_name} vs {away_name}",
                    "placed_minute": int(el or 0),
                    "odds": float(best_odd),
                    "book": best_book,
                    "stake": float(stake),
                    "p_used": float(p_used),
                    "ts": pd.Timestamp.utcnow().isoformat(),
                }
                state["open_bets"] = open_bets
                save_state(state_file, state)

                top_lines = []
                for b, q in books_sorted[:5]:
                    top_lines.append(f"- {b}: {q:.2f}")
                top_txt = "\n".join(top_lines)

                msg = (
                    "ALERT OV 0.5 HT (picker)\n"
                    f"{when_local} — {lg_name}\n"
                    f"{home_name} vs {away_name}\n"
                    f"Minuto: {el}\n"
                    f"Quota: {best_odd:.2f} @ {best_book}\n"
                    f"Prob. stimata: {p_used*100:.0f}%  (played H/A: {pc.get('played_h',0)}/{pc.get('played_a',0)})\n"
                    f"Stake (Kelly x{kelly_mult:.2f}): {stake:.2f}  —  Bankroll: {bankroll:.2f}\n"
                    "\nBook (top):\n" + top_txt
                )
                tg_send(tg_token, tg_chat, msg)

                time.sleep(0.05)

            # 4) settlement giocate aperte
            to_close: List[str] = []
            for key, bet in list(open_bets.items()):
                fid = int(bet["fixture_id"])
                placed_min = int(bet.get("placed_minute", 0))
                try:
                    fx_data = api_get("/fixtures", headers, {"id": fid})
                    fx = (fx_data.get("response") or [None])[0] or {}
                    st, el = status_elapsed(fx)
                    ev = fixture_events(fid, headers)
                    mins = goal_minutes_first_half(ev)
                    win = any(m >= placed_min for m in mins)
                    reached_ht = (st in {"HT","2H","FT","AET","PEN"}) or (el is not None and el >= 46)

                    if win or reached_ht:
                        stake = float(bet["stake"])
                        odds = float(bet["odds"])
                        lg = bet["league"]; match = bet["match"]
                        result = "WIN" if win else "LOSS"
                        pnl = stake * (odds - 1.0) if win else -stake
                        bankroll = round(bankroll + pnl, 2)
                        state["bankroll"] = bankroll

                        bet_log_csv.parent.mkdir(parents=True, exist_ok=True)
                        with bet_log_csv.open("a", encoding="utf-8") as f:
                            f.write(f"{pd.Timestamp.utcnow().isoformat()},{fid},{lg},{match},{placed_min},{odds:.2f},{bet['book']},{stake:.2f},{result},{bankroll:.2f}\n")

                        tg_send(
                            tg_token, tg_chat,
                            f"SETTLED OV 0.5 HT — {result}\n{lg}\n{match}\n"
                            f"Stake {stake:.2f} @ {odds:.2f} — ingresso {placed_min}'\n"
                            f"Nuovo bankroll: {bankroll:.2f}"
                        )
                        to_close.append(key)
                        time.sleep(0.05)

                except requests.HTTPError as e:
                    code = e.response.status_code if e.response is not None else None
                    if code == 429:
                        time.sleep(2.0)
                    else:
                        time.sleep(0.3)
                except requests.RequestException:
                    time.sleep(0.3)
                except Exception:
                    time.sleep(0.2)

            for key in to_close:
                open_bets.pop(key, None)
            if to_close:
                state["open_bets"] = open_bets
                save_state(state_file, state)

            time.sleep(poll_seconds)

    except KeyboardInterrupt:
        save_state(state_file, {"bankroll": bankroll, "open_bets": open_bets})
        tg_send(tg_token, tg_chat, "Monitor interrotto manualmente.")

# ---------------- CLI ----------------
def main():
    load_dotenv()

    ap = argparse.ArgumentParser(description="LIVE OV 0.5 HT con logica picker integrata + Kelly")
    ap.add_argument("--threshold", type=float, default=float(os.getenv("THRESHOLD", "1.50")))
    ap.add_argument("--poll", type=float, default=float(os.getenv("POLL_SECONDS", "8")))
    ap.add_argument("--max-ht", type=int, default=int(os.getenv("MAX_HT_MINUTE", "50")))
    ap.add_argument("--tz", default=os.getenv("TZ", "Europe/Rome"))
    ap.add_argument("--kelly-mult", type=float, default=float(os.getenv("KELLY_MULT", "0.5")))
    ap.add_argument("--min-played", type=int, default=int(os.getenv("MIN_PLAYED", "8")))
    ap.add_argument("--pthresh", type=float, default=float(os.getenv("PTHRESH", "0.75")))
    ap.add_argument("--bankroll-start", type=float, default=float(os.getenv("BANKROLL_START", "100.0")))
    ap.add_argument("--state-file", default=os.getenv("STATE_FILE", "output/bankroll_state.json"))
    ap.add_argument("--bet-log", default=os.getenv("BET_LOG", "output/bet_log.csv"))
    ap.add_argument("--only-books", type=str, default=os.getenv("ONLY_BOOKS", "").strip(), help="Book ammessi (lista separata da virgola)")
    ap.add_argument("--only-leagues", type=str, default=os.getenv("ONLY_LEAGUES", "").strip(), help="ID leghe ammessi (es. 39,135,140)")
    ap.add_argument("--stop-after-hit", action="store_true", help="Una sola bet per match")
    args = ap.parse_args()

    api_key = os.getenv("API_FOOTBALL_KEY", "").strip()
    tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    tg_chat  = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not api_key or not tg_token or not tg_chat:
        raise SystemExit("Manca API_FOOTBALL_KEY e/o TELEGRAM credenziali nel .env")

    only_books = [x.strip() for x in args.only_books.split(",") if x.strip()] if args.only_books else None
    only_leagues = [int(x) for x in args.only_leagues.split(",") if x.strip().isdigit()] if args.only_leagues else None

    run(
        api_key=api_key,
        tg_token=tg_token,
        tg_chat=tg_chat,
        tz_name=args.tz,
        threshold=args.threshold,
        poll_seconds=args.poll,
        max_ht_minute=args.max_ht,
        only_books=only_books,
        only_leagues=only_leagues,
        kelly_mult=args.kelly_mult,
        min_played=args.min_played,
        pthresh=args.pthresh,
        start_bankroll=args.bankroll_start,
        state_file=Path(args.state_file),
        bet_log_csv=Path(args.bet_log),
        stop_after_hit=args.stop_after_hit,
    )

if __name__ == "__main__":
    main()
