#!/usr/bin/env python3
"""Collect UEFA Champions League history, xG and availability from FotMob.

football-data.co.uk does not publish the Champions League, so unlike the
big-5 feeds this collector writes all three products itself, from the same
FotMob source the other collectors use (league id 42):

  data/history/UCL-<SEASON>.csv      one row per played match, in the
                                     football-data.co.uk layout
                                     (Div,Date,Time,HomeTeam,AwayTeam,
                                     FTHG,FTAG) the app's history importer
                                     already parses. Date/Time are the
                                     kick-off in Europe/London, the zone
                                     that importer assumes.
  data/xg/UCL-<SEASON>.csv           per-match expected goals, same layout
                                     as the big-5 xG files.
  data/availability/UCL-<SEASON>.csv per-fixture missing-minutes shares,
                                     same layout and walk-forward discipline
                                     as collect_availability.py.

Scores are **regulation (90-minute) scores**, because that is what the
app's model trains on and settles. FotMob's league payload flags matches
decided after extra time or on penalties (`status.reason.short` = AET /
Pen); for those, the payload score is the post-extra-time score, so the
90-minute score is reconstructed from the match details' goal events
(every goal with time <= 90, shoot-out events excluded). A match whose
regulation score cannot be reconstructed is skipped and reported, never
guessed. Extra-time matches are also left out of the xG file: their
published xG covers 120 minutes and is not comparable to a 90-minute row.

Team names are FotMob's, verbatim: there is no football-data.co.uk naming
to align to for this competition, so FotMob naming is the canonical naming
of all three files and they join onto each other exactly.

Usage:
    python3 collect_ucl.py [--seasons 2122,2223,...]
                           [--history-dir data/history]
                           [--xg-dir data/xg]
                           [--availability-dir data/availability]
                           [--cache-dir .cache]

Match details are disk-cached, so a daily run only fetches details for
fixtures it has not seen. Each run rewrites the CSVs for every requested
season: availability for upcoming fixtures changes as players get injured
or return, while rows for played fixtures are stable.
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
from zoneinfo import ZoneInfo

import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-GB,en;q=0.9",
}

LEAGUE_ID = 42  # FotMob: Champions League (verified against /api/data/leagues)
CODE = "UCL"
LONDON = ZoneInfo("Europe/London")

# Assumed full-match length for deriving minutes from substitution events.
# Shares are relative within a team-season, so the constant mostly cancels.
MATCH_MINUTES = 95.0

# A team needs this many played matches before its minutes shares mean
# anything; earlier fixtures get empty shares rather than noise.
MIN_TEAM_MATCHES = 3


def _build_seasons():
    """Season code -> (FotMob season string, output label), 2021-22 onwards."""
    today = dt.date.today()
    current_start = today.year if today.month >= 7 else today.year - 1
    seasons = {}
    for start in range(2021, current_start + 1):
        code = f"{start % 100:02d}{(start + 1) % 100:02d}"
        seasons[code] = (f"{start}/{start + 1}", f"{start}-{(start + 1) % 100:02d}")
    return seasons


SEASONS = _build_seasons()


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


def fotmob_fixtures(session, fotmob_season: str):
    """All fixtures of the season, chronological.

    Each: (utc_time, home, away, home_id, away_id, match_id, finished,
    reason_short, home_score, away_score). Team names are FotMob's.
    """
    url = (f"https://www.fotmob.com/api/data/leagues"
           f"?id={LEAGUE_ID}&season={fotmob_season}")
    matches = find_match_list(get_json(session, url))
    out = []
    for m in matches:
        status = m.get("status") or {}
        out.append((status.get("utcTime", ""),
                    m["home"]["name"], m["away"]["name"],
                    m["home"].get("id"), m["away"].get("id"),
                    m["id"], bool(status.get("finished")),
                    (status.get("reason") or {}).get("short"),
                    m["home"].get("score"), m["away"].get("score")))
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


def regulation_score(details, reason, home_score, away_score):
    """The 90-minute score, or None when it cannot be established.

    For matches finished in regulation the payload score already is the
    90-minute score. For AET/Pen matches it is rebuilt from goal events:
    every goal event at minute <= 90 (stoppage time carries minute 90 with
    an overload; extra time starts at 91), shoot-out events excluded.
    """
    if reason in (None, "FT"):
        if home_score is None or away_score is None:
            return None
        return home_score, away_score
    if not details:
        return None
    events = (((details.get("content") or {}).get("matchFacts") or {})
              .get("events") or {}).get("events") or []
    goals = [e for e in events
             if e.get("type") == "Goal" and not e.get("isPenaltyShootoutEvent")]
    if not goals:
        return None
    home = sum(1 for e in goals
               if e.get("isHome") and (e.get("time") or 999) <= 90)
    away = sum(1 for e in goals
               if not e.get("isHome") and (e.get("time") or 999) <= 90)
    # The reconstruction can only ever remove extra-time goals.
    if home_score is not None and (home > home_score or away > away_score):
        return None
    return home, away


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
    """Per side: [unavailable player_id, ...], or None when no lineup section."""
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


def collect_season(session, season_code, cache_dir):
    """Returns (label, history_rows, xg_rows, availability_rows, problems)."""
    fotmob_season, label = SEASONS[season_code]
    fixtures = fotmob_fixtures(session, fotmob_season)
    with ThreadPoolExecutor(max_workers=8) as pool:
        details = list(pool.map(lambda f: match_details(f[5], cache_dir), fixtures))

    history_rows, xg_rows, availability_rows, problems = [], [], [], []
    minutes = {}   # team_id -> {player_id: minutes}
    played = {}    # team_id -> finished matches
    for (utc, home, away, hid, aid, mid, finished, reason, hg, ag), det in \
            zip(fixtures, details):
        date_utc = (utc or "")[:10]
        # Availability first: the row describes this fixture, computed only
        # from matches played before it (walk-forward, as in
        # collect_availability.py).
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
        availability_rows.append([date_utc, CODE, home, away, shares[0], shares[1]])

        if finished:
            score = regulation_score(det, reason, hg, ag)
            if score is None:
                problems.append(f"{label} {date_utc} {home} vs {away}: "
                                f"regulation score unavailable (reason={reason})")
            else:
                kickoff = dt.datetime.fromisoformat(
                    utc.replace("Z", "+00:00")).astimezone(LONDON)
                history_rows.append([
                    CODE, kickoff.strftime("%d/%m/%Y"), kickoff.strftime("%H:%M"),
                    home, away, score[0], score[1],
                ])
                if reason in (None, "FT"):
                    xg = extract_xg(det) if det else None
                    if xg is not None:
                        xg_rows.append([date_utc, CODE, home, away,
                                        f"{xg[0]:.2f}", f"{xg[1]:.2f}"])
            mins = minutes_facts(det)
            if mins is not None:
                for team_id, side in ((hid, 0), (aid, 1)):
                    team_minutes = minutes.setdefault(team_id, {})
                    for pid, m in mins[side].items():
                        team_minutes[pid] = team_minutes.get(pid, 0.0) + m
                    played[team_id] = played.get(team_id, 0) + 1
    return label, history_rows, xg_rows, availability_rows, problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", default=",".join(SEASONS))
    ap.add_argument("--history-dir", default="data/history")
    ap.add_argument("--xg-dir", default="data/xg")
    ap.add_argument("--availability-dir", default="data/availability")
    ap.add_argument("--cache-dir", default=".cache")
    args = ap.parse_args()

    session = requests.Session()
    session.headers.update(HEADERS)
    for d in (args.history_dir, args.xg_dir, args.availability_dir):
        os.makedirs(d, exist_ok=True)

    all_problems = []
    for season_code in args.seasons.split(","):
        label, history, xg, availability, problems = collect_season(
            session, season_code, args.cache_dir)
        all_problems += problems

        path = os.path.join(args.history_dir, f"{CODE}-{label}.csv")
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["Div", "Date", "Time", "HomeTeam", "AwayTeam", "FTHG", "FTAG"])
            w.writerows(sorted(history, key=lambda r: (
                dt.datetime.strptime(r[1], "%d/%m/%Y"), r[2], r[3])))

        path = os.path.join(args.xg_dir, f"{CODE}-{label}.csv")
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["date", "league", "home_team", "away_team", "home_xg", "away_xg"])
            w.writerows(sorted(xg, key=lambda r: (r[0], r[2])))

        path = os.path.join(args.availability_dir, f"{CODE}-{label}.csv")
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["date", "league", "home_team", "away_team",
                        "missing_home", "missing_away"])
            w.writerows(sorted(availability, key=lambda r: (r[0], r[2])))

        ready = sum(1 for r in availability if r[4] != "" and r[5] != "")
        print(f"{CODE} {label}: {len(history)} history, {len(xg)} xg, "
              f"{len(availability)} availability ({ready} with shares)",
              file=sys.stderr)

    for p in all_problems:
        print("  PROBLEM:", p, file=sys.stderr)
    return 0 if not all_problems else 1


if __name__ == "__main__":
    sys.exit(main())
