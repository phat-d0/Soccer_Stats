# CLAUDE.md

Premier League betting model and phone app. The model is Dixon-Coles plus xG for
match bets, and a factor model for player shot bets. A phone and laptop web app
(PWA) is published at https://phat-d0.github.io/Soccer_Stats/. The owner is Phat
(GitHub phat-d0); he works from an iPhone and from Cursor on a laptop.

The work plan and scope live in the Docs artifact "Soccer Stats App: Backtest and
Portfolio Scope" (https://claude.ai/code/artifact/ff86c3e7-f90b-47a9-8474-28c7c7266e1d).
Read its "Status and next steps" section first; this file is the technical map.

## Branches and where things run

- `claude/soccer-stats-scaffold`: the default branch. All code is here.
- `data-log`: written only by workflows. Holds:
  - the paper-trade ledger (`trades/*.jsonl`, append-only);
  - team-news history (`fpl_news/`);
  - the live DraftKings odds log (`odds_log/E0_<YYYY-MM>.jsonl`, append-only);
  - backtest results (`backtest/E0_dk.json`, `backtest/E0_players.json`, `backtest/E0_players_detail.json`, `backtest/E0_goals.json`, `backtest/E0_player_trades.csv`, `backtest/E0_player_lines.csv.gz`).
  Read it with `git fetch origin data-log && git show origin/data-log:<path>`.
- GitHub Pages serves the app. Source = GitHub Actions. The repo must stay public, or Pages goes 404.
- Nothing runs locally in a cloud session:
  - the sandbox can't reach Understat, FPL, The Odds API, ESPN or github.io;
  - every data job runs in GitHub Actions;
  - trigger jobs with the GitHub MCP `actions_run_trigger`, and read logs with `get_job_logs`.

## Workflows (`.github/workflows`)

| File | What | Credits |
| --- | --- | --- |
| `publish.yml` | Builds the site (`soccer-stats publish`), then on `data-log` logs team news (`log-news`), the build's DraftKings prices (`log-odds`) and paper trades (`paper`), and deploys Pages. Only the default branch publishes: pushes elsewhere don't trigger it, and a dispatch on another branch skips `data-log`. Hourly cron plus every 15 min 10:00–22:00 UTC, but GitHub throttles scheduled runs; dispatch by hand to refresh now. | ~2 per match-odds refresh, plus live player lines near kickoff |
| `players.yml` | Player model: stage-1 test, then the priced backtest on cached FanDuel lines. Writes `backtest/E0_players*.json` and the anytime-goalscorer stage 1 (`backtest/E0_goals.json`) to `data-log`. Weekly (Mon) plus dispatch. Inputs: `seasons` (default `2023-now`: `now` = the season in progress, left out until it has played matches), `odds_seasons` (blank = download nothing), `max_credits`, `dry_run`. | 0 unless `odds_seasons` is set |
| `backfill.yml` | Historical DraftKings match odds, then `backtest-dk`, which writes `backtest/E0_dk.json` (incl. the live match blend `blend.live`) to `data-log`. Inputs: `seasons`, `max_credits` (0 = download nothing; the backtest still runs on cached odds and saves), `keep_credits`, `dry_run` (true = no backtest, no push), `print_only`: run `backtest-dk` from the cached odds and only print (no key, no download, no push), `match_markets` (+ `markets_seasons`): run `match-markets` on football-data only (no key, no push; log + 7-day artifact). | ~20 per snapshot; 0 with `print_only` |
| `odds-check.yml` | Diagnostics, edge research and the research lab. Inputs: `task` (coverage, props, match, shots, signals, lab, lab-holdout, goalscorer, player-lab), `cap` (props / goalscorer credit cap, 0 = dry run; the key is passed only to coverage, props and goalscorer), `hist_dates`, `seasons`; `league` (lab, lab-holdout: E0–E3); for `lab-holdout`: `reason`, `finalists`, `stack_base`. Never pushes. | 0 (match, shots, signals, lab, player-lab) to ~100 per historical props call; goalscorer ≤ `cap` (5 per live call, 10 per pilot call) |
| `ci.yml` | On every push and pull request. Job `test`: `uv sync --frozen`, ruff format check, ruff check, pytest, `node --check` on the app. Job `web`: installs Playwright's Chromium (cached) and runs `tests/web/smoke.mjs`; a skip counts as a failure, and screenshots are uploaded when it fails. No secrets. | 0 |

- After `players.yml` or `backfill.yml`, dispatch `publish.yml`, so the app picks up the new results.
- Caches:
  - `raw-data-*` holds the Understat match files;
  - `odds-history-*` and `player-odds-history-*` hold the paid historical odds;
  - never delete these caches: refilling them costs credits.

## The Odds API

- The repo secret is `ODDS_API_KEY`, on the 100K-credit plan.
- **The key is shared with Phat's baseball app**, so the balance can drop without any run here.
- Read the balance from the publish log ("credits left") or the backfill log.
- Never print the key, or write it into logs, data, the ledger or commits.
- Historical event odds cost about 20 credits per call (2 markets). Empty snapshots cost less.
- Match bets use DraftKings (h2h, totals).
- **Player shot lines use FanDuel**: DraftKings has no EPL player props on this API.
  - FanDuel lists **over sides only**, as whole numbers meaning "at least X" (1.0 = 1+ shots).
  - Code treats an over as winning at `count >= ceil(line)`.
  - Implied chance = 1 / odds, so it includes the margin.
