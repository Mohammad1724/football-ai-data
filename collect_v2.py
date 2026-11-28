#!/usr/bin/env python3
"""Collect the v2 app bundles (see DATA_CONTRACT.md) from FotMob.

For each of the seven day-one leagues this writes, from the same FotMob
source the v1 collectors use:

  data/v2/<slug>/bundle.json   fixtures/results for the app window
                               (-3 days .. +7 days), the current table, and
                               per-match details (lineups with formations,
                               unavailable players with names, events, xG)
                               for window matches that have them published.
  data/v2/<slug>/history.json  every played match of the current season
                               (regulation scores; xG where the details
                               cache already holds the match).

Iran (FotMob 523) is first-class but reduced: FotMob publishes no xG and
no unavailable lists for it, and no formation string or bench — formations
are derived from the starters' position lines and flagged as such. The
per-league `features` block in each bundle states this explicitly so the
app never has to guess (verified 2026-11-28; see DATA_CONTRACT.md).

Usage:
    python3 collect_v2.py [--leagues iran,premier-league,...]
                          [--out-dir data/v2] [--cache-dir .cache]
                          [--workers 8]

Match details are disk-cached, so a daily run only fetches details for
window matches it has not seen (played matches) — upcoming matches are
always re-fetched, because their lineups/unavailable lists change.
"""
import argparse
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

# slug -> (FotMob league id, display name, country)
LEAGUES = {
    "premier-league": (47, "Premier League", "ENG"),
    "la-liga": (87, "La Liga", "ESP"),
    "serie-a": (55, "Serie A", "ITA"),
    "bundesliga": (54, "Bundesliga", "GER"),
    "ligue-1": (53, "Ligue 1", "FRA"),
    "champions-league": (42, "Champions League", "EUR"),
    "iran": (523, "Persian Gulf", "IRN"),
}

# League-level FotMob capabilities, verified against live responses on
# 2026-11-28 (DATA_CONTRACT.md, "Iran coverage verdict"). Everything else
# publishes xG, unavailable lists and formation strings; Iran does not.
FEATURES = {
    slug: {"xg": True, "availability": True, "lineups": True,
           "formationsPublished": True}
    for slug in LEAGUES
}
FEATURES["iran"] = {"xg": False, "availability": False, "lineups": True,
                    "formationsPublished": False}

WINDOW_BACK_DAYS = 3
WINDOW_AHEAD_DAYS = 7


def current_season(today: dt.date) -> str:
    start = today.year if today.month >= 7 else today.year - 1
    return f"{start}/{start + 1}"


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


_thread_local = threading.local()


def _thread_session() -> requests.Session:
    if not hasattr(_thread_local, "session"):
        _thread_local.session = requests.Session()
        _thread_local.session.headers.update(HEADERS)
    return _thread_local.session


def _details_empty(details) -> bool:
    """True when a details payload carries neither lineups nor stats.

    FotMob backfills some competitions' details (notably Iran's lineups)
    with a delay of days, so an empty payload for a finished match must
    not be cached as final — the next run should look again.
    """
    if not details:
        return True
    content = details.get("content") or {}
    lu = content.get("lineup") or {}
    has_lineup = any((lu.get(side) or {}).get("starters")
                     for side in ("homeTeam", "awayTeam"))
    return not has_lineup and not content.get("stats")


def match_details(match_id, cache_dir: str, force: bool = False):
    """Fetch (and disk-cache) match details; None when unavailable.

    Played matches are cached (their details stop changing once complete),
    except that a cached payload that is still empty (see _details_empty)
    is re-fetched in case FotMob has backfilled it. Upcoming/live matches
    pass force=True because lineups and unavailable lists appear and
    change before kickoff.
    """
    path = os.path.join(cache_dir, f"{match_id}.json")
    if not force and os.path.exists(path):
        try:
            with open(path) as f:
                cached = json.load(f)
            if not _details_empty(cached):
                return cached
        except Exception:  # noqa: BLE001
            pass
    try:
        details = get_json(
            _thread_session(),
            f"https://www.fotmob.com/api/data/matchDetails?matchId={match_id}")
    except RuntimeError:
        return None
    os.makedirs(cache_dir, exist_ok=True)
    with open(path, "w") as f:
        json.dump(details, f)
    return details


