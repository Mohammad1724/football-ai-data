# football-ai data contract v2 (rewrite)

App-facing data contract for the rewritten football-ai app. Single source:
the FotMob mirror pipeline in this repo. The app downloads static JSON files
from `data/v2/` (via the repo's raw URLs / CDN) — no other provider exists.

Producers:

- `collect_v2.py` — daily. Writes one `bundle.json` and one `history.json`
  per league.
- `collect_live.py` — every few minutes. Writes a single `live.json`.

Conventions:

- All timestamps are UTC ISO-8601 (`2026-11-28T15:00:00Z`).
- Team names are FotMob's, verbatim, and are canonical in v2 (the v1 CSVs
  align big-5 names to football-data.co.uk; v2 does not). Teams are always
  identified by their FotMob `teamId` as well as by name.
- `null` means "not published / not available", never zero. The app renders
  an explicit "data unavailable" note for null sections (locked product
  rule) instead of silently omitting them.
- Optional-per-league capabilities are declared in each bundle's `features`
  block (see below), so the app never has to guess.

## Leagues (day one)

| slug              | FotMob id | competition        | xG   | availability | formation |
|-------------------|-----------|--------------------|------|--------------|-----------|
| `premier-league`  | 47        | Premier League     | yes  | yes          | published |
| `la-liga`         | 87        | La Liga            | yes  | yes          | published |
| `serie-a`         | 55        | Serie A            | yes  | yes          | published |
| `bundesliga`      | 54        | Bundesliga         | yes  | yes          | published |
| `ligue-1`         | 53        | Ligue 1            | yes  | yes          | published |
| `champions-league`| 42        | Champions League   | yes* | yes          | published |
| `iran`            | 523       | Persian Gulf (IRN) | **no** | **no**     | derived   |

Verified against live FotMob responses on 2026-11-28 (see "Iran coverage
verdict" at the bottom). `*` UCL xG covers regulation play only, as in v1.

- **Iran has no xG**: FotMob's match details for league 523 carry an empty
  stats block (no `expected_goals`, no shotmap). The engine must run Iran
  **without the xG blend** (`features.xg = false`).
- **Iran has no unavailable data**: the pre-match `unavailable` lists are
  empty for every sampled Iran match (53 checked). `features.availability =
  false`; the reasons page shows the "injuries data unavailable" note.
- **Iran formations are derived**: FotMob publishes starters and the coach
  for Iran (~93% of played matches) but no formation string and no
  substitutes list. `formation` is derived from the starters'
  `usualPlayingPositionId` line counts (e.g. `4-4-2`) and flagged with
  `formationSource: "derived"`; it cannot express split lines (4-2-3-1).

## `data/v2/<slug>/bundle.json` (daily)

One file per league covering the app window: **3 days back to 7 days ahead**
of `generatedAt` (the home timeline range), plus the current league table.

```jsonc
{
  "contractVersion": 2,
  "league": {
    "slug": "iran",
    "fotmobId": 523,
    "name": "Persian Gulf",
    "country": "IRN",
    "season": "2026/2027"
  },
  "generatedAt": "2026-11-28T11:00:00Z",
  "window": { "from": "2026-11-25", "to": "2026-12-05" },
  "features": {
    "xg": false,            // league-level: does FotMob publish xG?
    "availability": false,  // league-level: pre-match unavailable lists?
    "lineups": true,        // starters published (per-match detail may vary)
    "formationsPublished": false
  },
  "fixtures": [ /* every match in the window, all statuses */ ],
  "table":    [ /* current standings; [] for knock-out-only phases */ ],
  "details":  { "<matchId>": { /* per-match detail, when published */ } }
}
```

### Fixture object

```jsonc
{
  "id": 1000023188,                 // FotMob match id
  "kickoffUtc": "2026-11-28T11:45:00Z",
  "status": "upcoming",             // upcoming | live | finished | postponed | cancelled
  "round": "13",                    // tournament stage / match round, may be null
  "home": { "teamId": 101614, "name": "Tractor", "shortName": "Tractor" },
  "away": { "teamId": 101613, "name": "Zob Ahan", "shortName": "Zob Ahan" },
  "homeScore": null,                // null until the match starts
  "awayScore": null,
  "minute": null                    // live minute label ("67'", "HT") when live
}
```

Finished fixtures double as the window's **results** (the app filters
`status == "finished"`); there is no separate results file for the window.

### Table row

```jsonc
{
  "position": 2,
  "teamId": 1642,
  "name": "Persepolis",
  "played": 13, "won": 8, "drawn": 3, "lost": 2,
  "goalsFor": 20, "goalsAgainst": 10, "goalDifference": 10,
  "points": 27
}
```

### Match detail object (`details["<matchId>"]`)

Present only when FotMob has published something for the match (lineups,
unavailable lists, or — for played matches — events). Any sub-field may be
`null` independently.

```jsonc
{
  "matchId": 1000018829,
  "homeXg": null,                   // null when the league has no xG or the
  "awayXg": null,                   //   match's stats were not published
  "lineups": {
    "home": {
      "formation": "4-4-2",
      "formationSource": "derived", // fotmob | derived
      "coach": "Osmar Loss",
      "starters": [
        { "playerId": 900780, "name": "Mohammad Amin Kazemian",
          "shirtNumber": 69, "positionId": 2 }   // 0 GK, 1 DEF, 2 MID, 3 FWD
      ],
      "substitutes": null           // null when FotMob does not publish them
    },
    "away": { /* same shape */ }
  },
  "unavailable": {
    "home": [ { "playerId": 123, "name": "…", "reason": "injury" } ],
    "away": []
  },
  "events": [
    { "minute": 43, "type": "goal",        // goal | yellow_card | red_card |
      "team": "home",                      //   substitution | penalty_goal …
      "player": "Aref Rostami", "assist": null,
      "homeScore": 1, "awayScore": 0 }     // score after the event
  ]
}
```

