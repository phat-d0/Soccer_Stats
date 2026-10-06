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
| `players.yml` | Player model: stage-1 test, then the priced backtest on cached FanDuel lines. Writes `backtest/E0_players*.json` to `data-log`. Weekly (Mon) plus dispatch. Inputs: `seasons` (e.g. `2023-2025`), `odds_seasons` (blank = download nothing), `max_credits`, `dry_run`. | 0 unless `odds_seasons` is set |
| `backfill.yml` | Historical DraftKings match odds for the match backtest. Input `print_only`: run `backtest-dk` from the cached odds and only print (no key, no download, no push). | ~20 per snapshot; 0 with `print_only` |
| `odds-check.yml` | Diagnostic: which bookmakers price EPL player props. | ~80 |

- After `players.yml`, dispatch `publish.yml`, so the app picks up the new results.
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
  - Player bets: `player_picks`, best line per player and market, max 4 per match.
  - Also settlement, summaries, `report`. Keep it free of network code.
- `paper.py`: live paper ledger (append-only "open" plus "update" events) and the `portfolio` section of `data.json`. It merges the priced player backtest trades into the backtest view.

**Player bets**
- `player_data.py`: Understat per-match shots, name matching (exact name wins; ambiguous names are skipped and counted), season stats, active players (FPL status "u" = left).
- `factors.py`: player features, built from earlier matches only.
- `models/player_counts.py`: NB2 shots/on-target model, start/sub mixture, `prob_over`, `over_min`.
- `player_backtest.py`: stage-1 walk-forward, scoring, ablation, and `priced_trades`.
  - `priced_trades` replays three strategies (`STRATEGIES`), sweeping edge thresholds for each.
  - `MAIN_STRATEGY = "blend_lineup"`: confirmed starters, at the last price before kickoff, using the blended chance.
- `player_calibration.py`: logistic blend `logit p = a + b·logit(implied) + c·logit(model)`.
  - Refitted every 28 days on earlier lines (walk-forward).
  - The live fit coefficients are saved in `E0_players.json` → `priced.calibration.{look,close}.coef`.
- `player_odds.py`: FanDuel live lines (`fetch_live`, keeps 3,000 credits in reserve) and the historical backfill (cached per event; files `{event_id}_{look|close}_fanduel.json`).
- `player_live.py`: player lines for upcoming fixtures in the app.

**Site and CLI**
- `publish.py`: builds `data.json`, `players_stats.json` and the rest of the site.
- `cli.py`: the `soccer-stats` commands: `publish`, `paper`, `backtest-dk`, `backtest-players`, `backfill-odds`, `backfill-player-odds`, `player-odds-check`, `log-news`.

**App (`web/`)**
- One vanilla JS file (`app.js`), plus `style.css`, `index.html`, `sw.js`.
- Bump the cache name in `sw.js` (`pl-model-vNN`) on every web change, or phones keep the old app.
- Tabs:
  - Matches;
  - Teams, with Players (season stats with list and deviation chart, model backtest);
  - Record, with Match bets (model replay vs Pinnacle) and Player shots (the FanDuel backtest strategies, threshold sweep, calibration table);
  - Portfolio (live paper trades and the backtest, filter All/Match/Player);
  - Explore.

## Agent team (`.claude/agents/`)

Five agents, each owning part of the code. Start a session's work by calling the relevant agent by name.

| Agent | Owns |
| --- | --- |
| `ui-designer` | `web/*` and the display fields in `publish.py`. Screenshot-tests at 390px, light and dark. |
| `player-shots` | The player model, backtest, calibration and live player lines (`player_*.py`, `factors.py`, `models/player_counts.py`, `players.yml`). |
| `moneyline` | The match model and match bets (`models/dixon_coles.py`, `backtest.py`, `odds*.py`, match parts of `trades.py` and `paper.py`, `backfill.yml`). |
| `edge-finder` | Research into where a real edge could exist (`src/soccer_stats/edge/`, `docs/edge.md`, `odds-check.yml`). Proposes; owners build. |
| `lead-reviewer` | Reviews and merges specialist branches, CI, CLAUDE.md, README, the work plan, and credit budgets. |

- Specialists work on their own branches: `agent/<name>`.
- The lead reviews each branch, runs the full checks and merges into `claude/soccer-stats-scaffold`.
- Only the lead sets Odds API credit caps. The default cap is 0.

## Conventions

- Run checks before every push: `uv run ruff format src tests && uv run ruff check src tests && uv run pytest -q`. All must pass. Use `.venv/bin/pytest` if `uv` isn't on the path. JS: `node --check web/app.js`.
- No look-ahead anywhere: features, refits and calibration use only earlier kickoffs. Tests check this.
- Commit messages: imperative summary, a body explaining why, and the attribution lines the session gives.
- App text is plain English for a non-specialist. Money is in $ for portfolio views and in units for the match replay. Odds are American plus decimal.
- Charts follow the dataviz rules already used:
  - blue `--pos` and red `--neg` diverging colours;
  - a readout line instead of tooltips over the chart;
  - colour tokens defined for both light and dark themes.

## Status (2026-10-06)

- Match bets: the DraftKings backtest at a 12% edge lost (ROI about −17%). The claimed edges pick out model errors more than value. Live paper trading continues.
- Match blend (6 Oct; print-only run of `backtest-dk --seasons 2025` on the cached DraftKings odds, 368 matches; blend fitted on up to 1,468 earlier Pinnacle-priced matches):
  - 1X2 log loss: model 1.0302, blend 1.0178, DraftKings close 1.0123. The fit gives the model no weight (latest b = 1.04, c = −0.03): the price already holds what the model knows.
  - Raw, no cap, at 2/5/8/12/20%: 313/261/217/173/112 bets, ROI −8/−8/−12/−13/−21% (12%: 95% range −38% to +16%), CLV −5% to −7%.
  - Blend: 0 bets at every threshold (with `--seasons 2023-2025`, 2,276 fit matches: 5 bets at 2%, all lost; none above).
  - The cached DraftKings history has no over/under 2.5 prices (0% of looks), so totals are untested.
  - Once `E0_dk.json` holds `blend.live` (next non-print `backfill.yml` run), match value picks and paper trades use the blend, so they will mostly stop: the honest result.
- Player model, stage 1: it beats the season-average baseline (shots log loss 0.420 vs 0.496 before lineups; 0.400 lineup known). The gate is passed.
- The starter flag was fixed on 6 Oct. Understat sets `roster_in` on starters who were replaced, so a starter is now anyone whose position isn't "Sub". Check: 11 starters in all 3,040 team-matches.
- Player bets, priced backtest on FanDuel 2023/24–2025/26, at a 12% edge:

  | Strategy | Bets | ROI |
  | --- | --- | --- |
  | Starters after lineups (blend) | 153 | −17% |
  | Blend, 3 hours before | 223 | −35% |
  | Raw model, 3 hours before | 628 | −22% |

  - Every threshold from 2% to 20% loses money.
  - The blend is well calibrated (23.3% predicted vs 23.1% won among starters). FanDuel's over-only lines imply 33%, so their margin is the obstacle, not the model.
- The live app still opens player paper trades using the raw model at 12%; the blend and lineup rules are not live yet.
- No live lineup feed has been built. Candidate source: ESPN's summary API (`rosters[].roster[].starter`). Build it only if some strategy backtests positive.
- Next ideas are in the work plan's "Status and next steps" section. First: find a bookmaker with two-sided (over and under) EPL player shot lines.
- Credits: 22,962 left on 6 Oct.
