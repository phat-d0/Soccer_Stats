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

## iPhone app

A phone-first web app you add to your home screen. It opens full-screen with its own
icon, works offline, and follows your phone's dark mode. Tabs: **Matches** (next round
with value picks; tap for markets and scoreline heatmap), **Teams** (ratings + xG),
**Record** (replayed profit, closing line value, goals vs xG), **Portfolio** (paper
trades and the DraftKings backtest), **Explore** (any matchup).

A GitHub Actions job (`.github/workflows/publish.yml`) re-runs the model every hour,
and every 15 minutes from 10:00 to 22:00 UTC, and publishes the result to GitHub Pages.

**Team news (player availability):** each run reads injuries, suspensions and "chance of
playing" from the Fantasy Premier League API (`src/soccer_stats/players.py`). For each
team's next match, a missing player's share of the team's chance creation (xG + xA per
90, weighted by his usual minutes and shrunk toward his position's average) is taken off
its expected goals, with a below-average stand-in replacing him; missing regular
keepers/defenders raise goals conceded by small fixed amounts. The app shows the
absences and the probabilities with and without the adjustment. These adjustments
can't be backtested yet (FPL keeps no history), so the workflow logs every change to
the `data-log` branch (`fpl_news/*.jsonl`) to build one.

**DraftKings odds:** upcoming-match odds, bookmaker probabilities and edges come from
DraftKings via [The Odds API](https://the-odds-api.com) (`src/soccer_stats/odds_feed.py`),
shown as American odds. Add a free API key as the repo secret `ODDS_API_KEY`
(Settings → Secrets and variables → Actions). Refreshes are budgeted: each build checks the
credits left and what the last call cost, and spaces refreshes so the 500 monthly free
credits last until the reset (`ODDS_API_RESET_DAY`, set to the 5th in the workflow; the
real day is learned automatically when credits go back up), keeping 20 in reserve and
never refreshing more than hourly, or every 30 minutes in the two hours before a kickoff
(on the free plan that works out at about every 3 hours; a paid plan reaches the floor). If paused on the
reserve it checks once a day in case the allowance has reset.
Refreshing the app on your phone never uses credits. Without
a key the app falls back to football-data's odds. The Record tab still replays against
football-data's historical odds, since past DraftKings prices aren't available.

## Trade rule, paper trades and the Portfolio tab

The goal is to learn whether betting DraftKings at a 12% claimed edge actually makes
money, measured on realized results rather than the edge the model claims. One rule
(`src/soccer_stats/trades.py`) decides every trade, in the backtest, the live log and
the app's value pick (`bestPick` in `web/app.js`):

| Rule | Value |
| --- | --- |
| Markets | Home, draw, away, over 2.5, under 2.5 |
| Edge | model probability × DraftKings decimal odds − 1 |
| Paper-trade threshold | edge ≥ 12% (`PAPER_EDGE`), whatever the app's filter shows |
| App filter presets | 2%, 5%, 8%, 12%; default 5% |
| Trades per match | one, the market with the highest edge |
| Odds cap | none (the backtest also reports a 6.0 cap) |
| Thin data | skip if either team has under 6 matches in the training window |
| Stake | flat $10 |
| Probability | the one the app shows (team news included); the one without is stored too |
| Live entry | first build where the pick qualifies, odds under 3 hours old, before kickoff |
| Backtest entry | first qualifying look, 48 and 3 hours before kickoff |
| Close | last DraftKings price before kickoff (for closing line value) |
| Settlement | 90-minute result from football-data; void if kickoff moves 48h+ or no result in 14 days |

**Live paper trades** (`paper.py`): after each build, `soccer-stats paper` updates an
append-only ledger at `paper_trades/E0_<season>.jsonl` on the `data-log` branch. A trade's
entry (time, price, probability, edge) is written once and never edited; later lines only
add the closing price and the settlement. Rebuilding adds nothing new. If the ledger or
the odds are unavailable, the site still publishes, opens nothing, and the tab says why.

**DraftKings backtest** (needs a paid Odds API plan for historical odds):

```bash
uv run soccer-stats backfill-odds --seasons 2025 --dry-run          # snapshots + credits, no calls
uv run soccer-stats backfill-odds --seasons 2025 --max-credits 5000 # cached, never fetched twice
uv run soccer-stats backtest-dk --seasons 2025 --out dk_trades.csv  # report + one row per trade
```

Or from the **Actions** tab: *Backfill DraftKings odds* → Run workflow (a dry run by
default). It downloads the snapshots (two looks and the close per kickoff; kickoffs within
15 minutes share them; about 20 credits each, so roughly 12,000 credits a season), runs
the backtest and saves its summary and trade list to `backtest/` on the `data-log` branch,
where the Portfolio tab picks it up. A backfill stops at `--max-credits` and never lets
credits left fall below `--keep-credits` (1,500 by default) so live refreshes keep going.
Raw snapshots stay in the workflow's cache and are not committed.

Each look uses a model fitted only on matches played before that look's refit date,
and prices from snapshots taken at or before the look. Team news can't be replayed (its
history starts October 2026), so backtest trades use the probability without it. The
report gives trades, staked, profit, ROI with a standard error and a bootstrap 95%
interval (resampling match weeks), win rate against break-even, average claimed edge,
closing line value against DraftKings' and Pinnacle's close, maximum drawdown, model vs
DraftKings log loss, breakdowns by season, market, edge, odds and look, and a threshold
sweep at 2, 5, 8, 12, 15 and 20% with and without a 6.0 odds cap.

**Reading the Portfolio tab:** switch between *Live paper* and *Backtest*; they are
never mixed. Look at the trade count first: at 12% there are only a few trades a round,
and ROI on a small sample is mostly noise. Trust closing line value over ROI: beating
the close on average (especially Pinnacle's) is the best sign of a real edge. If ROI
falls as the sweep's threshold rises, big claimed edges are mostly the model's mistakes,
and the next step is blending with the market, not more bets. Tap any trade for every
stored field.

**One-time setup**
1. On GitHub: repo **Settings → Pages → Build and deployment → Source: GitHub Actions**.
2. **Actions** tab → *Publish app* → **Run workflow** (or wait for the next scheduled run).
3. On your iPhone, open `https://phat-d0.github.io/Soccer_Stats/` in **Safari**, tap
   **Share → Add to Home Screen**.

Build it locally: `uv run soccer-stats publish --out _site && python -m http.server -d _site`
(add `uv run soccer-stats paper --site _site --log-dir <folder>` to try the ledger).
The app code lives in `web/`; `src/soccer_stats/publish.py` writes the `data.json` it reads.

## Streamlit app (desktop)

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
  publish.py            builds the phone app's data.json
  players.py            FPL team news -> attack/defence adjustments, injury history log
  odds_feed.py          DraftKings odds from The Odds API (cached, credit-aware)
  trades.py             the trade rule, settlement and metrics (no network or files)
  paper.py              live paper-trade ledger on the data-log branch
  odds_history.py       historical DraftKings snapshots: plan, budget, cache
app/streamlit_app.py    the Premier League dashboard (desktop)
web/                    the iPhone web app (HTML/CSS/JS, service worker, icons)
.github/workflows/      scheduled build + deploy to GitHub Pages; by-hand odds backfill
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
