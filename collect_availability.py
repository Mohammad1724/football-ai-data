#!/usr/bin/env python3
"""Collect per-fixture player availability for the big-5 European leagues.

For every fixture (played or upcoming) the CSV row carries each team's
"missing share": the share of the team's season minutes — computed from the
lineups of its *earlier* matches that season only — belonging to players
FotMob lists as unavailable for the fixture (injured, suspended, or
otherwise out). The walk-forward discipline is the point: a row for a match
uses only information published before that match, so the file can feed both
the app's forecasts and honest backtests of them.

Output: data/availability/<LEAGUE>-<SEASON>.csv, e.g.
data/availability/E0-2021-22.csv, with columns:
date, league, home_team, away_team, missing_home, missing_away
Team names use football-data.co.uk naming throughout. Shares are empty when
they cannot be computed (a team's first three matches of a season, or no
lineup published yet) — empty means "no data", never zero.

Usage:
    python3 collect_availability.py [--seasons 2122,2223,...]
                                    [--leagues E0,SP1,...]
                                    [--out-dir data/availability]
                                    [--cache-dir .cache]

Each run rewrites the CSV for every requested league-season: availability
for upcoming fixtures changes as players get injured or return, while rows
for played fixtures are stable. Match details are disk-cached, so a daily
run only fetches details for fixtures it has not seen.
"""
import argparse
import csv
import datetime as dt
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

# Assumed full-match length for deriving minutes from substitution events.
# Shares are relative within a team-season, so the constant mostly cancels.
MATCH_MINUTES = 95.0

# A team needs this many played matches before its minutes shares mean
# anything; earlier fixtures get empty shares rather than noise.
MIN_TEAM_MATCHES = 3


def _build_seasons():
    """football-data season code -> (FotMob season string, output label)."""
    today = dt.date.today()
    current_start = today.year if today.month >= 7 else today.year - 1
    seasons = {}
    for start in range(2021, current_start + 1):
        code = f"{start % 100:02d}{(start + 1) % 100:02d}"
        seasons[code] = (f"{start}/{start + 1}", f"{start}-{(start + 1) % 100:02d}")
    return seasons


SEASONS = _build_seasons()

# FotMob team name -> football-data.co.uk team name (identity unless listed).
# Same verified mapping as collect_xg.py.
FM2FD = {
    "Nottm Forest": "Nott'm Forest", "Sheff Utd": "Sheffield United",
    "Almería": "Almeria", "Athletic Club": "Ath Bilbao",
    "Atlético Madrid": "Ath Madrid", "Celta Vigo": "Celta", "Cádiz": "Cadiz",
    "Deportivo A Coruña": "La Coruna", "Deportivo Alavés": "Alaves",
    "Espanyol": "Espanol", "Leganés": "Leganes", "Málaga": "Malaga",
    "Racing Santander": "Santander", "Rayo Vallecano": "Vallecano",
    "Real Betis": "Betis", "Real Oviedo": "Oviedo",
    "Real Sociedad": "Sociedad", "Real Valladolid": "Valladolid",
    "Arminia Bielefeld": "Bielefeld", "Bayern München": "Bayern Munich",
    "FC Heidenheim": "Heidenheim", "Frankfurt": "Ein Frankfurt",
    "Greuther Fürth": "Greuther Furth", "Hamburger SV": "Hamburg",
    "Hertha BSC": "Hertha", "Köln": "FC Koln", "St. Pauli": "St Pauli",
    "VfB Stuttgart": "Stuttgart",
    "Hellas Verona": "Verona",
    "AC Ajaccio": "Ajaccio", "Clermont Foot": "Clermont", "PSG": "Paris SG",
    "Saint-Étienne": "St Etienne",
}


def fd_name(fotmob_name: str) -> str:
    return FM2FD.get(fotmob_name, fotmob_name)


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
    """All fixtures: (iso_date, fd_home, fd_away, home_id, away_id, match_id, finished)."""
    url = (f"https://www.fotmob.com/api/data/leagues"
           f"?id={LEAGUES[league_code]}&season={fotmob_season}")
    matches = find_match_list(get_json(session, url))
    out = []
    for m in matches:
        status = m.get("status") or {}
        out.append((status.get("utcTime", "")[:10],
                    fd_name(m["home"]["name"]), fd_name(m["away"]["name"]),
                    m["home"].get("id"), m["away"].get("id"),
                    m["id"], bool(status.get("finished"))))
    out.sort(key=lambda r: (r[0], r[1]))
    return out


_thread_local = threading.local()