- Cached FanDuel history covers 2023/24 (sparse), 2024/25 and 2025/26.

## Code map (`src/soccer_stats`)

**Match model**
- `models/dixon_coles.py`: the match model.
- `backtest.py`: walk-forward and the DraftKings backtest (`dk_trades`).
  - `STRATEGIES`: `raw` (model) and `blend`; `add_blend` adds `pb_*`, `dk_strategies` runs the sweep with bootstrap ranges; `dk_log_loss` reports model, blend and DraftKings.
- `match_calibration.py`: conditional-logit blend `score_k = a_k + b·log(price_k) + c·log(model_k)` (1X2; two outcomes = logistic for O/U 2.5).
  - Fitted on Pinnacle closing odds (football-data) beside the model's walk-forward chances; refitted every 28 days on earlier matches only.
  - The live fit is saved in `E0_dk.json` → `blend.live.{h2h,totals}.coef`; `publish.add_match_blend` reads it (`DK_BACKTEST_FILE`) and sets each fixture's `p_bet`, which `paper.update_ledger` and the app's `bestPick` use. No file = raw model, as before.
  - `fit`/`apply`/`walk_forward` also take row weights (AH pushes) and extra signal columns (`d` per feature: +home/−away for 1X2, a logit shift with two outcomes). Without them the coefficients are `[a, b, c]` as before. `RESEARCH_GROUPS` (`ah`) is research only; `GROUPS` (live) is unchanged.
- `match_markets.py`: research, not live. 1X2, O/U 2.5 and Asian handicap vs Pinnacle on football-data (`soccer-stats match-markets`, `backfill.yml` `match_markets`).
  - AH settlement (quarter lines split; break-even chance from the score matrix);
  - `clean_prices` blanks broken average/maximum rows;
  - `xg_features` (6-match xG form, earlier matches only) and `soft_vs_sharp` are blend signals;
  - `run` reports log loss, the blend weight, signal tests and the threshold sweep at Pinnacle early/close and the average/maximum early, with ROI ranges and CLV vs Pinnacle's fair close.
