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
with value picks; tap for model, blend and DraftKings side by side, player lines and the
scoreline heatmap), **Teams** (ratings + xG), **Record** (match bets: model alone vs
blend against DraftKings' prices with the threshold sweep, then the longer Pinnacle
replay; player shots: the FanDuel strategies), **Portfolio** (paper trades and the
DraftKings backtest, breakdowns folded away), **Explore** (any matchup).

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
| Probability for the edge | the blend (model + DraftKings' margin-free price, below) when a fit is saved; else the model's own |
| Edge | that probability × DraftKings decimal odds − 1 |
| Paper-trade threshold | edge ≥ 12% (`PAPER_EDGE`), whatever the app's filter shows |
| App filter presets | 2%, 5%, 8%, 12%; default 5% |
| Trades per match | one, the market with the highest edge |
| Odds cap | none (the backtest also reports a 6.0 cap) |
| Thin data | skip if either team has under 6 matches in the training window |
| Stake | flat $10 |
| Model probability | the one the app shows (team news included); the one without is stored too |
| Live entry | first build where the pick qualifies, odds under 3 hours old, before kickoff |
| Backtest entry | first qualifying look, 48 and 3 hours before kickoff |
| Close | last DraftKings price before kickoff in the odds log (for closing line value); its minutes before kickoff are stored, since scheduled builds can be late |
| Settlement | 90-minute result from football-data; void if kickoff moves 48h+ or no result in 14 days |

**Live paper trades** (`paper.py`): after each build, `soccer-stats paper` updates an
append-only ledger at `paper_trades/E0_<season>.jsonl` on the `data-log` branch. A trade's
entry (time, price, probability, edge) is written once and never edited; later lines only
add the closing price and the settlement. Rebuilding adds nothing new. If the ledger or
the odds are unavailable, the site still publishes, opens nothing, and the tab says why.

**Odds log.** Each publish run appends the DraftKings prices it downloaded (no extra API
calls) to `odds_log/E0_<YYYY-MM>.jsonl` on the `data-log` branch: one row per fixture and
market, with each outcome's price and margin-free chance, when DraftKings quoted it, and
the model's and the blend's chances at that moment. Rows are never rewritten and never
duplicated. Live trades take their close from it (`close_odds`, `close_minutes_before`,
`clv_dk`, `beat_close_dk`), and the Portfolio summary counts closes quoted over an hour
before kickoff (`close_over_60min`). Read it with `odds_log.load`.

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

**The blend** (`match_calibration.py`). The raw model's big edges are mostly its own
errors, so the edge can instead use a blend of the model and DraftKings' margin-free
(Shin) price: `score_k = a_k + b·log(price_k) + c·log(model_k)`, softmax over home/draw/
away, and the same with two outcomes (a logistic blend) for over/under 2.5. It is fitted
on football-data's Pinnacle closing odds beside the model's walk-forward chances
(`--blend-seasons`, 3 by default, before the first DraftKings season) and refitted every
28 days on matches played before the refit date only. `backtest-dk` reports both
strategies (`raw` and `blend`) with the whole sweep, the bootstrap ROI range, CLV,
average chance vs win rate and log loss, and saves a fit on every settled match under
`blend.live` in `backtest/E0_dk.json`. The publish workflow reads that file: each fixture
then gets `p_bet` (the blend), which the app's value pick, its edge column and the paper
trades use; without the file everything falls back to the model's own chance. To rerun
the backtest from cached odds only (no key, no download, no push), dispatch *Backfill
DraftKings odds* with `print_only` ticked.

**Other match markets (research only).** `soccer-stats match-markets` (or *Backfill
DraftKings odds* with `match_markets` ticked: no key, no push) replays the model and the
blend on 1X2, over/under 2.5 and Asian handicap against Pinnacle's prices in
football-data's files. It reports log loss beside the market, the weight the model earns
in the blend, extra signals (xG form, soft-vs-sharp prices) and the threshold sweep with
ROI ranges and CLV against Pinnacle's fair close. Results in `docs/edge.md`: the model
adds nothing to Pinnacle's price in any of these markets, so the live rule stays on
DraftKings 1X2 and O/U 2.5 through the blend.

**Reading the Portfolio tab:** switch between *Live paper* and *Backtest*; they are
never mixed. Look at the trade count first: at 12% there are only a few trades a round,
and ROI on a small sample is mostly noise. Trust closing line value over ROI: beating
the close on average (especially Pinnacle's) is the best sign of a real edge. If ROI
falls as the sweep's threshold rises, big claimed edges are mostly the model's mistakes,
and the next step is blending with the market, not more bets. Tap any trade for every
stored field.

## Player bets: shots and shots on target

A multi-factor model prices each player's shots and shots on target, and player bets go
through the same rule, ledger and Portfolio tab as match bets.

- **Data** (`player_data.py`): Understat's per-match files give every shot (player,
  result) and both rosters (minutes, starter or sub). Shots on target = goals + saved
  shots. One cached file per match; builds download at most 40 new ones each. Player
  names are matched across Understat, FPL and The Odds API within a match's teams (exact
  name first, then a unique surname); anything ambiguous is skipped and counted, never
  guessed. Fixes go in `src/soccer_stats/player_names.csv`.