def cached_details(match_id, cache_dir: str):
    """Details only if already in the cache (never fetches)."""
    path = os.path.join(cache_dir, f"{match_id}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def fixture_status(m) -> str:
    st = m.get("status") or {}
    if st.get("cancelled"):
        return "cancelled"
    if st.get("finished"):
        return "finished"
    if st.get("started"):
        return "live"
    reason = ((st.get("reason") or {}).get("short") or "").upper()
    if reason in ("PP", "POSTP"):
        return "postponed"
    return "upcoming"


def team_ref(side: dict) -> dict:
    return {"teamId": side.get("id"), "name": side.get("name"),
            "shortName": side.get("shortName") or side.get("name")}


def as_int_id(value):
    """Match ids arrive as strings from league payloads, ints elsewhere."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def fixture_obj(m) -> dict:
    st = m.get("status") or {}
    status = fixture_status(m)
    live_time = (st.get("liveTime") or {}).get("short")
    return {
        "id": as_int_id(m.get("id")),
        "kickoffUtc": st.get("utcTime"),
        "status": status,
        "round": (m.get("tournament") or {}).get("stage") or None,
        "home": team_ref(m.get("home") or {}),
        "away": team_ref(m.get("away") or {}),
        "homeScore": (m.get("home") or {}).get("score") if status in ("live", "finished") else None,
        "awayScore": (m.get("away") or {}).get("score") if status in ("live", "finished") else None,
        "minute": live_time if status == "live" else None,
    }


def parse_table(payload) -> list:
    groups = payload.get("table") or []
    rows = []
    for group in groups if isinstance(groups, list) else []:
        table = ((group.get("data") or {}).get("table") or {})
        for row in table.get("all") or []:
            gf = ga = None
            scores = row.get("scoresStr") or ""
            if "-" in scores:
                try:
                    gf, ga = (int(x) for x in scores.split("-", 1))
                except ValueError:
                    gf = ga = None
            rows.append({
                "position": row.get("idx"),
                "teamId": row.get("id"),
                "name": row.get("name"),
                "played": row.get("played"),
                "won": row.get("wins"),
                "drawn": row.get("draws"),
                "lost": row.get("losses"),
                "goalsFor": gf,
                "goalsAgainst": ga,
                "goalDifference": row.get("goalConDiff"),
                "points": row.get("points", row.get("pts")),
            })
    rows.sort(key=lambda r: r["position"] if r["position"] is not None else 999)
    return rows


def xg_from_details(details):
    """(home_xg, away_xg) from a matchDetails payload, else (None, None).

    The stats tree nests `expected_goals` inside sections (e.g. under
    "Top stats"), so search recursively; values may be strings.
    """
    def find(o):
        if isinstance(o, dict):
            if o.get("key") == "expected_goals":
                return o.get("stats")
            for v in o.values():
                r = find(v)
                if r is not None:
                    return r
        if isinstance(o, list):
            for v in o:
                r = find(v)
                if r is not None:
                    return r
        return None

    if not details:
        return None, None
    vals = find(((details.get("content") or {}).get("stats") or {}))
    if vals and len(vals) == 2:
        try:
            return float(vals[0]), float(vals[1])
        except (TypeError, ValueError):
            return None, None
    return None, None


def derive_formation(starters) -> str | None:
    """Coarse formation (DEF-MID-FWD counts) from starter position lines."""
    counts = {1: 0, 2: 0, 3: 0}
    for p in starters:
        pos = p.get("usualPlayingPositionId")
        if pos in counts:
            counts[pos] += 1
    if sum(counts.values()) != 10:
        return None
    return f"{counts[1]}-{counts[2]}-{counts[3]}"


def player_obj(p) -> dict:
    return {
        "playerId": p.get("id"),
        "name": p.get("name"),
        "shirtNumber": p.get("shirtNumber"),
        "positionId": p.get("usualPlayingPositionId"),
    }


def lineup_side(team: dict):
    if not team:
        return None
    starters = team.get("starters") or []
    if not starters and not team.get("unavailable"):
        return None
    formation = team.get("formation")
    source = "fotmob" if formation else None
    if not formation and starters:
        formation = derive_formation(starters)
        source = "derived" if formation else None
    coach = team.get("coach")
    if isinstance(coach, dict):
        coach = coach.get("name")
    subs = team.get("subs") or team.get("substitutes")
    return {
        "formation": formation,
        "formationSource": source,
        "coach": coach,
        "starters": [player_obj(p) for p in starters],
        "substitutes": [player_obj(p) for p in subs] if subs else None,
    }


EVENT_TYPES = {
    "Goal": "goal", "Card": "card", "Subst": "substitution",
    "Var": "var", "Penalty": "penalty",
}


def parse_events(details) -> list:
    if not details:
        return []
    mf = ((details.get("content") or {}).get("matchFacts") or {})
    raw = mf.get("events") or {}
    events = raw.get("events") if isinstance(raw, dict) else None
    out = []
    for e in events or []:
        if e.get("type") in ("Half", "HalfTime"):
            continue  # period markers are not match events for the app
        etype = EVENT_TYPES.get(e.get("type"), (e.get("type") or "").lower())
        if e.get("type") == "Card":
            etype = "red_card" if (e.get("card") or "").lower().startswith("red") else "yellow_card"
        if e.get("type") == "Goal" and e.get("isPenalty"):
            etype = "penalty_goal"
        player = e.get("player") or {}
        minute = e.get("time")
        if e.get("overloadTime"):
            minute = f"{minute}+{e['overloadTime']}"
        # FotMob's homeScore/awayScore on an event are the score BEFORE the
        # event; the contract carries the score after it, so a goal adds
        # one to the scoring side (the other side for an own goal).
        home_score, away_score = e.get("homeScore"), e.get("awayScore")
        if etype in ("goal", "penalty_goal") and home_score is not None:
            home_scored = bool(e.get("isHome")) != bool(e.get("ownGoal"))
            if home_scored:
                home_score += 1
            else:
                away_score += 1
        out.append({
            "minute": minute,
            "type": etype,
            "team": "home" if e.get("isHome") else "away",
            "player": player.get("name") or e.get("nameStr"),
            "assist": e.get("assistStr"),
            "homeScore": home_score,
            "awayScore": away_score,
        })
    return out


def detail_obj(match_id, details):
    if not details:
        return None
    content = details.get("content") or {}
    lu = content.get("lineup") or {}
    home_lu = lineup_side(lu.get("homeTeam") or {})
    away_lu = lineup_side(lu.get("awayTeam") or {})
    lineups = None
    if home_lu or away_lu:
        lineups = {"home": home_lu, "away": away_lu}
    unavailable = None
    if lu.get("homeTeam") or lu.get("awayTeam"):
        unavailable = {}
        for side_key, side in (("homeTeam", "home"), ("awayTeam", "away")):
            entries = (lu.get(side_key) or {}).get("unavailable") or []
            unavailable[side] = [
                {"playerId": p.get("id"), "name": p.get("name"),
                 "reason": p.get("type") or p.get("reason")}
                for p in entries
            ]
    home_xg, away_xg = xg_from_details(details)
    events = parse_events(details)
    if lineups is None and unavailable is None and not events and home_xg is None:
        return None
    return {
        "matchId": as_int_id(match_id),
        "homeXg": home_xg,
        "awayXg": away_xg,
        "lineups": lineups,
        "unavailable": unavailable,
        "events": events,
    }


def collect_league(session, slug, season, today, out_dir, cache_dir, workers):
    fotmob_id, name, country = LEAGUES[slug]
    url = (f"https://www.fotmob.com/api/data/leagues"
           f"?id={fotmob_id}&season={season}")
    payload = get_json(session, url)
    matches = find_match_list(payload)
    win_from = today - dt.timedelta(days=WINDOW_BACK_DAYS)
    win_to = today + dt.timedelta(days=WINDOW_AHEAD_DAYS)

    fixtures, history_matches, window_ids = [], [], []
    for m in matches:
        st = m.get("status") or {}
        utc = st.get("utcTime") or ""
        if not utc:
            continue
        day = dt.date.fromisoformat(utc[:10])
        status = fixture_status(m)
        if status == "finished":
            hxg, axg = xg_from_details(cached_details(m["id"], cache_dir))
            history_matches.append({
                "id": as_int_id(m["id"]),
                "date": utc[:10],
                "kickoffUtc": utc,
                "season": season,
                "home": team_ref(m.get("home") or {}),
                "away": team_ref(m.get("away") or {}),
                "homeGoals": (m.get("home") or {}).get("score"),
                "awayGoals": (m.get("away") or {}).get("score"),
                "homeXg": hxg,
                "awayXg": axg,
            })
        if win_from <= day <= win_to:
            fixtures.append(fixture_obj(m))
            window_ids.append((m["id"], status == "finished"))

    # Details for every window match (played ones from cache where possible).
    details = {}
    def fetch(pair):
        mid, finished = pair
        return mid, detail_obj(mid, match_details(mid, cache_dir,
                                                   force=not finished))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for mid, obj in ex.map(fetch, window_ids):
            if obj is not None:
                details[str(mid)] = obj
    # Window details may have filled the cache with xG for recent history.
    for h in history_matches:
        if h["homeXg"] is None:
            hxg, axg = xg_from_details(cached_details(h["id"], cache_dir))
            h["homeXg"], h["awayXg"] = hxg, axg

    fixtures.sort(key=lambda f: f["kickoffUtc"] or "")
    history_matches.sort(key=lambda h: h["kickoffUtc"] or "")
    generated = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    league_meta = {"slug": slug, "fotmobId": fotmob_id, "name": name,
                   "country": country, "season": season}
    bundle = {
        "contractVersion": 2,
        "league": league_meta,
        "generatedAt": generated,
        "window": {"from": win_from.isoformat(), "to": win_to.isoformat()},
        "features": FEATURES[slug],
        "fixtures": fixtures,
        "table": parse_table(payload),
        "details": details,
    }
    history = {
        "contractVersion": 2,
        "league": {k: league_meta[k] for k in ("slug", "fotmobId", "name")},
        "generatedAt": generated,
        "matches": history_matches,
    }
    league_dir = os.path.join(out_dir, slug)
    os.makedirs(league_dir, exist_ok=True)
    with open(os.path.join(league_dir, "bundle.json"), "w") as f:
        json.dump(bundle, f, ensure_ascii=False, indent=1)
    with open(os.path.join(league_dir, "history.json"), "w") as f:
        json.dump(history, f, ensure_ascii=False, indent=1)
    return {
        "slug": slug,
        "fixtures": len(fixtures),
        "finished_in_window": sum(1 for f in fixtures if f["status"] == "finished"),
        "table_rows": len(bundle["table"]),
        "details": len(details),
        "with_lineups": sum(1 for d in details.values() if d["lineups"]),
        "with_unavailable": sum(
            1 for d in details.values()
            if d["unavailable"] and (d["unavailable"]["home"] or d["unavailable"]["away"])),
        "with_events": sum(1 for d in details.values() if d["events"]),
        "history": len(history_matches),
        "history_with_xg": sum(1 for h in history_matches if h["homeXg"] is not None),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--leagues", default=",".join(LEAGUES),
                    help="comma-separated league slugs")
    ap.add_argument("--out-dir", default="data/v2")
    ap.add_argument("--cache-dir", default=".cache")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    session = requests.Session()
    session.headers.update(HEADERS)
    today = dt.datetime.now(dt.timezone.utc).date()
    season = current_season(today)
    cache_dir = os.path.join(args.cache_dir, "matchdetails")
    summaries = []
    for slug in [s.strip() for s in args.leagues.split(",") if s.strip()]:
        if slug not in LEAGUES:
            print(f"unknown league slug: {slug}", file=sys.stderr)
            return 1
        summaries.append(collect_league(session, slug, season, today,
                                        args.out_dir, cache_dir, args.workers))
    for s in summaries:
        print(json.dumps(s, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
