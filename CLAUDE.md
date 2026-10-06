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
  - backtest results (`backtest/E0_dk.json`, `backtest/E0_players.json`, `backtest/E0_players_detail.json`, `backtest/E0_player_trades.csv`, `backtest/E0_player_lines.csv.gz`).
  Read it with `git fetch origin data-log && git show origin/data-log:<path>`.
- GitHub Pages serves the app. Source = GitHub Actions. The repo must stay public, or Pages goes 404.
- Nothing runs locally in a cloud session:
  - the sandbox can't reach Understat, FPL, The Odds API, ESPN or github.io;
  - every data job runs in GitHub Actions;
  - trigger jobs with the GitHub MCP `actions_run_trigger`, and read logs with `get_job_logs`.

## Workflows (`.github/workflows`)

| File | What | Credits |
| --- | --- | --- |
| `publish.yml` | Builds the site (`soccer-stats publish`), updates paper trades and team news on `data-log`, deploys Pages. Hourly cron plus every 15 min 10:00–22:00 UTC, but GitHub throttles scheduled runs; dispatch by hand to refresh now. | ~2 per match-odds refresh, plus live player lines near kickoff |
| `players.yml` | Player model: stage-1 test, then the priced backtest on cached FanDuel lines. Writes `backtest/E0_players*.json` to `data-log`. Weekly (Mon) plus dispatch. Inputs: `seasons` (default `2023-now`: `now` = the season in progress, left out until it has played matches), `odds_seasons` (blank = download nothing), `max_credits`, `dry_run`. | 0 unless `odds_seasons` is set |
| `backfill.yml` | Historical DraftKings match odds, then `backtest-dk`, which writes `backtest/E0_dk.json` (incl. the live match blend `blend.live`) to `data-log`. Inputs: `seasons`, `max_credits` (0 = download nothing; the backtest still runs on cached odds and saves), `keep_credits`, `dry_run` (true = no backtest, no push), `print_only`: run `backtest-dk` from the cached odds and only print (no key, no download, no push). | ~20 per snapshot; 0 with `print_only` |
| `odds-check.yml` | Diagnostics and edge research. Inputs: `task` (coverage, props, match, shots), `cap` (props credit cap, 0 = dry run), `hist_dates`, `seasons`. Never pushes. | 0 (match, shots) to ~100 per historical props call |
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
- `odds_feed.py`: live DraftKings odds and the credit budget.
- `odds_history.py`: historical match odds.

**Trades and portfolio**
- `trades.py`: the one trade rule.
  - Match bets: `select_trades`, 12% edge, $10, one per match.
  - Player bets: `player_picks`, best line per player and market, max 4 per match; a line with no chance (`p` None) never trades.
  - `PLAYER_PAPER_TRADES = False`: the owner's switch (6 Oct). No live player paper trades open until a player rule backtests positive; open ones still settle. Published as `players_status.paper_trades`; the app says player trades are off.
  - Also settlement, summaries, `report`. Keep it free of network code.
