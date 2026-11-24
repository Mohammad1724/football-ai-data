#!/usr/bin/env python3
"""Collect per-match team xG for the big-5 European leagues into CSVs.

The fixture list comes from FotMob's public web API (Opta-fed xG), which is
fresher than any CSV mirror. Where football-data.co.uk (the source
football-ai already uses for its match history) has the same fixture, the
final score is **cross-verified** against it before a row is accepted;
football-data lags a few days behind on the newest matchweek, so its newest
fixtures simply have no verification partner yet.

Output: data/xg/<LEAGUE>-<SEASON>.csv, e.g. data/xg/E0-2021-22.csv, with
columns: date, league, home_team, away_team, home_xg, away_xg
Team names use football-data.co.uk naming throughout.

Usage:
    python3 collect_xg.py [--seasons 2122,2223,...] [--leagues E0,SP1,...]
                          [--out-dir data/xg] [--cache-dir .cache]

Incremental: fixtures already present in an output CSV are skipped, so a
daily run only fetches details for newly played matches.
"""
import argparse
import csv
import datetime as dt
import io
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-GB,en;q=0.9",
}

# football-data league code -> FotMob league id
LEAGUES = {"E0": 47, "SP1": 87, "D1": 54, "I1": 55, "F1": 53}

def _build_seasons():
    """football-data season code -> (FotMob season string, output label).

    Covers 2021-22 onwards, including the in-progress season, so the daily
    update never needs a code change at a season rollover.
    """
    today = dt.date.today()
    current_start = today.year if today.month >= 7 else today.year - 1
    seasons = {}
    for start in range(2021, current_start + 1):
        code = f"{start % 100:02d}{(start + 1) % 100:02d}"
        seasons[code] = (f"{start}/{start + 1}", f"{start}-{(start + 1) % 100:02d}")
    return seasons


SEASONS = _build_seasons()

# FotMob team name -> football-data.co.uk team name (identity unless listed).
# Verified complete for all big-5 teams in seasons 2021-22..2026-27: team sets
# from both sources have identical sizes per league and pair up 1:1.
FM2FD = {
    # E0
    "Nottm Forest": "Nott'm Forest", "Sheff Utd": "Sheffield United",
    # SP1
    "Almería": "Almeria", "Athletic Club": "Ath Bilbao",
    "Atlético Madrid": "Ath Madrid", "Celta Vigo": "Celta", "Cádiz": "Cadiz",
    "Deportivo A Coruña": "La Coruna", "Deportivo Alavés": "Alaves",
    "Espanyol": "Espanol", "Leganés": "Leganes", "Málaga": "Malaga",
    "Racing Santander": "Santander", "Rayo Vallecano": "Vallecano",
    "Real Betis": "Betis", "Real Oviedo": "Oviedo",
    "Real Sociedad": "Sociedad", "Real Valladolid": "Valladolid",
    # D1
    "Arminia Bielefeld": "Bielefeld", "Bayern München": "Bayern Munich",
    "FC Heidenheim": "Heidenheim", "Frankfurt": "Ein Frankfurt",
    "Greuther Fürth": "Greuther Furth", "Hamburger SV": "Hamburg",
    "Hertha BSC": "Hertha", "Köln": "FC Koln", "St. Pauli": "St Pauli",
    "VfB Stuttgart": "Stuttgart",
    # I1
    "Hellas Verona": "Verona",
    # F1
    "AC Ajaccio": "Ajaccio", "Clermont Foot": "Clermont", "PSG": "Paris SG",
    "Saint-Étienne": "St Etienne",
}


def fd_name(fotmob_name: str) -> str:
    return FM2FD.get(fotmob_name, fotmob_name)


