# soccer_stats

Models for soccer match outcomes, built to find a betting edge against bookmaker odds.

The approach in one line: estimate match probabilities, compare them with the
**de-vigged Pinnacle closing line** (the sharpest public price), and only trust an
edge that holds up in a walk-forward backtest, with **closing line value (CLV)**
as the main yardstick, since ROI is noisy.

## Setup

```bash
uv sync                 # installs deps + dev tools into .venv
uv run pytest           # run tests
```

## Premier League app

A dashboard to follow the model through the season:

- **Upcoming matches**: win/draw/loss and over 2.5 chances for the next round, Pinnacle
  odds, and any value pick above your edge threshold. Open a match for expected goals,
  fair odds and a scoreline heatmap.
- **Team ratings**: how many goals each team scores and concedes against an average side,
  plus this season's xG for and against.
- **Track record**: the model replayed over last season and this one, with profit,
  closing line value and accuracy against the bookmaker, and a goals-only vs goals + xG
  comparison.
- **Match explorer**: any two teams head to head.

```bash
uv sync --extra app
uv run streamlit run app/streamlit_app.py     # opens http://localhost:8501
```

Data refreshes automatically every few hours (or with the "Refresh data now" button).

**On your phone:** deploy free on [Streamlit Community Cloud](https://share.streamlit.io):
sign in with GitHub → *Create app* → pick this repo, branch, and `app/streamlit_app.py`.
It installs from `requirements.txt` and gives you a URL you can bookmark.

## Quick start (backtest from the command line)

```bash
# Walk-forward backtest on the Premier League, seasons 2019/20–2024/25,
# training on the first two seasons before predicting.
uv run soccer-stats backtest --league E0 --seasons 2019-2024 --out preds.csv
```

### Expected goals (xG)

The model can rate teams on a blend of xG (from [Understat](https://understat.com), top-5
leagues since 2014/15) and real goals. xG measures chance quality, so it tells good
teams from lucky ones faster than goals do. `--xg-weight` sets the blend
(0 = goals only, 1 = xG only); pass several values to compare them on the same matches:

```bash
uv run soccer-stats backtest --league E0 --seasons 2019-2024 --xg-weight 0 0.5 0.7 1
```

Pick the weight with the lowest model log loss, then set `XG_WEIGHT` in
`app/streamlit_app.py` to match (default 0.7). If Understat can't be reached, everything
falls back to goals only.

The output shows:
- **log loss / Brier**, model vs. market. If the model can't get close to the market
  here, betting edges it "finds" are almost certainly noise.
- **betting sim**: flat stakes at Pinnacle *opening* odds wherever model edge > `--min-edge`,
  with ROI and average CLV against the de-vigged close.

Data comes from [football-data.co.uk](https://www.football-data.co.uk/) and is cached in `data/raw/`.

## Layout

```
src/soccer_stats/
  data.py               download + normalize football-data.co.uk CSVs (results + odds)
  odds.py               implied probs, margin removal (proportional, Shin), edge, Kelly
  markets.py            score matrix -> 1X2, over/under, BTTS, Asian handicap
  models/dixon_coles.py Dixon-Coles Poisson model with time decay
  backtest.py           walk-forward predictions, scoring vs. market, bet simulation
  cli.py                `soccer-stats` command
  dashboard.py          data shaping for the app (fixtures, ratings, track record)
  xg.py                 Understat xG download, team-name mapping, merge onto matches
app/streamlit_app.py    the Premier League dashboard
tests/                  unit tests on synthetic leagues with known parameters
notebooks/              exploration
```

## Ideas to try next

- Tune `xi` (time decay) and the lookback window by out-of-sample log loss
- Elo / pi-ratings as a second model; blend with Dixon-Coles
- Non-penalty xG, and separate weights for attack vs defence
- Over/under and Asian handicap markets (often softer than 1X2)
- Market-blending: model probability shrunk toward the market, bet only on large disagreements
- Lower leagues, where bookmaker prices are less efficient
