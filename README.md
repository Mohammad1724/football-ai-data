# football-ai-data

Per-match expected goals (xG) for the big-5 European leagues, published as
plain CSV files for the [football-ai](https://github.com/Mohammad1724/football-ai)
Android app. The app's Dixon–Coles rating model blends these xG values into
its team-strength fit (a goals/xG blend validated by walk-forward backtest:
log loss 0.9939 → 0.9871, top-pick accuracy 52.2% → 52.8% on 6,566 big-5
forecasts, seasons 2021-22…2024-25).

## Data

One file per league and season under `data/xg/`:

```
data/xg/E0-2021-22.csv
data/xg/SP1-2021-22.csv
...
```

League codes follow football-data.co.uk: `E0` (Premier League), `SP1`
(La Liga), `D1` (Bundesliga), `I1` (Serie A), `F1` (Ligue 1). Seasons run
from 2021-22 to the current season.

Columns:

| column      | meaning                                             |
|-------------|-----------------------------------------------------|
| `date`      | match date (UTC), `YYYY-MM-DD`                      |
| `league`    | football-data.co.uk league code                     |
| `home_team` | home team, football-data.co.uk naming               |
| `away_team` | away team, football-data.co.uk naming               |
| `home_xg`   | expected goals of the home team (two decimals)      |
| `away_xg`   | expected goals of the away team (two decimals)      |

One row per played match. Team names deliberately use football-data.co.uk
naming so the files join directly onto the app's existing match history.

## Availability data

One file per league and season under `data/availability/`, produced by
`collect_availability.py` from the same FotMob source. One row per fixture
(played or upcoming):

| column         | meaning                                                          |
|----------------|------------------------------------------------------------------|
| `date`         | match date (UTC), `YYYY-MM-DD`                                   |
| `league`       | football-data.co.uk league code                                  |
| `home_team`    | home team, football-data.co.uk naming                            |
| `away_team`    | away team, football-data.co.uk naming                            |
| `missing_home` | share of the home team's season minutes belonging to players listed unavailable for this fixture (injured/suspended) |
| `missing_away` | same, for the away team                                          |

Shares are computed walk-forward: a fixture's row uses only lineups of
earlier matches that season and the unavailable list published before the
fixture, so the file is safe for backtests as well as live forecasts. A
share is empty when it cannot be computed (a team's first three matches of
a season, or nothing published yet) — empty means "no data", never zero.
The app turns the difference between the two shares into a bounded
expected-goals tilt (validated on untouched 2024-25 + 2025-26 seasons:
log loss 0.9843 → 0.9837, accuracy 53.11% → 53.20%).

## Champions League data

`collect_ucl.py` publishes the UEFA Champions League (FotMob league id 42),
which football-data.co.uk does not carry, in three products:

- `data/history/UCL-<SEASON>.csv` — one row per played match in the
  football-data.co.uk layout (`Div,Date,Time,HomeTeam,AwayTeam,FTHG,FTAG`),
  the format the app's history importer already parses. Scores are
  **regulation (90-minute) scores**: matches decided after extra time or on
  penalties are flagged by FotMob, and their 90-minute score is
  reconstructed from the match's goal events (the reconstruction was
  verified against the published score on 439 regulation matches with zero
  mismatches). Kick-offs are Europe/London, the zone that importer assumes.
- `data/xg/UCL-<SEASON>.csv` — per-match xG, same layout as the big-5
  files. Extra-time/penalty matches are omitted: their published xG covers
  120 minutes and is not comparable to a 90-minute row.
- `data/availability/UCL-<SEASON>.csv` — missing-minutes shares, same
  layout and walk-forward discipline as the big-5 availability files
  (minutes accumulate from this competition's own earlier matches).

Team names are FotMob's verbatim — there is no football-data.co.uk naming
to align to for this competition, so FotMob naming is canonical for all
three files. Coverage: every season from 2021-22 to the current one
(825 matches through Nov 2026). A walk-forward backtest of the app's model
on this history (train 2021-22…2023-24, test 2024-25) gives log loss
0.9985 / Brier 0.5956 / 52.8% top-pick accuracy against a 49.6%
always-home baseline on the same matches — solidly useful, a notch below
the domestic big-5 numbers, as expected for cross-league fixtures.

The daily workflow (`.github/workflows/update-xg.yml`, 05:17 UTC) runs all
three collectors — `collect_xg.py`, `collect_availability.py` and
`collect_ucl.py` — and commits any new rows.

## How it is produced

`collect_xg.py` (Python 3, only dependency: `requests`) takes the fixture
list from FotMob's public web API and reads each match's published
"Expected goals (xG)" statistic (FotMob's data is Opta-fed). Where
football-data.co.uk already lists the same fixture, the final score is
cross-verified against it before a row is accepted; mismatches are dropped
and logged. A GitHub Actions workflow (`.github/workflows/update-xg.yml`)
re-runs all three collectors daily (05:17 UTC); each is incremental — only
newly played matches are fetched — and commits any new rows.

```bash
pip install -r requirements.txt
python3 collect_xg.py                 # all leagues, all seasons
python3 collect_xg.py --seasons 2627  # one season (football-data code)
```

## Source, licensing and personal-use note

xG values are statistics published by FotMob (data © Opta / Stats Perform
and FotMob); fixture and score reference data is from football-data.co.uk.
This repository exists solely as a personal, non-commercial mirror so that a
personal app can read the numbers from a stable URL — the same pattern the
app already uses for football-data.co.uk CSV mirrors. All rights in the
underlying data remain with the respective providers; if you are a rights
holder and want this mirror taken down, open an issue. Do not use these
files commercially.