Notes:

- Lineups publish ~1 hour before kickoff; before that `lineups` is `null`
  and the app shows its "published ~1 hour before kickoff" placeholder.
  For the big five + UCL, FotMob publishes the `unavailable` lists well
  before lineups, so a pre-kickoff detail may carry `unavailable` while
  `lineups.<side>.starters` is still an empty list — the app treats empty
  starters as "not yet published", not as a lineup of zero players.
  A near-kickoff refresh of a single match re-runs `collect_v2.py` (the
  bundle is regenerated; unchanged matches come from the details cache).
- `unavailable[].reason` is FotMob's free-text/type string when present
  (`injury`, `suspension`, …), otherwise `null`. Player **names** are part
  of the contract — the reasons page lists them.
- Events come from `matchFacts.events`; scores are regulation (90-minute)
  scores, consistent with v1.

## `data/v2/<slug>/history.json` (daily)

Season history the engine rates teams from: **every played match of the
current season** (the daily run may be extended to earlier seasons with the
same shape; consumers must tolerate multiple seasons in one file).

```jsonc
{
  "contractVersion": 2,
  "league": { "slug": "iran", "fotmobId": 523, "name": "Persian Gulf" },
  "generatedAt": "2026-11-28T11:00:00Z",
  "matches": [
    {
      "id": 1000018829,
      "date": "2026-11-20",                 // UTC date of kickoff
      "kickoffUtc": "2026-11-20T13:00:00Z",
      "season": "2026/2027",
      "home": { "teamId": 394619, "name": "Esteghlal Khuzestan" },
      "away": { "teamId": 101612, "name": "Malavan" },
      "homeGoals": 1, "awayGoals": 0,       // regulation score
      "homeXg": null, "awayXg": null        // null where not published
    }
  ]
}
```

xG in history is populated when the match's details were fetched (the
bundle window always is; older matches only when the shared details cache
already holds them). Full-history xG for the big five + UCL continues to be
published as the v1 `data/xg/*.csv` files; v2 history guarantees **scores
for every played match**, which is what ratings need, with xG as a
best-effort enrichment. For Iran, xG is always `null`.

## `data/v2/live.json` (every few minutes)

Tiny file: today's matches (UTC date of `generatedAt`) in the 7 leagues
only. Designed to be regenerated every few minutes by `collect_live.py`
(one FotMob request) and polled by the app while it is open. Live matches
show score + minute only — there is no live prediction, ever.

Quirk (verified 2026-11-28): the matches-by-date endpoint does **not** use
the canonical league ids for every competition — Bundesliga appears as
`1000005083`, Ligue 1 as `1000006325` and the Persian Gulf as `1000001715`
there, while the Premier League, La Liga, Serie A and the Champions League
keep their canonical ids. `collect_live.py` maps both id sets plus a
(name, country) fallback. The league-payload endpoints used by
`collect_v2.py` use the canonical ids throughout.

```jsonc
{
  "contractVersion": 2,
  "generatedAt": "2026-11-28T11:52:00Z",
  "matches": [
    {
      "id": 1000023188,
      "leagueSlug": "iran",
      "kickoffUtc": "2026-11-28T11:45:00Z",
      "status": "live",              // upcoming | live | finished | postponed | cancelled
      "minute": "7'",                // null unless live
      "home": { "teamId": 101614, "name": "Tractor" },
      "away": { "teamId": 101613, "name": "Zob Ahan" },
      "homeScore": 0, "awayScore": 0
    }
  ]
}
```

## Iran coverage verdict (verified 2026-11-28, live FotMob API)

League 523, season 2026/2027: 153 fixtures (117 played, 36 upcoming at
probe time).

| data                | present? | evidence |
|---------------------|----------|----------|
| fixtures / results  | yes      | full season list in the league payload |
| league table        | yes      | `table[0].data.table.all` in the league payload |
| lineups (starters)  | yes, delayed | 28/30 sampled played matches have 11 starters per side — but the most recent matchdays (25–27 Nov 2026) had **empty** lineup sections at generation time: FotMob backfills Iran lineups with a delay of days. `collect_v2.py` therefore re-fetches finished matches whose cached details are still empty, and a bundle may legitimately show `lineups: null` for a just-played Iran match until the backfill lands |
| coach names         | yes      | same 28/30 |
| formations          | **no**   | 0/30 publish a formation string; derived from starter positions |
| substitutes         | **no**   | 0/30 publish a bench list |
| match events        | yes      | `matchFacts.events` present in 30/30 |
| xG                  | **no**   | 0/30 — stats block empty, no `expected_goals`, no shotmap |
| unavailable (inj./susp.) | **no** | 0 entries across 53 matches checked (played + upcoming) |

Consequence (locked product rule): the engine runs Iran **without the xG
blend and without the availability adjustment**, and the app labels those
factors "data unavailable" in the reasons page instead of omitting them.