- `paper.py`: live paper ledger (append-only "open" plus "update" events) and the `portfolio` section of `data.json`. Match trades use the fixture's `p_bet` (the blend) when set, else `p`. It merges the priced player backtest trades into the backtest view.

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
  - Promoted teams without Premier League history (Hull, Coventry in 2026/27) get no rows; `players_status` reports `teams_without_history`, `moved_in`/`moved_out`, `unmatched_odds` and a sample of `unmatched_names` (add spellings to `player_names.csv`). The publish log prints them.
  - `publish.add_players` loads three seasons (`PLAYER_SEASONS`), so the model trains on up to two years as in the backtest even at a season's start; `players_stats.json` keeps the last two.
  - Each priced side has `p_model` (raw model, for display) and `p` (the blend, which sets `edge`, the app's player picks and, if the switch is on, the paper trades).
  - The blend coefficients come from `publish.player_gate()` → `calibration` (= `E0_players.json` → `priced.calibration.look.coef`).
  - No coefficients: `p` and `edge` are None, no player picks or trades, and `players_status.blend_note` says so.
- `player_segments.py`: out-of-sample segment search on `E0_player_lines.csv.gz` (`soccer-stats player-segments --lines <path>`).
  - Scores 5,760 segments (strategy × market × line × position × venue × odds band × min blended edge) on one season, then reports the best ones unchanged on the other.
  - Bootstrap ranges resample whole matches. Each line is a 1-unit bet.

**Edge research**
- `edge/`: analysis helpers (line shopping on football-data books, two-sided prop probe,
  FanDuel slices, Understat vs ESPN shot counts), run via `python -m soccer_stats.edge.run`
  and `odds-check.yml` (`task` = props, match, shots). Findings and ranked next experiments: `docs/edge.md`.

**Site and CLI**
- `publish.py`: builds `data.json`, `players_stats.json` and the rest of the site.
- `cli.py`: the `soccer-stats` commands: `publish`, `paper`, `backtest-dk`, `backtest-players`, `backfill-odds`, `backfill-player-odds`, `player-odds-check`, `player-segments`, `log-news`.

**App (`web/`)**
- One vanilla JS file (`app.js`), plus `style.css`, `index.html`, `sw.js`.
- Bump the cache name in `sw.js` (`pl-model-vNN`) on every web change, or phones keep the old app.
- Tabs:
  - Matches;
  - Teams, with Players (season stats with list and deviation chart, model backtest);
  - Record, with Match bets (model replay vs Pinnacle) and Player shots (strategy switch, main strategy first; threshold sweep with one row per edge and one column per strategy; predicted vs actual win rate);
  - Portfolio (live paper trades and the backtest, filter All/Match/Player; player trades are titled by the player's bet);
  - Explore.
- The value pick (`bestPick`) and the detail sheet's edge column use `fx.p_bet || fx.p`, as `trades.best_pick` in `paper.py`. With the blend live and no picks, the Matches note says the model doesn't beat the market.
- Local test data: `tests/fixtures/web/` (`data.json`, `players_stats.json`, `players_backtest.json`, under 1 MB).
  - Rebuild with `uv run python tests/web/make_fixture.py`: a synthetic league through `publish.build_data`, plus the real ledger and backtests from `origin/data-log` through `paper.run`. Fixtures carry `p_bet` (E0_dk.json `blend.live`, or a fallback fit like the real one) and blended player lines (E0_players.json coefficients). `now` is fixed, so the output is reproducible.
- Smoke test: `node tests/web/smoke.mjs [--shots DIR]`.
  - Serves `web/` with the fixture and visits every tab, sub-view and sheet at 390px light and dark, and at 1280px.
  - Fails on console errors, horizontal scroll, or "undefined", "NaN" or "${" in the text.
  - `tests/test_web_smoke.py` runs it under pytest, and skips without node or Chromium (`/opt/pw-browsers/chromium`, or set `CHROMIUM`).

## Agent team (`.claude/agents/`)

Five agents, each owning part of the code. Start a session's work by calling the relevant agent by name.

| Agent | Owns |
| --- | --- |
| `ui-designer` | `web/*` and the display fields in `publish.py`. Screenshot-tests at 390px, light and dark. |
| `player-shots` | The player model, backtest, calibration and live player lines (`player_*.py`, `factors.py`, `models/player_counts.py`, `players.yml`). |
| `moneyline` | The match model and match bets (`models/dixon_coles.py`, `backtest.py`, `odds*.py`, match parts of `trades.py` and `paper.py`, `backfill.yml`). |
| `edge-finder` | Research into where a real edge could exist (`src/soccer_stats/edge/`, `docs/edge.md`, `odds-check.yml`). Proposes; owners build. |
| `lead-reviewer` | Reviews and merges specialist branches, CI, CLAUDE.md, README, the work plan, and credit budgets. |

- Specialists work on their own branches: `team/<name>` (`team/edge`, `team/player-shots`, `team/moneyline`, `team/ui`) from round 2. Round 1 (6 Oct) used `agent/<name>`; those branches are merged.
- From round 2 each specialist runs as a separate cloud session on its `team/<name>` branch, cut from the latest `claude/soccer-stats-scaffold`. It pushes only its branch; it never merges, and never pushes `data-log` by hand.
- The lead runs in the main (coordinating) session, as in the owner's baseball team. It reviews each branch, runs the full checks and merges into `claude/soccer-stats-scaffold`.
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

## Status (2026-10-06, after round 1)

**Match bets**
- DraftKings backtest, 2025/26, raw model at a 12% edge: 173 bets, ROI −13% (95% range −38% to +16%), CLV vs DraftKings −5% to −7%. Every threshold from 2% to 20% loses (−8% to −21%).
- Blend (`match_calibration.py`, fitted on up to 2,276 earlier Pinnacle-priced matches): the fit gives the model about no weight (b ≈ 1.04, c ≈ −0.03). 1X2 log loss: model 1.030, blend 1.018, DraftKings close 1.012. Blend bets: 0 at every threshold (5 at 2% over 2023–2025, all lost).
- The cached DraftKings history has no over/under 2.5 prices, so totals are untested.
- Line shopping (edge finder, football-data 2017–2026, ~1,700–2,100 bets): the rule's CLV vs Pinnacle's fair close is −6.6% at the average book, −4.0% at Pinnacle early, −2.3% at the best of seven named books. Only the unbettable market maximum is positive, and it turned negative in the last two seasons. The picks do no better than random against the sharp close.
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
- Season 2026/27 (starts 10 Oct), round 2: live lines handle transfers and promoted teams (above); FanDuel's live fetch keeps 3,000 credits (an unknown balance allows one call, then the headers' balance holds the reserve); the weekly run adds 2026/27 once it has played matches. Nothing was re-run here (the sandbox can't reach the sources): the next `players.yml` run writes the per-strategy trades and `by_season`.
- No live lineup feed. Build one (ESPN summary API, `rosters[].roster[].starter`) only if a strategy backtests positive.

**Credits**: 22,732 left on 6 Oct after the edge round (220 spent). The key is shared, so check the publish log.

Ranked next steps are in the work plan's "Status and next steps" section and in `docs/edge.md`.