- **Factors** (`factors.py`, one list for future markets): his shots per 90 (recent
  matches weighted, shrunk to his position), position, penalty duty; team shots per
  match, team expected goals from the match model, absent team-mates' share of shots;
  shots the opponent concedes, overall and to his position; home/away, expected game
  state, days of rest. Every feature uses only earlier kickoffs.
- **Model** (`models/player_counts.py`): a regularised negative binomial for shots, with
  minutes ÷ 90 as exposure; shots on target either as a share of his shots or its own
  count model, whichever tests better. Prices assume he plays (bets on non-players are
  void), mixing "starts" and "comes on" by his chance of starting.
- **Testing** (`soccer-stats backtest-players`, or the *Player model* workflow every
  Monday): walk-forward by week from 2023/24 to the season in progress (`--seasons
  2023-now`; a new season joins once it has played matches). Stage 1 needs no odds: the chance of over
  0.5, 1.5 and 2.5 is scored against each player's season average, before lineups and
  with the lineup known, plus an ablation of each factor group (groups that don't help
  are dropped) and a check that players' expected shots add up to the team's. **Player
  odds and player paper trades switch on only when the model beats the baseline for
  both shots and shots on target.** Stage 2 (`soccer-stats backfill-player-odds`, about
  20 credits per match per snapshot) replays three strategies on historical FanDuel
  player lines (DraftKings has no EPL player props on The Odds API), with a walk-forward
  blend of the model and FanDuel's price (`player_calibration.py`). Every strategy and
  threshold loses money (−17% to −35% at 12%); `soccer-stats player-segments` checks
  slices out of sample, and none survives.
- **Rule for player bets**: 12% edge on the blended chance, $10; per player, match and
  market, the line and side with the highest edge; at most 4 player trades per match;
  void if he doesn't play. **Live player paper trades are switched off**
  (`PLAYER_PAPER_TRADES = False` in `trades.py`) until some player rule backtests
  positive.
- **In the app**: player lines in each match's sheet with the raw model chance, the blended
  chance, FanDuel's implied chance and the edge, and a note that player paper trades are off; a
  *Player picks* list on the Matches tab, and an All / Match / Player filter on the
  Portfolio tab. Live player lines refresh per match within 30 hours of kickoff (hourly in
  the last 3 hours) and keep 3,000 credits spare for match odds.

Settlement data: Understat's player shot counts match ESPN's (Opta-fed) on 99.8% of a
617 player-match sample (`docs/edge.md`), so the losses are the bookmaker's margin, not a
counting mismatch.