def match_details(match_id, cache_dir: str):
    """Fetch (and disk-cache) match details; None when unavailable."""
    path = os.path.join(cache_dir, f"{match_id}.json")
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            pass
    if not hasattr(_thread_local, "session"):
        _thread_local.session = requests.Session()
        _thread_local.session.headers.update(HEADERS)
    try:
        details = get_json(
            _thread_local.session,
            f"https://www.fotmob.com/api/data/matchDetails?matchId={match_id}")
    except RuntimeError:
        return None
    os.makedirs(cache_dir, exist_ok=True)
    with open(path, "w") as f:
        json.dump(details, f)
    return details


def player_minutes(player, is_starter):
    perf = player.get("performance") or {}
    sub_in = sub_out = None
    for e in perf.get("substitutionEvents") or []:
        t = e.get("time")
        if t is None:
            continue
        if e.get("type") == "subIn" and sub_in is None:
            sub_in = float(t)
        elif e.get("type") == "subOut" and sub_out is None:
            sub_out = float(t)
    if is_starter:
        return max(0.0, sub_out if sub_out is not None else MATCH_MINUTES)
    if sub_in is None:
        return 0.0
    return max(0.0, (sub_out if sub_out is not None else MATCH_MINUTES) - sub_in)


def unavailable_facts(details):
    """Per side: [unavailable player_id, ...], or None when no lineup section.

    FotMob publishes the unavailable list for upcoming fixtures well before
    lineups exist, so this deliberately does not require starters.
    """
    if not details:
        return None
    lu = ((details.get("content") or {}).get("lineup")) or {}
    if not lu.get("homeTeam") and not lu.get("awayTeam"):
        return None
    return [[p["id"] for p in (lu.get(side) or {}).get("unavailable") or []
             if p.get("id") is not None]
            for side in ("homeTeam", "awayTeam")]


def minutes_facts(details):
    """Per side: {player_id: minutes}, or None when no lineup was published."""
    if not details:
        return None
    lu = ((details.get("content") or {}).get("lineup")) or {}
    sides = []
    for side in ("homeTeam", "awayTeam"):
        t = lu.get(side) or {}
        if not t.get("starters"):
            return None
        mins = {}
        for p in t["starters"]:
            if p.get("id") is not None:
                mins[p["id"]] = player_minutes(p, True)
        for p in t.get("subs") or []:
            m = player_minutes(p, False)
            if m > 0 and p.get("id") is not None:
                mins[p["id"]] = m
        sides.append(mins)
    return sides


def collect_league_season(session, league_code, season_code, cache_dir):
    fotmob_season, label = SEASONS[season_code]
    fixtures = fotmob_fixtures(session, league_code, fotmob_season)
    with ThreadPoolExecutor(max_workers=8) as pool:
        details = list(pool.map(lambda f: match_details(f[5], cache_dir), fixtures))
    minutes = {}   # team_id -> {player_id: minutes}
    played = {}    # team_id -> finished matches
    rows = []
    for (date, home, away, hid, aid, _mid, finished), det in zip(fixtures, details):
        unav = unavailable_facts(det)
        shares = []
        for team_id, side in ((hid, 0), (aid, 1)):
            team_minutes = minutes.get(team_id) or {}
            total = sum(team_minutes.values())
            if unav is None or played.get(team_id, 0) < MIN_TEAM_MATCHES or total <= 0:
                shares.append("")
            else:
                missing = sum(team_minutes.get(pid, 0.0) / total for pid in unav[side])
                shares.append(f"{missing:.4f}")
        rows.append([date, league_code, home, away, shares[0], shares[1]])
        if finished:
            mins = minutes_facts(det)
            if mins is not None:
                for team_id, side in ((hid, 0), (aid, 1)):
                    team_minutes = minutes.setdefault(team_id, {})
                    for pid, m in mins[side].items():
                        team_minutes[pid] = team_minutes.get(pid, 0.0) + m
                    played[team_id] = played.get(team_id, 0) + 1
    return label, rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", default=",".join(SEASONS))
    ap.add_argument("--leagues", default=",".join(LEAGUES))
    ap.add_argument("--out-dir", default="data/availability")
    ap.add_argument("--cache-dir", default=".cache")
    args = ap.parse_args()

    session = requests.Session()
    session.headers.update(HEADERS)
    os.makedirs(args.out_dir, exist_ok=True)
    for league_code in args.leagues.split(","):
        for season_code in args.seasons.split(","):
            label, rows = collect_league_season(session, league_code, season_code, args.cache_dir)
            path = os.path.join(args.out_dir, f"{league_code}-{label}.csv")
            with open(path, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["date", "league", "home_team", "away_team", "missing_home", "missing_away"])
                w.writerows(rows)
            ready = sum(1 for r in rows if r[4] != "" and r[5] != "")
            print(f"{league_code} {label}: {len(rows)} fixtures, {ready} with shares -> {path}",
                  file=sys.stderr)


if __name__ == "__main__":
    main()