# Fixtures that will never have xG in FotMob, so the daily run must not fail
# on them (otherwise every run exits 1 and the availability/UCL steps and the
# commit never happen). (date, football-data home, football-data away).
# - Bournemouth-Luton 2023-12-16, Nice-Marseille 2021-08-22 and
#   Lyon-Marseille 2021-11-21 were all abandoned (never completed; the
#   replays are separate fixtures and are collected normally).
# - Mallorca-Girona 2024-03-03 finished 1-0 but FotMob's matchDetails carry
#   no expected_goals stat for it at all (verified 2026-11-24).
NO_XG_FIXTURES = {
    ("2023-12-16", "Bournemouth", "Luton"),
    ("2024-03-03", "Mallorca", "Girona"),
    ("2021-08-22", "Nice", "Marseille"),
    ("2021-11-21", "Lyon", "Marseille"),
}


def get_json(session: requests.Session, url: str, tries: int = 4):
    last = None
    for i in range(tries):
        try:
            r = session.get(url, timeout=30)
            if r.status_code == 200:
                return r.json()
            last = f"HTTP {r.status_code}"
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
        time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"GET {url} failed: {last}")


def find_match_list(payload):
    """FotMob league payloads nest the match list; find it by shape."""
    def find(o, depth=0):
        if depth > 4:
            return None
        if (isinstance(o, list) and o and isinstance(o[0], dict)
                and "home" in o[0] and "away" in o[0]):
            return o
        if isinstance(o, dict):
            for v in o.values():
                f = find(v, depth + 1)
                if f:
                    return f
        return None
    return find(payload) or []


def fotmob_fixtures(session, league_code: str, fotmob_season: str):
    """All finished matches: (iso_date, fd_home, fd_away, hg, ag, match_id)."""
    url = (f"https://www.fotmob.com/api/data/leagues"
           f"?id={LEAGUES[league_code]}&season={fotmob_season}")
    matches = find_match_list(get_json(session, url))
    out = []
    for m in matches:
        status = m.get("status") or {}
        if not status.get("finished"):
            continue
        out.append((status.get("utcTime", "")[:10],
                    fd_name(m["home"]["name"]), fd_name(m["away"]["name"]),
                    m["home"].get("score"), m["away"].get("score"), m["id"]))
    return out


def extract_xg(details: dict):
    """Walk the Periods.All stats sections for the expected_goals text stat."""
    stats = (((details.get("content") or {}).get("stats") or {})
             .get("Periods") or {}).get("All") or {}
    stats = stats.get("stats") or []

    def walk(items):
        for s in items:
            if s.get("key") == "expected_goals" and s.get("type") == "text":
                v = s.get("stats", [])
                if len(v) == 2 and v[0] is not None and v[1] is not None:
                    return float(v[0]), float(v[1])
            sub = s.get("stats")
            if (isinstance(sub, list) and sub and isinstance(sub[0], dict)
                    and "key" in sub[0]):
                r = walk(sub)
                if r:
                    return r
        return None

    return walk(stats)


_thread_local = threading.local()


def match_xg(match_id, cache_dir: str):
    """Fetch (and disk-cache) match details; return (home_xg, away_xg) or None."""
    path = os.path.join(cache_dir, f"{match_id}.json")
    details = None
    if os.path.exists(path):
        try:
            with open(path) as f:
                details = json.load(f)
        except Exception:  # noqa: BLE001
            details = None
    if details is None:
        if not hasattr(_thread_local, "session"):
            _thread_local.session = requests.Session()
            _thread_local.session.headers.update(HEADERS)
        details = get_json(
            _thread_local.session,
            f"https://www.fotmob.com/api/data/matchDetails?matchId={match_id}")
        os.makedirs(cache_dir, exist_ok=True)
        with open(path, "w") as f:
            json.dump(details, f)
    return extract_xg(details)


def fd_fixtures(session, league_code: str, season_code: str):
    """Played fixtures from football-data.co.uk: (date, home, away, hg, ag)."""
    url = f"https://www.football-data.co.uk/mmz4281/{season_code}/{league_code}.csv"
    r = session.get(url, timeout=30)
    r.raise_for_status()
    out = []
    for row in csv.DictReader(io.StringIO(r.text)):
        if not row.get("HomeTeam") or not row.get("FTHG"):
            continue
        raw = row["Date"].strip()
        date = None
        for fmt in ("%d/%m/%Y", "%d/%m/%y"):
            try:
                date = dt.datetime.strptime(raw, fmt).date()
                break
            except ValueError:
                continue
        if date is None:
            continue
        out.append((date.isoformat(), row["HomeTeam"].strip(),
                    row["AwayTeam"].strip(), int(float(row["FTHG"])),
                    int(float(row["FTAG"]))))
    return out