- `odds_feed.py`: live DraftKings odds and the credit budget.
- `odds_log.py`: the live DraftKings price log on `data-log` (`odds_log/E0_<YYYY-MM>.jsonl`), written by `soccer-stats log-odds` from the built `data.json` (no API calls).
  - One row per fixture × market (h2h, totals 2.5): prices, margin-free `fair`, `bookmaker`, `fetched_at` (DraftKings' `last_update`, else the download time; `time_source` says which), `downloaded_at`, `logged_at`, and the model's `p` and `p_bet` then.
  - Append-only and deduplicated on fixture, market, prices and `fetched_at`; quotes at or after kickoff are skipped.
  - `load` reads it; `last_before(log, home, away, kickoff, market)` gives the last quote before kickoff (the live close).
- `odds_history.py`: historical match odds.

**Trades and portfolio**
- `trades.py`: the one trade rule.
  - Match bets: `select_trades`, 12% edge, $10, one per match.
  - Player bets: `player_picks`, best line per player and market, max 4 per match; a line with no chance (`p` None) never trades.
  - `PLAYER_PAPER_TRADES = False`: the owner's switch (6 Oct). No live player paper trades open until a player rule backtests positive; open ones still settle. Published as `players_status.paper_trades`; the app says player trades are off.
  - Also settlement, summaries, `report`. Keep it free of network code.
- `paper.py`: live paper ledger (append-only "open" plus "update" events) and the `portfolio` section of `data.json`. Match trades use the fixture's `p_bet` (the blend) when set, else `p`. It merges the priced player backtest trades into the backtest view.
- Portfolios, one per strategy: `trades.PORTFOLIOS` is the registry (`id`, `name`, `status` live/testing/retired, `note`, `backtest` = the stem of `backtest/<league>_<stem>.json`). Now: `moneyline` (live, `dk`), `goalscorer` (testing, `goalscorer`), `player_shots` (retired, `players`). Adding a strategy is one entry.
  - `trades.portfolio_of(trade)`: the trade's `portfolio` field (new trades get it in `new_trade`; it is an entry field), else by market (`PORTFOLIO_MARKETS`: player shots → `player_shots`, `player_goal_scorer_anytime` → `goalscorer`), else match → `moneyline`.
  - `paper.portfolios_section` → `data.json` `portfolio.portfolios`: per entry `id, name, status, note`, a `live` section (that portfolio's ledger trades through `portfolio_section`, plus the ledger's `error`/`note`) and a `backtest` (its file as-is when it has a `summary`, like `E0_dk.json`; else `portfolio_section` of its trades plus `generated_at`/`seasons`, like `E0_players.json`; None without a file).
  - The old top-level `portfolio.live`, `portfolio.backtest` stay for one release; `portfolio.player_model` stays (Record → Player shots reads it). The app reads `portfolios`.
  - Live match closes come from the odds log: the last logged price before kickoff sets `close_odds`, `close_prices`, `close_fetched_at`, `close_minutes_before`, `clv_dk` and `beat_close_dk` (update events; entry fields never change). Without a log, the price each build sees until kickoff.
  - `trades.summarize` adds `close_early`: settled trades whose close was quoted over 60 minutes before kickoff (scheduled runs are throttled).

**Player bets**
- `player_data.py`: Understat per-match shots, name matching (exact name wins; ambiguous names are skipped and counted), season stats, active players (FPL status "u" = left).
- `factors.py`: player features, built from earlier matches only.
- `models/player_counts.py`: NB2 shots/on-target model, start/sub mixture, `prob_over`, `over_min`.
- `player_backtest.py`: stage-1 walk-forward, scoring, ablation, and `priced_trades`.
  - `priced_trades` replays three strategies (`STRATEGIES`), sweeping edge thresholds for each.
  - `MAIN_STRATEGY = "blend_lineup"`: confirmed starters, at the last price before kickoff, using the blended chance.
  - Every strategy keeps its bets at the 12% threshold in `E0_players.json` → `priced.strategies.<name>.trades`: compact rows (~100 bytes each), columns in `priced.trade_fields` (`date, home, away, player, market, line, side, odds, p, implied, edge, actual, status, profit, clv`), oldest first, voids included. The full main-strategy trades stay in the top-level `trades`.
  - `score_by_season`: stage-1 log loss per season (`E0_players.json` → `by_season`), so a new season's first weeks show on their own.
- `cli backtest-players`: `--seasons 2023-now` by default; the in-progress season joins once football-data and Understat both have a played match (`_season_not_ready`).
- `player_calibration.py`: logistic blend `logit p = a + b·logit(implied) + c·logit(model)`.
  - Refitted every 28 days on earlier lines (walk-forward).
  - The live fit coefficients are saved in `E0_players.json` → `priced.calibration.{look,close}.coef`.
- `player_odds.py`: FanDuel live lines (`fetch_live`, keeps 3,000 credits in reserve) and the historical backfill (cached per event; files `{event_id}_{look|close}_fanduel.json`).
- `player_live.py`: player lines for upcoming fixtures in the app.
  - Candidates are each team's players from its last 5 matches, corrected for transfers with `player_data.active_players` (`active=`): FPL at another club or status "u" drops a player; FPL at this club adds a signing with Premier League history. Players FPL doesn't match are kept.
  - A team with no Understat appearances yet (a promoted side before its first match) gets no rows; `players_status` reports `teams_without_history`, `moved_in`/`moved_out`, `unmatched_odds` and a sample of `unmatched_names` (add spellings to `player_names.csv`). The publish log prints them.
  - `publish.add_players` loads three seasons (`PLAYER_SEASONS`), so the model trains on up to two years as in the backtest even at a season's start; `players_stats.json` keeps the last two.
  - Each priced side has `p_model` (raw model, for display) and `p` (the blend, which sets `edge`, the app's player picks and, if the switch is on, the paper trades).
  - The blend coefficients come from `publish.player_gate()` → `calibration` (= `E0_players.json` → `priced.calibration.look.coef`).
  - No coefficients: `p` and `edge` are None, no player picks or trades, and `players_status.blend_note` says so.
- `models/player_goals.py` + `player_goals.py`: anytime goalscorer, P(scores ≥ 1 | plays).
  - NB/Poisson goals per 90 on `GOAL_FACTORS` (shrunk xG/90, shrunk goals/xG, shots/90, penalty share, position, team xG, opponent, venue, game state), all from earlier kickoffs (`goal_features`); start/sub mixture as for shots.
  - `walk_forward` (weekly refit, 2-year lookback, ≥3 earlier appearances) against two season-to-date benchmarks (goals and xG per appearance, Poisson). `report` scores development (2023/24–2024/25), the locked 2025/26 holdout and live 2026/27: log loss, Brier, calibration, match-resampled 95% ranges. Gate (reported only): beats both benchmarks on the holdout.
  - Run inside `backtest-players` (same appearances and match model), saved as `E0_goals.json`.
  - On the lab harness since round 5: `walk_forward` is `lab.harness.walk_forward` with `GoalCandidate` (fits when its block arrives, on the 730 days before the block; identical predictions to the old loop) and the lab's `Holdout`. `stage1_holdout()` opens it with a logged reason for the pre-registered weekly scoring (`holdout_log` in `E0_goals.json`); `stage1_holdout(None)` keeps it locked. `report` adds `lab` per split: `lab.metrics.evaluate` with the season-xG benchmark in the market's place (gain and blend weight; no prices, so no CLV or `passes`).
- `player_goal_odds.py`: the goalscorer odds probe and pilot (`soccer-stats goalscorer-pilot --cap N`, `odds-check.yml` `task=goalscorer`, never pushes).
  - Stage 0: live, all five regions, one market (≤5 credits a call; empty = 0). Pilot: the first cached 2025/26 FanDuel close in Aug/Oct/Dec/Feb/Apr, re-requested for `player_goal_scorer_anytime` with one `bookmakers=` list of ≤10 books (≤10 credits).
  - `CappedBudget`: a call is skipped if its maximum cost would pass the cap or leave under 3,000 credits on the shared key; the counted cost is `x-requests-last`. Bodies are cached as `{event_id}_close_goalscorer.json`; the job writes `goalscorer_prices.json` before the analysis.
  - `match_players` (names within the two clubs' whole-season squads), `evaluate` (starters at the best price: gap, back-all ROI, coverage, lift vs FanDuel, model vs price), `verdict` (docs/player_props.md §5 as written).
- `player_lab.py`: priced shot lines (`E0_player_lines.csv.gz`) through `lab.metrics`, development seasons only (`Holdout` 2025-07-01 stays locked; `soccer-stats player-lab`, `odds-check.yml` `task=player-lab`).
  - FanDuel's over-only 1/odds isn't margin-free, so the market's chance is `recalibrate`d walk-forward (logistic on logit(1/odds), fitted on earlier lines; same for the close, which sets CLV). The raw-price metrics are kept beside them as `raw_price_not_valid`: against a margin-laden price any calibrated model "adds information" and drift reads as CLV.
  - The lab's bet rule takes every line at a 12% edge (no best-line-per-player, no 4-per-match cap), so it makes more bets than the backtest; `backtest_dev_summary` gives the backtest's own development numbers beside them.
- `player_segments.py`: out-of-sample segment search on `E0_player_lines.csv.gz` (`soccer-stats player-segments --lines <path>`).
  - Scores 5,760 segments (strategy × market × line × position × venue × odds band × min blended edge) on one season, then reports the best ones unchanged on the other.
  - Bootstrap ranges resample whole matches. Each line is a 1-unit bet.

**Edge research**
- `edge/`: analysis helpers (line shopping on football-data books, two-sided prop probe,
  FanDuel slices, Understat vs ESPN shot counts), run via `python -m soccer_stats.edge.run`
  and `odds-check.yml` (`task` = props, match, shots, signals). Findings and ranked next experiments: `docs/edge.md`.
- `edge/signals.py`: seven pre-registered free signals (xG form, xG minus goals, rest, model vs early, soft books vs Pinnacle early, two totals signals; 6-match window, earlier matches only) tested against Pinnacle's early-to-close move and as a blend term beside Pinnacle early, season by season on earlier seasons, ranges Bonferroni-widened for 14 tests.
- **football-data's Asian handicap average and maximum (`AvgAH*`, `MaxAH*`) are unreliable**: often stale or at a different line. Pinnacle's AH prices are fine. Anything using the average/maximum AH columns must run `match_markets.clean_prices` or equivalent, and even then treat the results as suspect.
- **football-data's 2025/26 files have Pinnacle 1X2 prices for only part of the season**: about 52% of E0 matches, 47% / 30% / 30% of E1 / E2 / E3 (found in round 5). Anything fitted or scored on 2025/26 Pinnacle prices (the live match blend, the Record replay, any holdout) has a thinner sample than the match count suggests. Check price coverage before pre-registering a holdout.

**Research lab** (`lab/`, rules and results in `docs/lab.md`)
- `harness.py` (generic: rows with a time, group, outcome, features, market): `walk_forward` (refit every 28 days on earlier rows), `nested` (each season's settings chosen on the season before, trained on still earlier rows), `Holdout` (2025/26 is locked: predicting it raises `HoldoutLocked` unless `unlock(reason)` is called, which prints and logs the time).
- `metrics.py` (generic, any number of outcomes): log loss, Brier, calibration table, blend weight `c` beside the market with a range from whole-match bootstrap refits, `walk_forward_blend`, the live 12% rule's CLV and ROI with ranges, and `passes` (blend-weight range > 0 AND CLV range > 0).
- `features.py`: Elo, 6/20-match xG and goals for/against, rest; earlier matches only (`FEATURES_GOALS` for leagues without xG). `models.py`: the bake-off candidates (Dixon-Coles baseline, hierarchical Poisson, LightGBM, multinomial logit) and their tuning grids. `run.py`: the 1X2 bake-off (`python -m soccer_stats.lab.run --league E0|E1|E2|E3`, `odds-check.yml` `task=lab` with `league`; `lab-holdout` opens the holdout for named finalists). E1–E3 run goals-only, count as one multiple-testing family, and score only seasons where Pinnacle prices cover at least 90% of matches.
- Player markets can reuse `harness` and `metrics` as they are (two outcomes; NaN odds where a side isn't offered).

**Site and CLI**
- `publish.py`: builds `data.json`, `players_stats.json` and the rest of the site.
- `cli.py`: the `soccer-stats` commands: `publish`, `log-odds`, `paper`, `backtest-dk`, `backtest-players`, `backfill-odds`, `backfill-player-odds`, `player-odds-check`, `player-segments`, `match-markets`, `log-news`, `goalscorer-pilot`, `player-lab`.

**App (`web/`)**
- One vanilla JS file (`app.js`), plus `style.css`, `index.html`, `sw.js`.
- Bump the cache name in `sw.js` (`pl-model-vNN`) on every web change, or phones keep the old app.
- Tabs:
  - Matches;
  - Teams, with Players (season stats with list and deviation chart, model backtest);
  - Record, with Match bets (model replay vs Pinnacle) and Player shots (strategy switch, main strategy first; threshold sweep with one row per edge and one column per strategy; predicted vs actual win rate);
  - Portfolio: a switcher with one button per portfolio that isn't retired (Moneyline, Anytime goalscorer; remembered in localStorage `pfId`), a status pill and note, then Live paper / Backtest for that portfolio only. An empty portfolio shows its note. A "Retired:" link at the bottom opens retired ones (Player shots) read-only. Player trades are titled by the player's bet;
  - Explore.
- The value pick (`bestPick`) and the detail sheet's edge column use `fx.p_bet || fx.p`, as `trades.best_pick` in `paper.py`. With the blend live and no picks, the Matches note says the model doesn't beat the market.
- Minimum edge from history (no fixed edge buttons): each portfolio's backtest carries `edge_threshold` (research lab contract: `min_edge` float or null, `confidence`, `method`, `n_bets`, `seasons`, `note`, `by_bucket[{edge_lo, edge_hi, n, implied, model, realized, realized_lo, realized_hi}]`).
  - `edgeRule(pfId)`: an explored step if the owner picked one, else `min_edge`, else (no `edge_threshold`, older data) `PAPER_EDGE`. `matchEdge()` (Moneyline) drives `bestPick`, the Matches count, the match sheet and the Record replay; `playerEdge()` (Player shots) drives player picks. `null` flags nothing, and the Matches note and match sheet say why (`noEdgeText`).
  - `edgePanel(pfId)` (Matches bottom, Record → Match bets and Player shots): the recommendation in plain English plus "Explore other edges" (folded; `EDGE_STEPS` buttons set `state.exploreEdge`, not saved; "Back to the recommended level" resets).
  - `edgeBucketsHtml(pfId)` (Record, and each portfolio's Backtest): a dot plot per edge bucket of book's chance (tick), model's chance (ring) and won (dot plus range; blue `--pos` when it beat the book, red `--neg` when not), rows at or above `min_edge` shaded, tap a row for the readout line.
  - Paper trades still open at `trades.PAPER_EDGE` (12%); the app's threshold is display only.
- Match sheet "Markets": Model, Blend (`p_bet`, shaded), DK (margin-free), Odds, Edge. With the blend live and no pick, a "Why no value bet here?" box explains it from `match_blend.h2h` (model weight `c`, matches) and the margin.
- Match sheet "Player shots": per player a grid of Line, Odds, Model (`p_model`), Blend (`p`), FD (`implied`), Edge; a box above says player paper trades are off and shows `players_status.blend_note`.
- Record → Match bets opens with "Against DraftKings' prices": a Model alone / Blend switch over `portfolio.backtest.strategies` (E0_dk.json), tiles, the sweep (one column per strategy, odds-capped rows under their own label) and log loss (model, blend, DraftKings). The Pinnacle replay follows.
- Portfolio → Live: Settled profit, ROI, Closing line value and Beat the close are the first four tiles, with a note on why CLV is the faster sign of an edge. Settled match trades show the close (American + decimal), minutes before kickoff and CLV; a close taken more than 60 minutes before kickoff is marked approximate (`CLOSE_APPROX_MIN`). Fields (moneyline contract): per trade `close_odds`, `close_fetched_at`, `close_minutes_before` (else worked out from `close_fetched_at`), `clv_dk`, `beat_close_dk`; summary `clv_dk`, `beat_close_dk`, `close_early` (else counted from the trades). Missing fields show "–". Open trades label the price "Latest odds" and "CLV so far".
- The app reads `portfolio.portfolios` (`portfolios()`, `pfById`, `pfCurrent`); an older data.json without it shows as one Moneyline portfolio. Matches' open-trade badges use Moneyline's live trades; Record reads Moneyline's backtest (DraftKings strategies) and Player shots' backtest trades.
- Portfolio: breakdown tables sit in a folded `<details class="fold">`; implied-vs-realized shows when the portfolio has player trades; the match threshold sweep and the player model tables live on Record, not here. Settled trades page by 15.
- Local test data: `tests/fixtures/web/` (`data.json`, `players_stats.json`, `players_backtest.json`, under 1 MB).
  - Rebuild with `uv run python tests/web/make_fixture.py`: a synthetic league through `publish.build_data`, plus the real ledger and backtests from `origin/data-log` through `paper.run`. Fixtures carry `p_bet` (E0_dk.json `blend.live`, or a fallback fit like the real one) and blended player lines (E0_players.json coefficients). `now` is fixed, so the output is reproducible. `SYNTHETIC_EDGE` gives Moneyline an 8% `edge_threshold` and Player shots a null one (synthetic, until the research lab's backtests carry it). `SYNTHETIC_SETTLED` adds six settled live match trades with close fields (one close 95 min early, one with none) until the real ledger has them, then rebuilds `portfolios`. The fixture leaves out the old `portfolio.live`/`portfolio.backtest`, so the smoke test proves the app doesn't need them.
- Smoke test: `node tests/web/smoke.mjs [--shots DIR]`.
  - Serves `web/` with the fixture and visits every tab, sub-view and sheet at 390px light and dark, and at 1280px.
  - Fails on console errors, horizontal scroll, or "undefined", "NaN" or "${" in the text.
  - `tests/test_web_smoke.py` runs it under pytest, and skips without node or Chromium (`/opt/pw-browsers/chromium`, or set `CHROMIUM`).

## Agent team (`.claude/agents/`)

Five agents, each owning part of the code. Start a session's work by calling the relevant agent by name.

| Agent | Owns |
| --- | --- |
| `ui-designer` | `web/*` and the display fields in `publish.py`. Screenshot-tests at 390px, light and dark. |
| `player-props` | Every player market (shots, on target, anytime goalscorer, assists, cards): models, backtests, calibration, live player lines and new player ideas (`player_*.py`, `factors.py`, `models/player_counts.py`, `players.yml`). Formerly `player-shots`. |
| `moneyline` | The match model and match bets (`models/dixon_coles.py`, `backtest.py`, `odds*.py`, match parts of `trades.py` and `paper.py`, `backfill.yml`). |
| `research-lab` | The shared evaluation harness and pass rules (`src/soccer_stats/lab/`, `docs/lab.md`), the machine-learning model bake-off, and signal/market research (`src/soccer_stats/edge/`, `docs/edge.md`, `odds-check.yml`). Proposes; owners build. Formerly `edge-finder`. |
| `lead-reviewer` | Reviews and merges specialist branches, CI, CLAUDE.md, README, the work plan, and credit budgets. |

- Specialists work on their own branches: `team/<name>` (`team/research`, `team/player-props`, `team/moneyline`, `team/ui` from round 4; `team/edge` and `team/player-shots` in rounds 2–3). Round 1 (6 Oct) used `agent/<name>`; those branches are merged.
- From round 2 each specialist runs as a separate cloud session on its `team/<name>` branch, cut from the latest `claude/soccer-stats-scaffold`. It pushes only its branch; it never merges, and never pushes `data-log` by hand.
- When done, each specialist **opens a pull request** from `team/<name>` into `claude/soccer-stats-scaffold` (from round 4; rounds 1–3 were merged with git directly, without PRs).
- The lead runs in the main (coordinating) session, as in the owner's baseball team. It reviews each PR (diff, CI on the PR, full checks), comments, and merges it on GitHub with a merge commit, so each shows as Merged in the owner's app.
- Only the lead sets Odds API credit caps. The default cap is 0.

## Conventions

- Run checks before every push: `uv run ruff format src tests && uv run ruff check src tests && uv run pytest -q`. All must pass (CI runs the same). Use `.venv/bin/pytest` if `uv` isn't on the path. JS: `node --check web/app.js`, and after web changes `node tests/web/smoke.mjs` (regenerate the fixture first if `data.json`'s shape changed).
- No look-ahead anywhere: features, refits and calibration use only earlier kickoffs. Tests check this.
- Commit messages: imperative summary, a body explaining why, and the attribution lines the session gives.
- App text is plain English for a non-specialist. Money is in $ for portfolio views and in units for the match replay. Odds are American plus decimal.
- Charts follow the dataviz rules already used:
  - blue `--pos` and red `--neg` diverging colours;
  - a readout line instead of tooltips over the chart;
  - colour tokens defined for both light and dark themes.

## Status (2026-10-07, after round 5)

**Match bets**
- DraftKings backtest, 2025/26, raw model at a 12% edge: 173 bets, ROI −13% (95% range −38% to +16%), CLV vs DraftKings −5% to −7%. Every threshold from 2% to 20% loses (−8% to −21%).
- Blend (`match_calibration.py`, fitted on up to 2,276 earlier Pinnacle-priced matches): the fit gives the model about no weight (b ≈ 1.04, c ≈ −0.03). 1X2 log loss: model 1.030, blend 1.018, DraftKings close 1.012. Blend bets: 0 at every threshold (5 at 2% over 2023–2025, all lost).
- The cached DraftKings history has no over/under 2.5 prices, so totals are untested at DraftKings.
- Round 2 (`match_markets.py`, football-data 2017–2026, Pinnacle AH and O/U from 2019/20; details in `docs/edge.md`):
  - Log loss, model / blend / Pinnacle close: 1X2 0.9614 / 0.9504 / 0.9489; O/U 2.5 0.6794 / 0.6770 / 0.6755; AH 0.7066 / 0.6949 / 0.6939. The model never beats the close.
  - Model weight in the blend (live fit): 1X2 0.06, O/U 0.11, AH 0.12 (AH shrinks Pinnacle's price, b ≈ 0.5).
  - Model picks at Pinnacle early, 12%: CLV −4.2% (1X2, 1,419 bets), −2.9% (O/U, 285), −2.0% (AH, 744), worse than a random side. AH blend: CLV −0.2% to +0.8% at 2–8%, no range above 0.
  - xG form, xG-minus-goals and soft-vs-sharp earn no out-of-sample log loss (all ranges include 0). Late team news can't be tested yet (FPL log starts 5 Oct 2026).
  - Verdict: no AH or O/U bets in the live rule; nothing in the live code changed.
  - The AH rows at football-data's average and maximum prices (avg −3.2%, max blend +1.2% to +2.6%) are unreliable (stale quotes; see the data warning above). The Pinnacle-only AH results stand.
- Free signals (edge finder, `edge/signals.py`, 2017–2026, 7 signals × 2 tests, Bonferroni 99.64% ranges): none earns blend weight beside Pinnacle early (best gain +0.0002, range −0.0017 to +0.0019). Soft-vs-sharp and xG form predict the early-to-close move a little (R² 0.1–1.6%), but betting them at Pinnacle early gives CLV −1.6% to −3.7%, never positive.
- Line shopping (edge finder, football-data 2017–2026, ~1,700–2,100 bets): the rule's CLV vs Pinnacle's fair close is −6.6% at the average book, −4.0% at Pinnacle early, −2.3% at the best of seven named books. Only the unbettable market maximum is positive, and it turned negative in the last two seasons. The picks do no better than random against the sharp close.
- Round 3 (moneyline + UI): every publish run appends the DraftKings quotes it already downloaded to `odds_log/E0_<YYYY-MM>.jsonl` on `data-log` (0 extra credits; 20 rows per refresh, 10 fixtures × h2h/totals; a refresh whose prices are unchanged but whose DraftKings `last_update` moved logs new rows, by design). Live match trades now take their close from the last logged quote before kickoff, with margin-free CLV vs DraftKings, `beat_close_dk` and `close_minutes_before`; `close_early` counts closes quoted over 60 minutes out (scheduled runs are throttled). No live trade has settled yet, so there is no live CLV figure. The log is the data for testing late team news against price moves once 2026/27 has a few more weeks.
- Round 4 (research lab, `lab/`, `docs/lab.md`): a pre-registered 1X2 bake-off against Pinnacle's early price. Development is 2017/18–2024/25, 2,934 matches, 99.5% ranges; the holdout 2025/26 was opened once (2026-10-06 22:53 UTC) with 198 priced matches. Nothing passes. Log loss: Dixon-Coles 0.9589, hierarchical Poisson 0.9618, LightGBM 0.9749, multinomial logit 0.9653, stack with the price 0.9511, Pinnacle early 0.9493. No blend-weight range clears 0. CLV −3.6% to −5.0% (holdout −3.6% and −5.1%). No live change. Next: the same bake-off on E1–E3 (free).
- Round 4 (UI, PR #3): the Portfolio tab is one portfolio per strategy (`trades.PORTFOLIOS`: Moneyline live, Anytime goalscorer testing, Player shots retired). The old `portfolio.live` / `portfolio.backtest` / `player_model` keys stay: the app still reads `player_model` (Record → Player shots) and falls back to `live`/`backtest` for an older data.json, so removing them is a separate change.
- Round 5 (research lab, bake-off 2, `docs/lab.md`): the same pre-registered bake-off on E1–E3. These are goals-only (no Understat xG); development is 2017/18–2024/25, about 4,000–4,200 matches per league, at 99.83% ranges (30 tests). Nothing passes in any league: every model trails Pinnacle early by 0.016–0.021 log loss, no blend-weight range clears 0, and CLV is −3.8% to −4.9%. The 2025/26 holdouts were opened once but couldn't be scored: football-data has Pinnacle prices for only 47% / 30% / 30% of 2025/26 (E0 about 52%). That gap also thins any 2025/26 fit on Pinnacle prices (the live blend). Verdict: stop 1X2 model work against Pinnacle; next is team news vs the DraftKings log.
- Round 5 (lead): PRs #5 (research lab) and #6 (player props) merged through the GitHub API; 220 tests pass; `players.yml` run 37577615109 (0 credits) reproduced the goalscorer stage-1 numbers exactly on the lab harness (holdout opening logged), and publish run 37578089543 succeeded.
- Live: value picks and match paper trades use the blend (`p_bet`) once `E0_dk.json` holds `blend.live`, so they will mostly stop. That is the honest result.

**Player bets**
- Stage 1 passes: shots log loss 0.420 vs 0.496 baseline (0.400 lineup known). Starter flag fixed 6 Oct (11 starters in all 3,040 team-matches).
- Priced backtest on FanDuel 2023/24–2025/26 at a 12% edge:

  | Strategy | Bets | ROI |
  | --- | --- | --- |
  | Starters after lineups (blend) | 153 | −17% |
  | Blend, 3 hours before | 223 | −35% |
  | Raw model, 3 hours before | 628 | −22% |

  Every threshold from 2% to 20% loses. The blend is well calibrated (23.3% predicted vs 23.1% won among starters); FanDuel's over-only lines imply 33%, so their margin is the obstacle.
- Segment search (`player-segments`): 5,760 segments picked on one season, reported on the other. 59 profitable in 2024/25; none of them profitable in 2025/26. With ≥300 bets, none profitable in either season.
- No Odds API book prices EPL player shots on both sides (all five regions; 1xBet and the Kambi books are over-only too). No FanDuel slice is close to fair: the best (odds ≤ 1.25 at the close) is −13.8%, −16.6% in the holdout season.
- Understat's shot counts match ESPN's (99.8% of 617 player-matches identical), so the losses are margin, not a data mismatch.
- Live: player lines show with the blended chance (3-hour "look" coefficients); `PLAYER_PAPER_TRADES = False`, so no player paper trades open.
- Round 2 (`players.yml` run 37528001150, `--seasons 2023-now` = 2023/24–2026/27): stage 1 beats the baseline in every season (shots log loss 0.426 / 0.414 / 0.419 vs 0.507 / 0.491 / 0.491 for 2023/24–2025/26; 2026/27 so far 0.452 vs 0.741 on 1,293 appearances, where the season-average baseline is still thin). Priced, 12% edge: starters after lineups 154 bets −18% (SE ±32 points), blend 3 h before 223 −35%, raw model 3 h before 635 −22%. Every strategy's bets are now in `E0_players.json` (`priced.strategies.<name>.trades`).
- Round 4 (player props, PR #1; `players.yml` run 37545935184, 0 credits): anytime goalscorer stage 1, P(scores | plays), walk-forward. Log loss before lineups, model / season-goals / season-xG benchmark: development 2023/24–2024/25 (21,878 appearances) 0.266 / 0.399 / 0.291; holdout 2025/26 (11,055, 380 matches) 0.247 / 0.380 / 0.268, gain vs xG benchmark −0.022 (95% −0.029 to −0.015); 2026/27 so far (1,293) 0.253 / 0.510 / 0.313. Holdout calibration within about 2 points per bucket (worst 30–45%: 34.4% predicted vs 32.1% scored, n=140). The gate (beats both benchmarks on the holdout) passes. That is a free-data result only: no goalscorer odds exist yet. `docs/player_props.md` costs the next step (no API calls made): a live probe (~10 credits) + 5-match pilot (50) ≈ 60 credits, a 100-match sample 1,000, a full season 3,800 (close only); its go/kill rules are fixed in advance. The goalscorer holdout is scored on every weekly run, so any change to its spec must be pre-registered and run through `lab/`. Player paper trades stay off.
- Round 5 (player props; owner-approved 60-credit cap; `odds-check.yml` runs 37574303057 and 37573679173):
  - Goalscorer odds: 14 books price EPL anytime goalscorer live (espnbet, bovada, fanatics, mybookieag, rebet, Caesars, BetMGM, the Kambi books, TAB, PointsBet AU), 27–46 players a match. **None lists a "No" price**, so no margin can be measured and there is no fair price.
  - Pilot (5 cached 2025/26 closes; BetRivers 5 matches, Bovada 3, ESPN BET 2, MyBookie 1; FanDuel not asked, the list filled with live books): 141 of 169 names matched (83%; misses are mostly full legal names). On 83 starters, the best price's implied chance is 16.4% vs 10.8% scored, a gap of 5.6 points (95% 2.3 to 8.8); back-all ROI −49%. The pre-registered rule says **kill** (gap above 5). It was written for the 100-match sample, and on 5 matches the range still includes ≤5, so the owner can still choose the sample; I don't recommend it.
  - Shots on the lab harness (development 2023/24–2024/25, holdout locked): against FanDuel's de-margined price the model earns real blend weight (c 0.75–0.90, ranges above 0) and better log loss, but CLV vs the de-margined close is −37% (the 12% edges are long shots at average odds ~15), so nothing passes. On raw over-only prices the 3-hour strategies would "pass" (CLV +3%): an artifact of the margin, kept in the output labelled as not valid. Consistent with data-log's losing backtest.
  - The goalscorer walk-forward runs through the lab harness with identical predictions; `trades.PORTFOLIOS` goalscorer note updated. Player paper trades stay off.
- Live lines correct squads for transfers (FPL); the 6 Oct publish showed 490 players, 0 priced (no FanDuel lines more than 30 hours before kickoff) and no teams without history.
- No live lineup feed. Build one (ESPN summary API, `rosters[].roster[].starter`) only if a strategy backtests positive.

**Credits**: 22,634 left on 7 Oct after round 5 (publish run 37578089543). Round 5 spent 60, the owner-approved cap, on the goalscorer probe and pilot (run 37574303057: 22,696 → 22,636); the bake-off on E1–E3 and the shots lab run were free. The key is shared, so check the publish log.

**App (rounds 3–4)**: Portfolio → Live leads with settled profit, ROI, CLV and beat-the-close tiles and a note on why CLV matters; settled match trades and the trade sheet show the close, how long before kickoff it was taken ("approx." past 60 min) and CLV. Round 2 added model / blend / DraftKings side by side on the match sheet, model / blend / FanDuel on player lines, and the DraftKings strategy switch on Record. Round 4 split Portfolio into one portfolio per strategy (Moneyline, Anytime goalscorer, Player shots) with a status note each. `sw.js` is `pl-model-v22`.

Ranked next steps are in the work plan's "Status and next steps" section and in `docs/edge.md`.