**Edge research** (`src/soccer_stats/edge/`, findings in `docs/edge.md`): line shopping
across books, a probe for two-sided player-prop books (none exist on The Odds API for EPL
shots), FanDuel slices, shot-count checks and a test of free signals (xG form, rest, soft
books vs Pinnacle) against the closing-line move. Run through the *Player odds check*
workflow's `task` input. None of it found a bettable edge; football-data's average and
maximum Asian handicap prices are often stale, so only Pinnacle's AH prices are used.

**One-time setup**
1. On GitHub: repo **Settings → Pages → Build and deployment → Source: GitHub Actions**.
2. **Actions** tab → *Publish app* → **Run workflow** (or wait for the next scheduled run).
3. On your iPhone, open `https://phat-d0.github.io/Soccer_Stats/` in **Safari**, tap
   **Share → Add to Home Screen**.

Build it locally: `uv run soccer-stats publish --out _site && python -m http.server -d _site`
(add `uv run soccer-stats paper --site _site --log-dir <folder>` to try the ledger).
The app code lives in `web/`; `src/soccer_stats/publish.py` writes the `data.json` it reads.

Test the app without the network: `node tests/web/smoke.mjs --shots /tmp/shots` serves
`web/` with the sample data in `tests/fixtures/web/`, clicks through every tab in Chromium
(phone light, phone dark and laptop) and fails on console errors or sideways scrolling.
It needs Playwright and Chromium; `uv run pytest` runs it too, and skips it without them
(as in CI, `.github/workflows/ci.yml`, which runs ruff and pytest on every push).
Refresh the sample data with `uv run python tests/web/make_fixture.py`.

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
  match_calibration.py  walk-forward blend of the match model with the bookmaker's price
  match_markets.py      research: 1X2, O/U 2.5 and Asian handicap vs Pinnacle, signal tests
  cli.py                `soccer-stats` command
  dashboard.py          data shaping for the app (fixtures, ratings, track record)
  xg.py                 Understat xG download, team-name mapping, merge onto matches
  publish.py            builds the phone app's data.json
  players.py            FPL team news -> attack/defence adjustments, injury history log
  odds_feed.py          DraftKings odds from The Odds API (cached, credit-aware)
  trades.py             the trade rule, settlement and metrics (no network or files)
  paper.py              live paper-trade ledger on the data-log branch
  odds_history.py       historical DraftKings snapshots: plan, budget, cache
  player_data.py        Understat shots/minutes per player per match, name matching
  factors.py            the player factor list (prior-only features)
  models/player_counts.py  negative binomial shot models, start/sub mixture
  player_backtest.py    walk-forward player tests, ablation, priced backtest
  player_odds.py        FanDuel player shot lines (live budgeted, historical)
  player_live.py        player lines for upcoming fixtures (raw model and blend)
  player_calibration.py walk-forward blend of the player model with FanDuel's price
  player_segments.py    out-of-sample segment search on the priced player lines
  edge/                 edge research: line shopping, prop books, shot-count checks
app/streamlit_app.py    the Premier League dashboard (desktop)
web/                    the iPhone web app (HTML/CSS/JS, service worker, icons)
.github/workflows/      CI, scheduled build + deploy to GitHub Pages, player model, by-hand backfills
docs/edge.md            edge research log
tests/                  unit tests on synthetic leagues with known parameters
tests/web/              app smoke test (Playwright) and its fixture generator
notebooks/              exploration
```

## Ideas to try next

- Tune `xi` (time decay) and the lookback window by out-of-sample log loss
- Elo / pi-ratings as a second model; blend with Dixon-Coles
- Non-penalty xG, and separate weights for attack vs defence
- Over/under and Asian handicap: tested in round 2 against Pinnacle; no edge (`docs/edge.md`)
- Market-blending: model probability shrunk toward the market, bet only on large disagreements
- Lower leagues, where bookmaker prices are less efficient
