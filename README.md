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

## How it is produced

`collect_xg.py` (Python 3, only dependency: `requests`) takes the fixture
list from FotMob's public web API and reads each match's published
"Expected goals (xG)" statistic (FotMob's data is Opta-fed). Where
football-data.co.uk already lists the same fixture, the final score is
cross-verified against it before a row is accepted; mismatches are dropped
and logged. A GitHub Actions workflow (`.github/workflows/update-xg.yml`)
re-runs the collector daily; it is incremental — only newly played matches
are fetched — and commits any new rows.

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
