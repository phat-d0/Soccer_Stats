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

## Quick start

```bash
# Walk-forward backtest on the Premier League, seasons 2019/20–2024/25,
# training on the first two seasons before predicting.
uv run soccer-stats backtest --league E0 --seasons 2019-2024 --out preds.csv
```

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
tests/                  unit tests on synthetic leagues with known parameters
notebooks/              exploration
```

## Ideas to try next

- Tune `xi` (time decay) and the lookback window by out-of-sample log loss
- Elo / pi-ratings as a second model; blend with Dixon-Coles
- Expected-goals (xG) inputs instead of goals (e.g. Understat / FBref)
- Over/under and Asian handicap markets (often softer than 1X2)
- Market-blending: model probability shrunk toward the market, bet only on large disagreements
- Lower leagues, where bookmaker prices are less efficient