def load_existing(path):
    done = {}
    if os.path.exists(path):
        with open(path) as f:
            for row in csv.DictReader(f):
                done[(row["date"], row["home_team"], row["away_team"])] = row
    return done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", default=",".join(SEASONS))
    ap.add_argument("--leagues", default=",".join(LEAGUES))
    ap.add_argument("--out-dir", default="data/xg")
    ap.add_argument("--cache-dir", default=".cache")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    session = requests.Session()
    session.headers.update(HEADERS)
    os.makedirs(args.out_dir, exist_ok=True)

    total_new = total_skip = total_fail = 0
    problems = []
    for league in args.leagues.split(","):
        for code in args.seasons.split(","):
            fotmob_season, label = SEASONS[code]
            out_path = os.path.join(args.out_dir, f"{league}-{label}.csv")
            existing = load_existing(out_path)
            fixtures = fotmob_fixtures(session, league, fotmob_season)
            fd_by_key = {}
            for fdate, fhome, faway, fhg, fag in fd_fixtures(session, league, code):
                fd_by_key[(fhome, faway, fdate)] = (fhg, fag)
            todo = [fx for fx in fixtures
                    if (fx[0], fx[1], fx[2]) not in existing]
            no_xg = [fx for fx in todo
                     if (fx[0], fx[1], fx[2]) in NO_XG_FIXTURES]
            todo = [fx for fx in todo
                    if (fx[0], fx[1], fx[2]) not in NO_XG_FIXTURES]
            for fx in no_xg:
                print(f"  SKIP (no xG available): {league} {label} {fx[0]} "
                      f"{fx[1]} vs {fx[2]}", flush=True)
            if todo:

                def work(fx):
                    date, home, away, hg, ag, mid = fx
                    check = fd_by_key.get((home, away, date))
                    if check is None:
                        d0 = dt.date.fromisoformat(date)
                        for dd in (-1, 1):
                            check = fd_by_key.get(
                                (home, away, (d0 + dt.timedelta(days=dd)).isoformat()))
                            if check:
                                break
                    if check is not None and check != (hg, ag):
                        return fx, None, f"score mismatch fotmob {hg}-{ag} fd {check[0]}-{check[1]}"
                    try:
                        xg = match_xg(mid, args.cache_dir)
                    except Exception as e:  # noqa: BLE001
                        return fx, None, f"fetch error: {type(e).__name__}: {e}"
                    if xg is None:
                        return fx, None, "no xg in details"
                    return fx, xg, None

                with ThreadPoolExecutor(max_workers=args.workers) as ex:
                    for fx, xg, err in ex.map(work, todo):
                        if err:
                            total_fail += 1
                            problems.append(f"{league} {label} {fx[0]} {fx[1]} vs {fx[2]}: {err}")
                        else:
                            existing[(fx[0], fx[1], fx[2])] = {
                                "date": fx[0], "league": league,
                                "home_team": fx[1], "away_team": fx[2],
                                "home_xg": f"{xg[0]:.2f}", "away_xg": f"{xg[1]:.2f}",
                            }
                            total_new += 1
            total_skip += len(fixtures) - len(todo)
            rows = sorted(existing.values(),
                          key=lambda r: (r["date"], r["home_team"]))
            with open(out_path, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=["date", "league", "home_team",
                                                  "away_team", "home_xg", "away_xg"])
                w.writeheader()
                w.writerows(rows)
            print(f"{league} {label}: {len(rows)} rows "
                  f"({len(fixtures)} played fixtures)", flush=True)

    print(f"\nnew rows: {total_new}, already present: {total_skip}, failed: {total_fail}")
    for p in problems[:40]:
        print("  PROBLEM:", p)
    if len(problems) > 40:
        print(f"  ... and {len(problems) - 40} more")
    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
