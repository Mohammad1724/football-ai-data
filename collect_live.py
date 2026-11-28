#!/usr/bin/env python3
"""Collect the tiny v2 live file (see DATA_CONTRACT.md) from FotMob.

One request to FotMob's matches-by-date endpoint produces
data/v2/live.json: today's matches in the seven day-one leagues only —
id, status, minute, score. Designed to be regenerated every few minutes
(e.g. by a scheduled workflow) and polled by the app while it is open.
Live matches show score only; there is no live prediction, ever.

Usage:
    python3 collect_live.py [--out data/v2/live.json] [--date YYYY-MM-DD]
"""
import argparse
import datetime as dt
import json
import os
import sys
import time

import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-GB,en;q=0.9",
}

# FotMob league id -> v2 slug, AS SEEN IN THE MATCHES-BY-DATE ENDPOINT.
# Quirk (verified 2026-11-28): that endpoint uses the canonical league ids
# for some competitions (47, 87, 55, 42) but alternate 100000xxxx ids for
# others — Bundesliga is 1000005083 there (not 54), Ligue 1 is 1000006325
# (not 53) and the Persian Gulf is 1000001715 (not 523). A (name, ccode)
# fallback below covers any further remapping.
LEAGUE_SLUGS = {
    47: "premier-league",
    87: "la-liga",
    55: "serie-a",
    54: "bundesliga",
    1000005083: "bundesliga",
    53: "ligue-1",
    1000006325: "ligue-1",
    42: "champions-league",
    523: "iran",
    1000001715: "iran",
}

# (league name, country code) -> slug, fallback for the id mapping above.
LEAGUE_NAMES = {
    ("Premier League", "ENG"): "premier-league",
    ("LaLiga", "ESP"): "la-liga",
    ("Serie A", "ITA"): "serie-a",
    ("Bundesliga", "GER"): "bundesliga",
    ("Ligue 1", "FRA"): "ligue-1",
    ("Champions League", "EUR"): "champions-league",
    ("Persian Gulf", "IRN"): "iran",
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


def match_status(m) -> str:
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


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="data/v2/live.json")
    ap.add_argument("--date", default=None,
                    help="UTC date YYYY-MM-DD (default: today)")
    args = ap.parse_args()

    day = args.date or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    session = requests.Session()
    session.headers.update(HEADERS)
    payload = get_json(
        session,
        f"https://www.fotmob.com/api/data/matches?date={day.replace('-', '')}")

    matches = []
    for league in payload.get("leagues") or []:
        slug = (LEAGUE_SLUGS.get(league.get("id"))
                or LEAGUE_NAMES.get((league.get("name"), league.get("ccode"))))
        if slug is None:
            continue
        for m in league.get("matches") or []:
            st = m.get("status") or {}
            status = match_status(m)
            live_time = (st.get("liveTime") or {}).get("short")
            matches.append({
                "id": m.get("id"),
                "leagueSlug": slug,
                "kickoffUtc": st.get("utcTime"),
                "status": status,
                "minute": live_time if status == "live" else None,
                "home": {"teamId": (m.get("home") or {}).get("id"),
                         "name": (m.get("home") or {}).get("name")},
                "away": {"teamId": (m.get("away") or {}).get("id"),
                         "name": (m.get("away") or {}).get("name")},
                "homeScore": (m.get("home") or {}).get("score") if status in ("live", "finished") else None,
                "awayScore": (m.get("away") or {}).get("score") if status in ("live", "finished") else None,
            })
    matches.sort(key=lambda x: x["kickoffUtc"] or "")

    out = {
        "contractVersion": 2,
        "generatedAt": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "matches": matches,
    }
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    by_status = {}
    for m in matches:
        by_status[m["status"]] = by_status.get(m["status"], 0) + 1
    print(f"live.json: {len(matches)} matches for {day}: {by_status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
