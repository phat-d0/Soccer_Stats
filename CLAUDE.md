# CLAUDE.md

Soccer betting model and phone app. The match model is Dixon-Coles plus xG (goals-only where
Understat has no xG); team corners use model (f); player markets use a factor model. The
web app (PWA) "Soccer Model" is published at https://phat-d0.github.io/Soccer_Stats/. The
owner is Phat (GitHub phat-d0); he works from an iPhone and from Cursor on a laptop.

This file is the single technical map and the current state. Past rounds, old run ids and
superseded results are in `docs/history.md`; research write-ups are in `docs/lab.md`,
`docs/totals.md`, `docs/leagues.md`, `docs/edge.md`, `docs/player_props.md` and
`docs/team_news.md`. The work plan is the Docs artifact "Soccer Stats App: Backtest and
Portfolio Scope" (https://claude.ai/code/artifact/ff86c3e7-f90b-47a9-8474-28c7c7266e1d),
section "Status and next steps".

## Current state (21 Oct 2026)

Every team reads this first, from `origin/claude/soccer-stats-scaffold` (see "Before every
task" under Agent team). If something here looks wrong, tell the lead.

**Live leagues** (`leagues.py`, `live=True`): E0 Premier League, SP1 La Liga, D1 Bundesliga,
I1 Serie A, F1 Ligue 1, E1 Championship, E2 League One, E3 League Two, UCL Champions League.
E1, E2 and E3 are goals-only (no Understat). UCL runs on the cross-league model
(`cup_model.py`); clubs no football-data league covers get odds only. The Europa League
(UEL) and Conference League (UECL) are registered off: never fetched, built or listed.

**Match strategies (Moneyline portfolio), both paper, $10, one trade per match each:**
- **Blend Lean (1σ+)**: `p_bet` from each league's fit in data-log `backtest/match_blends.json`
  (every live league, UCL included: σ 0.0475 on 302 rows; fits dated 13 Oct). Trades when
  z = (p_bet − 1/DraftKings' price)/σ ≥ 1; "strong" at z ≥ 2. Most Lean picks come from the
  blend's price weight b > 1 sharpening the price, not from the model; each pick's `driver`
  ("market"/"model") says which (`docs/history.md`, round 14 "Blend Lean beside the 12% test").
  Blend Lean trades only 1.50–3.00 since 24 Oct (owner; `trades.LEAN_ODDS_RANGE`, lo < odds ≤
  hi, no fallback to another side), backcast from Lean's start as "Blend Lean (new rule)";
  the old rule is tracked as a shadow ("Blend Lean, all odds (old rule)"; `docs/leagues.md`
  "Blend Lean odds range").
- **Model 12%**: `trades.PAPER_RULE = "fixed_raw"`, the raw model's chance at a 12% edge
  against DraftKings' quoted price. The owner's live test.
- The Matches tab shows Lean only; Portfolio → Moneyline shows both.
- Kelly sizing tracked as an alternative; flat $10 is the record (owner, 24 Oct; `sizing.py`,
  `docs/leagues.md` "Kelly sizing"): $1,000 paper bankroll per strategy, quarter Kelly capped
  at 5%, on the blend's chance (Lean, Model 12%) or model (f)'s (corners); no new bets.
  Backtests: Kelly loses more than flat for Lean and mostly declines Model 12%'s picks; Kelly on
  the raw model goes broke (`docs/leagues.md` "Kelly sizing").

**Team corners (portfolio "Team corners", testing), two paper tracks, model (f), 12%, $10:**
- Model (f) 12% vs Pinnacle, in E0, SP1, D1, I1, F1, E1.
- Model (f) 12% vs DraftKings, from 20 Oct (DraftKings lists no team corners in E1).
- One call per match snapshot asks both books (`bookmakers=pinnacle,draftkings`, 1 credit);
  corner cap 550 credits a month.
- Shadow filter "below 60%" (owner, 23 Oct; pre-registered in `docs/totals.md` "Shadow filter:
  below 60%", commit e0b0bcd): each track's trades with model chance given no push < 0.60, shown
  beside the track (`pinnacle_below60`, `draftkings_below60`). No bets of its own. Judged only on
  trades opened from 23 Oct 08:39 UTC (`trades.SHADOW_DEFINED_AT`), on the matched CLV difference
  vs Pinnacle's close, at the 8 Dec review; no live change unless the owner decides then.
- No corners in E2, E3 or the cups: Pinnacle quotes none in E2/E3 (11 Oct probe), and the
  cup corner model failed its pre-registered test (PR #69; `docs/totals.md` "Cup corners model").
- A League One / League Two corner re-check runs on 24 Oct. If Pinnacle lists team corners
  there, the owner has pre-approved switching them on and raising the corner cap to 750.

**Team totals:** logged for data only (FanDuel team totals plus the match total at FanDuel,
Bovada and Pinnacle; cap 800 a month). Nothing is scored until the round-13 gate (50 settled
matches for each anchor; `docs/totals.md` "Team totals against the main market").

**Players:** Anytime goalscorer is testing; its forward check (2026/27 from 10 Oct) is locked
until opened once with a reason. Player shots are retired (parked); `PLAYER_PAPER_TRADES =
False`, so no player paper trades open.

**Settlement:** football-data's result when it has one, else ESPN's (`espn_results.py`), a
few hours after full time. `closes.yml` plans close-snapshot runs before kickoffs and
settlement runs after them.

**Backtests** (data-log `backtest/`):
- `lean_backtest.json` and `edge12_backtest.json`: each strategy per league at Pinnacle's
  early price (domestic, `kind: "early"`), with UCL at DraftKings' bought close
  (`kind: "close"`, kept out of the pooled numbers).
- `E0_dk.json`: Model 12% at DraftKings' own prices, Premier League 2025/26 only.
- `match_blends.json`: the live Lean fits.

**Research verdicts to date** (nothing beats Pinnacle's close):
- 1X2 bake-offs (rounds 4, 5, 7), lineup surprises, free signals: `docs/lab.md`, `docs/edge.md`.
- Totals, corners bake-offs 1 and 2, team-corner recalibration, cup corners: `docs/totals.md`.
  Bake-off 2's finalist (f) in E0, I1 and F1 has one locked test left (3 Feb 2027).
- Model 12% filters (favourites only; no long shots): neither passes, pooled CLV −2.7% and
  −3.1% vs −4.6% for the plain rule (`docs/lab.md` "Model 12% filters").
- Price-band edges: every band's best CLV is below 0, so the frozen rule bets nothing
  (`docs/lab.md` "Price-band edge rule").
- Model 12% per league at Pinnacle's early price: CLV below 0 in every league; pooled 8,910
  bets, ROI −5.7% (−9.7% to −1.6%), CLV −4.6% (`docs/leagues.md` "Backtests in every live league").
- Blend Lean backtest, pooled over the eight domestic leagues: data-log's 21 Oct file has 2,626
  bets, ROI −2.2% (−7.5% to +2.9%), CLV −3.1% (−3.4% to −2.8%); the branch run in
  `docs/leagues.md` had 2,629 bets, ROI −2.3%. UCL Lean: 0 bets (only one block reaches the
  300-row minimum).
- The model overrates the side it likes in every price band, favourites included (favourites:
  model 63.1%, won 57.1%, price 58.7%), most of all on long shots: the side it likes wins a
  little less often than the price implies, so its win rate tracks the price, not the model
  (`docs/lab.md` "Price-band edge rule", step 1, and the "Model 12% filters" verdict).

**Live paper record** (publish of 20 Oct 08:14 UTC; changes daily, so read the publish log or
the app): Blend Lean 18 trades (13 settled), Model 12% 139 (88 settled), corners 54 (all settled).

**Credits:** about 19,800 left on 20 Oct. Live spend about 3,630 a month: DraftKings h2h
~2,476 (with League One/Two and UCL ~86), team totals ~682, corners ~471. The key is shared with
the owner's baseball app, so the balance can drop without any run here.

**Scheduled checks** (send_later into the lead session; trigger ids):
- 24 Oct 2026 (`trig_01N3hvxJ71uBGNfCGvcdmQxy`): League One/Two corner re-check near kickoff
  (`odds-check.yml` `task=markets`, `leagues=E2,E3`, `cap=4`); see Team corners above.
- 1 Nov 2026 (`trig_01PZn5NWRdXbQXB4PV3Y4C8g`): goalscorer forward-window count (E0, SP1, D1,
  I1, F1 since 10 Oct). At 150 or more, open it once with a reason in both places: `odds-check.yml`
  `task=goal-leagues` with `reason`, and `players.yml` with `goal_forward_reason`.
- 9 Nov 2026 (`trig_01RznLks8hbfhYeLHgWPQCR5`): Lean refit (`backfill.yml` `match_blends`, then
  `lean_backtest`) and Lean live tracking by driver.
- 12 Nov 2026 (`trig_01U5txZuqCkL1ZeB5ik3XAJK`): should Blend Lean extend to team corners and
  team goals? Team goals wait for the round-13 gate; corners need a pre-registered blend and σ.
- 16 Nov 2026 (`trig_01N4zsU1KdvQ66W9MZhGBDeK`): late team news vs the DraftKings odds log
  (Research lab; ≥100 logged matches). Measure how fast DraftKings reacts compared with
  Pinnacle after a lineup surprise; data-log `team_news/` has `first_confirmed_at` per match.
- 7 Dec 2026 (`trig_01KakNWotv6GkGQUqhjTvtbT`): goalscorer forward check, five leagues, ≥150
  pooled matches (may be ready in early November; delete once the window is opened).
- 8 Dec 2026 (`trig_01SJk1xh2PSaKfKEe6VfHW2m`): live team-corners review: per league and pooled
  trades, ROI and CLV with 95% ranges, E0/I1/F1 apart from SP1/D1/E1, credit use vs the cap;
  record in `docs/totals.md`.
- 3 Feb 2027 (`trig_011PdvR6MJoUd3gRpLbJaie6`): corners bake-off 2's locked test (E0, I1, F1,
  finalist (f)); `odds-check.yml` `task=corners2`, `finalists=f`, `cap=3`, a logged `reason`.

**Open decisions with the owner:**
- A weekly cache-touch job so GitHub doesn't evict the paid caches (offered, no answer yet).
- Logging Champions League corner prices only, without a model (offered; recommended no for now).

## Branches and where things run

- `claude/soccer-stats-scaffold`: the default branch. All code is here.
- `data-log`: written only by workflows. Read it with
  `git fetch origin data-log && git show origin/data-log:<path>`. Holds:
  - paper ledgers: `paper_trades/<code>_<season>.jsonl` (match trades, both strategies) and
    `paper_trades/<code>_corners_<season>.jsonl` (corners; `paper.load_ledger` skips them),
    append-only "open" plus "update" events;
  - FPL team news (`fpl_news/`); ESPN team news `team_news/<code>_<YYYY-MM>.jsonl` (one row per
    match and team when its lineup or injuries change; `first_confirmed`/`first_confirmed_at`);
  - `odds_log/<code>_<YYYY-MM>.jsonl`: the live DraftKings h2h log;
  - `odds_log/<code>_team_totals_<YYYY-MM>.jsonl` and `odds_log/team_totals_calls_<YYYY-MM>.jsonl`
    (one row per event and snapshot; read before each build so nothing is fetched twice);
  - `odds_log/<code>_corners_<YYYY-MM>.jsonl` (`market: "team_corners"`, Pinnacle and DraftKings
    rows, `book` says which; rows without `book` are Pinnacle's) and
    `odds_log/corners_calls_<YYYY-MM>.jsonl` (one row per event, snapshot and market group);
  - `odds_history/UCL_closes.jsonl` and `UCL_close_calls.jsonl` (bought Champions League closes);
  - `backtest/`: `E0_dk.json`, `E0_dk_trades.csv`, `E0_players.json`, `E0_players_detail.json`,
    `E0_goals.json`, `E0_player_trades.csv`, `E0_player_lines.csv.gz`, `match_blends.json`,
    `lean_backtest.json`, `edge12_backtest.json`.
- GitHub Pages serves the app (source = GitHub Actions). The repo must stay public, or Pages 404s.
- Nothing runs locally in a cloud session: the sandbox can't reach Understat, FPL, The Odds
  API, ESPN or github.io. Every data job runs in GitHub Actions: trigger with the GitHub MCP
  `actions_run_trigger`, read logs with `get_job_logs`.

## Workflows (`.github/workflows`)

| File | What | Credits |
| --- | --- | --- |
| `publish.yml` | `soccer-stats publish` builds the site for every live league, then on `data-log` logs FPL news (`log-news`), DraftKings prices (`log-odds`), ESPN team news (`log-team-news`), team totals (`log-team-totals`), corners (`log-corners`) and paper trades (`paper`), and deploys Pages. Team totals and corners are fetched only on the default branch (`TEAM_TOTALS_DIR`, `CORNERS_DIR`), after copying data-log's call records and price files into `TEAM_TOTALS_STATE` / `CORNERS_STATE`. ESPN team news runs on every branch (free; ≤120 s or 3 failures a run). Restores `raw-data-*` and `cup-espn-*`; copies data-log's `match_blends.json` (`MATCH_BLENDS_FILE`) and `E0_dk.json` (`DK_BACKTEST_FILE`). Only the default branch publishes. Hourly cron plus every 15 min 10:00–22:00 UTC (GitHub throttles it; `closes.yml` fills the gaps); `cancel-in-progress: false`. | 1 per DraftKings h2h refresh per league (E0 hourly; matchday leagues only within 48 h of a kickoff, pausing below 3,000), 2 per team-total snapshot (cap 800 a month), 1 per corner snapshot (cap 550 a month), plus live player lines near kickoff |
| `closes.yml` | Planner: reads the coming kickoffs from data-log's odds logs, dispatches `publish.yml` 15 min before each kickoff cluster (`close_runs.plan`, CLI `close-plan`) and a settlement run 2 h 15 min after each cluster's last kickoff (`SETTLE_AFTER`, merged into a close run starting within 20 min, `SETTLE_MERGE`); re-dispatches itself every 5.5 h while a kickoff or settlement is due; cron every 3 h at :41. Default branch only; concurrency `closes`. | 0 extra (the call records never fetch twice) |
| `backfill.yml` | Inputs: `estimate_month` (credit estimates per league, team-total and corner snapshots; no key), `cups` (+ `cup_seasons`: `estimate-cups`), `match_blends` (`fit-match-blends` → `backtest/match_blends.json`), `lean_backtest` (`backtest-lean` → `lean_backtest.json`), `edge12_backtest` (`backtest-edge12` → `edge12_backtest.json`), `edge12_filters` (`backtest-edge12-filters`, research, prints), `match_markets` (+ `markets_seasons`, research, prints), `print_only` (`backtest-dk` on cached odds), `cup_odds` (+ `cup_seasons`; `backfill-cup-odds`, cap = `max_credits`, `dry_run`/0 = plan only). Otherwise: historical DraftKings odds (`seasons`, `max_credits`, `keep_credits`, `dry_run`) then `backtest-dk` → `E0_dk.json`. Writes to data-log only from the default branch; elsewhere log + 7-day artifact. | 0 for every no-key input; `max_credits` 0 = download nothing; historical ~20 per snapshot; `cup_odds` ~10 per kickoff slot |
| `players.yml` | Player model stage 1, the priced shots backtest on cached FanDuel lines and goalscorer stage 1 → `backtest/E0_players*.json`, `E0_goals.json`. Weekly (Mon) plus dispatch. Inputs: `seasons` (default `2023-now`), `odds_seasons` (blank = download nothing), `max_credits`, `dry_run`, `goal_forward_reason` (opens the goalscorer forward window; scheduled runs stay locked). | 0 unless `odds_seasons` is set |
| `odds-check.yml` | Diagnostics and research; never pushes. `task`: coverage, props, match, shots, signals, lab, lab-holdout, lab-coverage, lab-edge, lineups, totals, corners, corners2, corner-coverage, corner-recal, cup-corner-coverage, cup-corners, corner-history, markets, goalscorer, player-lab, goal-lab, goal-league, goal-leagues, team-totals, band-edges, espn. Other inputs: `cap` (credit cap for coverage, props, goalscorer, markets, corner-history; 0 = dry run; only these get the key), `league`, `leagues` (markets/espn: e.g. `E2,E3` or `UCL,UEL,UECL`, with free name checks), `reason` (opens a locked holdout or forward window once), `finalists`, `holdout_season`, `stack_base`, `after` (team-totals confirmation), `hist_dates`, `seasons`. | 0 except the capped tasks |
| `ci.yml` | Every push and PR. Job `test`: `uv sync --frozen`, ruff format check, ruff check, pytest, `node --check web/app.js`. Job `web`: Playwright Chromium (cached) runs `tests/web/smoke.mjs`; a skip fails; screenshots uploaded on failure. No secrets. | 0 |

- After `players.yml` or `backfill.yml` writes a backtest, dispatch `publish.yml` so the app picks it up.
- Caches: `raw-data-*` (Understat), `odds-history-*`, `player-odds-history-*`,
  `cup-odds-history-*` (paid historical odds), `cup-espn-*` (ESPN cup days),
  `understat-<league>-*`, `cup-corners-*`. Never delete the paid ones: refilling costs credits.

## The Odds API

- Repo secret `ODDS_API_KEY`, 100K-credit plan, **shared with the owner's baseball app**.
  Read the balance from the publish log ("credits left") or a backfill log. Never print the
  key or write it into logs, data, the ledger or commits. Only the lead sets credit caps;
  the default cap is 0.
- Billing: one credit per market per region per call; up to ten books in one call count as
  one region. Historical event odds cost about 10 per region per market; empty snapshots less.
- **Match odds: DraftKings h2h** (`odds_feed.MARKETS = "h2h"`, 1 credit a refresh; DraftKings
  lists no main soccer `totals`). The budget still counts 2 a call (`BUDGET_COST`).
- **Team totals:** `/events/{id}/odds`, `bookmakers=fanduel,bovada,pinnacle`,
  `markets=team_totals,totals`, 2 credits a call, E0, SP1, D1, I1, F1 (`team_totals.py`).
  Pinnacle quotes no goal team totals on this API.
- **Team corners:** `alternate_team_totals_corners`, `bookmakers=pinnacle,draftkings`, 1 credit
  a call, E0, SP1, D1, I1, F1, E1 (`corners_live.py`). DraftKings doesn't list it in E1.
- **Player shots: FanDuel**, over sides only, whole numbers meaning "at least X"; an over wins
  at `count >= ceil(line)`; implied chance = 1/odds (margin included). Cached FanDuel
  history covers 2023/24 (sparse), 2024/25 and 2025/26. DraftKings has no EPL player props here.
- Every paid path keeps 3,000 credits in reserve on the freshest balance and counts cost from
  `x-requests-last`.

## Code map (`src/soccer_stats`)

**Leagues, match model and odds**
- `leagues.py`: the registry `LEAGUES` (football-data code, name, Understat name or None,
  Odds API sport key, `live`, `odds_policy`, `cup`). E0 policy `always`; every other live
  league `matchday`. `live_codes()` lists live leagues, E0 first. UEL/UECL are off (test
  `test_off_leagues_stay_out_of_every_live_list_and_data_json`). How to switch one on and
  what it costs: `docs/leagues.md`.
- `data.py`: football-data loaders; `pinnacle_closes`/`pinnacle_only` and `pinnacle_prices`
  return Pinnacle's own prices (no average/Bet365 fallback). `xg.py`: Understat xG merged onto
  football-data (`TEAM_NAMES`). `players.py`: FPL availability → team adjustments (E0).
  `markets.py`: score matrix → market chances. `odds.py`: odds conversion, margin removal.
- `models/dixon_coles.py`: the match model; `ridge` and `neutral` options for the cup model,
  off by default.
- `backtest.py`: `walk_forward` (weekly refits on earlier matches; the final partial week is
  predicted too), the DraftKings backtest (`dk_trades`, `dk_strategies` raw/blend sweeps with
  bootstrap ranges, `dk_log_loss`) and `dk_tiers` (Lean replayed on it; `E0_dk.json` → `tiers`).
- `match_calibration.py`: blend `score_k = a_k + b·log(price_k) + c·log(model_k)` (1X2; logistic
  for two outcomes), fitted on Pinnacle's close beside walk-forward model chances, refitted
  every 28 days on earlier matches. `sigma(coef, market, model)` = sd of (blend − price) on
  the fit rows; `league_fit(rows, fit_date)` = one league's live h2h fit (`coef`, `sigma`,
  `matches`, `fit_from`, `fit_to`, `fit_date`, `refit`, `walk_forward` check);
  `without_model` = the blend with c = 0; `training_rows`, `MIN_ROWS` 300. `E0_dk.json` →
  `blend.live.{h2h,totals}.coef` is E0's older fit (its totals part is still used for `p_bet`).
  Row weights and extra signal columns exist for research (`RESEARCH_GROUPS`).
- `fit-match-blends` (CLI; `backfill.yml` `match_blends`): per live league `walk_forward` from
  2022-07-01 (burn-in 2, xG weight 0.7, 0 for goals-only leagues) beside Pinnacle's own close,
  then `league_fit` today; UCL via `cup_model.blend_entry`. Writes `match_blends.json` =
  `{generated_at, pool, from_season, leagues: {code: {coef, sigma, matches, fit_from, fit_to,
  fit_date, refit, xg_weight, coverage, walk_forward}}}` and a `MATCH_BLENDS_JSON` log line.
- `lean_backtest.py` (CLI `backtest-lean`): Blend Lean walk-forward per league at Pinnacle's
  early price (decision day = kickoff − `EARLY_LEAD` 4 days; fits every 28 days on matches
  before the block; σ per fit; bets via `trades.lean_pick`, $1, `driver` from the c = 0 blend,
  CLV vs Pinnacle's Shin-fair close). Seasons scored only when early and close coverage are
  both ≥ 90% (2019/20–2024/25). `lean_backtest.json` = `{generated_at, kind, title, note, rule,
  leagues: {code: {name, kind, coverage, seasons_included, seasons_excluded, training_matches,
  fits, first_fit, sigma, candidates, xg_weight, lean, strong, by_season} or {error}}, all: {lean,
  strong, by_season, by_league}, draftkings_replay, bet_fields, bets_total, bets}` (bets newest
  first, ≤ 6,000; metrics `bets, wins, win_rate, breakeven, roi, roi_ci95, profit, avg_odds, avg_z,
  z_realized, clv, clv_ci95, clv_bets, beat_close, by_market`, plus `by_driver`). `app_section`
  trims to the newest 200 bets or returns `{available: false, note}`.
- `edge12_backtest.py` (CLI `backtest-edge12`): Model 12% replayed the same way (`best_pick` at
  12% on the raw model, $1); metrics add `model_p`, `avg_edge`. `edge12_backtest.json` has the
  same shape with `edge12` in place of `lean`/`strong` and `all.leagues`; `draftkings_replay`
  names `E0_dk.json`. Log line `EDGE12_BACKTEST_JSON`.
- Calibration in both files (owner, 22 Oct): each league report and `all` carry `calibration =
  {all_outcomes: {model, blend (Lean file only), price}, bets, note}`. `all_outcomes` scores every
  outcome (home/draw/away) of every scored match (Lean: matches with a blend fit; Model 12%: every
  candidate) on the same rows: the model's raw walk-forward chance, the blend `pb`, and the
  margin-free price (Pinnacle's early; DraftKings' close for UCL), the price given as the others'
  `implied`. `bets` = the strategy's own picks (`p_bet` for Lean, `model_p` for 12%, `predicted_from`
  names it) vs 1/odds vs won. Each is a `calibration.reliability` dict. The outcome rows ride in
  each report under `lb.OUTCOMES` for the pooled view and are never saved (`lb.saved`). Log lines
  `LEAN_CALIBRATION_JSON` / `EDGE12_CALIBRATION_JSON` (n, slope and range, Brier, log loss per
  series; the bets' predicted, implied, won).
- Bands in both files (owner via the Research lab, 23 Oct; `bands.py`): every league entry
  (UCL included, not `{error}` ones) and `all` carry `by_odds_band` (`ODDS_BANDS` ≤1.50,
  1.50–2.00, 2.00–3.00, 3.00–5.00, >5.00, lo < odds ≤ hi; empty bands kept as `bets: 0`;
  `{lo, hi, bets, share, wins, win_rate, breakeven, roi, roi_ci95, clv, clv_ci95}`),
  `by_edge_band` (five bands of the claimed edge, `claimed` = chance × odds − 1: `model_p` for
  Model 12%, `p_bet` for Lean, which adds the mean `z`; the band dict adds `claimed`) and
  `by_clv_band` (five CLV bands with their ROI). Edge and CLV cuts are the pooled domestic bets'
  quintiles, computed once in `build` and applied to every league; the file's top-level `bands`
  = `{odds_bands, edge_cuts, clv_cuts, edge_from, note}`. Every bet counts (not the trimmed
  rows); ranges resample matches (`boot_mean`, 2,000 draws). Log lines `LEAN_BANDS_JSON` /
  `EDGE12_BANDS_JSON` (pooled and UCL). `app_section` passes them through.
- Sizing in both files (owner, 24 Oct): every league entry (not `{error}`) and `all` carry
  `sizing` (`sizing.backtest_sizing`, every bet in time order, one bankroll for the pooled
  domestic bets): `{flat, kelly}` on `p_bet` in the Lean file; `{flat, kelly, kelly_raw}` in the
  Model 12% file, Kelly on `p_blend` (`edge12_backtest.with_blend`: the Lean backtest's own
  per-block blend for the bet's side, NaN before a league's first fit, so no stake) and
  `kelly_raw` on `model_p`. Log lines `LEAN_SIZING_JSON` / `EDGE12_SIZING_JSON` (no curves).
- `sizing.py` (owner, 24 Oct; display only): Kelly sizing tracked beside flat $10 over the same
  trades or backtest bets. `KELLY_FRACTION` 0.25, `KELLY_CAP` 0.05, `BANKROLL_START` 1,000,
  `FLAT_STAKE` 10; `kelly_f(p, d)` = (p·d − 1)/(d − 1), `kelly_stake(bankroll, p, d)` (0 when f ≤ 0,
  no p or no bankroll); `simulate(bets, stake_fn)` (events in time order: a stake uses the
  bankroll from bets settled at or before its open, P/L at settlement, pushes/voids return the
  stake) → `{bankroll_start, bankroll_now, profit, staked, roi_on_staked, max_drawdown,
  max_drawdown_pct, growth_per_bet (log(end/start) per settled bet), bets, bets_settled,
  bets_staked, bets_zero_stake, median_stake, curve [{date, bankroll}] (≤ `MAX_CURVE` 200),
  stakes}`; `block` adds `fraction, cap, stake (flat), p_source, p_note, bets_no_chance, note`.
  `live_sizing(trades, rule)` = `{flat, kelly, kelly_raw (Model 12%: raw model_p, "over-confident"),
  blend_from (Model 12%: counts by p_blend_from)}`, only `kelly` keeps `stakes` {trade id: stake};
  `chance`: Lean `p_bet`, Model 12% `p_blend`, corners model_p ÷ (1 − p_push).
  `backtest_sizing(df, p_col, p_source, raw_col=, lead=)` (open = kickoff − `BACKTEST_LEAD` 4
  days, 0 at the close; no stakes kept).
- `calibration.py`: `reliability(pred, won, implied=None, edges=EDGES, min_n=20, groups=None,
  few_below=None)` = `{n, wins, predicted, implied?, realized, brier, log_loss, intercept,
  intercept_ci95, slope, slope_ci95, bins: [{lo, hi, n, wins, predicted, implied?, realized,
  realized_lo, realized_hi, few?}]}` (`few: true` when n < `few_below`).
  Bins on the predicted chance at 0–10, …, 60–70, 70–100%; empty bins dropped, a bin under 20 rows
  joins the next (the last joins the one before); Wilson 95% ranges. Recalibration logit(won) = a
  + b·logit(pred) by Newton, 95% ranges from standard errors, cluster-robust over `groups` (one
  id per match); slope < 1 = over-confident; None under 10 rows or one outcome. Binary log loss
  per row (not the 1X2 one). `outcome_rows` (wide `<prefix>_<outcome>` → one row per outcome,
  `group` = key|row) and `series_report`.
- `equations.py`: `section(data)` → `portfolio.models` = `{order, match, blend, corners_f}`, each
  `{key, name, equation (lines), terms, note, leagues: {code: {name, ...}}}` from data.json only:
  `match_model` (publish.`fit_info`: `kind` domestic/cross_league, `intercept`, `home_adv`, `rho`,
  `xi`, `half_life_days`, `xg_weight`, `ridge`, `matches`, `teams`, `fit_from`, `fit_to`; E0 from
  `build_data`, the others from `league_fixtures`/`cup_league_fixtures` via the odds source's
  `model_fit`, popped before `odds_sources`), `match_blends.leagues` (`coef` as {a_draw, a_away, b,
  c}, `sigma`, `matches`, `fit_from`, `fit_to`, `fit_date`; the blend card also carries
  `odds_range`, `odds_range_from`, `odds_range_note` and an equation line for the range) and `corners_model` (now with
  `alpha_home`/`alpha_away`).
- `cup_backtest.py`: the Champions League in both files (`leagues.UCL`, `kind: "close"`, out of
  `all`): `cup_model.history()` chances bet at DraftKings' bought close, CLV vs Pinnacle's
  close at the same moment (it mostly measures the gap between the books). Lean needs 300
  earlier rows, so it scores few blocks; `note` says how many. No closes file = `{kind, error}`.
- `match_markets.py`: research only. 1X2, O/U 2.5 and Asian handicap vs Pinnacle; `clean_prices`.
- `odds_feed.py`: live DraftKings odds and the credit budget. `policy_floor`: E0 every 1 h (30 min
  within 2 h of a kickoff); matchday leagues nothing without a kickoff within 48 h, then 3 h,
  1 h within 6 h, 30 min within 2 h. `refresh_interval_hours` splits the budget; E0 always
  budgets with share 1. Matchday leagues keep `MATCHDAY_RESERVE_CREDITS` 3,000 (on
  `latest_meta`), E0 `RESERVE_CREDITS` 20. `TEAM_NAMES` maps Odds API spellings to
  football-data's for every league (a missing one shows as a duplicate card;
  `tests/test_leagues.py` lists League One/Two's 48 clubs); `name_check` for free name checks;
  `estimate_credits` and `estimate_snapshot_credits` replay scheduled runs (CLI
  `estimate-credits`, `estimate-team-totals`); `TEAM_TOTAL_LEAGUES` = E0, SP1, D1, I1, F1.
- `odds_log.py`: the DraftKings log (`log-odds`, from the built `data.json`, no API calls): one
  row per fixture and market with prices, margin-free `fair`, `bookmaker`, `fetched_at`
  (`time_source`), `downloaded_at`, `logged_at`, the model's `p` and `p_bet`; deduplicated;
  quotes at or after kickoff skipped. `last_before(...)` = the live close.
- `odds_history.py`: historical match odds (cached).
- `team_totals.py`: live team totals plus the match total. `due_snapshot`: "look" 18–30 h before
  kickoff, "close" within 30 min (75 min when no later scheduled run is left). Event ids from
  the cached DraftKings body; an (event, snapshot) in the call record is never fetched again.
  Before each call: balance − 2 ≥ 3,000 and month spend + 2 ≤ `MONTHLY_CAP` 800. An error
  part-way keeps the calls made and logs only the error type. Rows: per book, team (or `team`
  null and `side: "match"` for `market: "totals"`), line, both sides, Shin `fair_over/under`,
  `margin`, `fetched_at`, `minutes_before`, `p_model_over`; rows without `market` are team
  totals. `log` (CLI `log-team-totals`). `report`/`market_report` (CLI `team-totals-report`,
  `odds-check.yml task=team-totals`): the round-13 pre-registered test (anchors `fanduel_anchor`,
  `pinnacle_anchor`, the model, a 50/50 blend; counts only below the gate; `plumbing` never
  reads results). Log line "Team totals: N calls, C credits, R rows (T match totals); month M of
  800 credits, credits left X".
- `corners_live.py`: live team corners. `TeamCornersF` = bake-off 2's model (f) (empirical-Bayes
  team for/against, NB2 per side), fitted on the 730 days before now, ≥ `MIN_TRAIN` 300, from
  football-data HC/AC. `add_model(cards)` sets card `corners = {model: "f", home/away: {mean,
  cdf}}` (P(corners ≤ k), k = 0..15) and returns `data.corners_model` per league (or `{error}`).
  `side_probs`, `pick(cdf, line, over, under, threshold)` (the better side if P(win) × odds +
  P(push) − 1 ≥ threshold; whole lines push). `run`: one call per (event, snapshot, group
  "team_corners") for `BOOKS` pinnacle, draftkings; cap `CORNERS_MONTHLY_CAP` 550 and the
  reserve checked at `expected_cost` (dearest two-book call this month, ≥ 1); `LEAGUES` = E0,
  SP1, D1, I1, F1, E1. Rows: Pinnacle's two-sided lines, then DraftKings' (`book: "draftkings"`,
  one-sided lines kept with the missing side and `fair_*`/`margin` None). `newest_quotes(rows,
  book=)`, `add_quotes(cards, dirs, book=, rows=)` → card `corners.pinnacle` / `corners.draftkings`
  = `{book, fetched_at, home/away: [{line, over, under, fair_over, margin, snapshot,
  downloaded_at, p_over, p_under, p_push, pick}]}`. `log` (CLI `log-corners`), `corner_results`
  (HC/AC for settlement), `estimate` (CLI `estimate-corners`). Corner files and rows never mix
  into the team-total test (tests). Log lines "Corners (Pinnacle, DraftKings): N calls, C credits,
  R rows (P Pinnacle, D DraftKings); month M of 550 corner credits, credits left X" and "Corners
  shown: …".
- `espn_news.py`: free ESPN team news for all live leagues (`SLUGS`: eng.1–eng.4, esp.1,
  ger.1, ita.1, fra.1, uefa.champions). `add(cards)`: scoreboard one date per call (cached 6 h),
  events matched by both names (`team_name`: `ESPN_NAMES`, then `odds_feed._team`) and kickoff
  ±3 h, never adding cards; summaries within 36 h of kickoff (every 3 h; every run within 90
  min until both XIs are in), ≤ `MAX_SUMMARIES` 90 a run, `RUN_SECONDS` 120, `MAX_FAILURES` 3.
  Card field `team_news` = `{source: "ESPN", event_id, fetched_at, updated, injuries: {home,
  away}, lineup: {confirmed, fetched_at, first_confirmed_at?, home?/away?: {starters, subs}}}`
  (ESPN carries no injuries today). `log` (CLI `log-team-news`); `probe` (CLI `espn-probe`).
- `espn_results.py`: ESPN scoreboard results for domestic settlement, from 1 h 45 min after
  kickoff, only for days with a trade waiting. `final` = completed, not postponed/abandoned,
  90-minute score known. Settles only on one event with both names exact, home/away right,
  kickoff ±3 h; corner counts from the summary's `wonCorners`. `Budget`: 60 s, 3 failures, 80
  requests. Rows carry `result_source: "espn"`. Log "ESPN results: N of M matches final …".
- `close_runs.py`: the close and settlement planner behind `closes.yml` (`plan`, `SETTLE_AFTER`,
  `SETTLE_MERGE`, `settle_pending`).
- `cup_model.py`: the cross-league model for UCL. One goals-only Dixon-Coles fit (`XI` 0.0019,
  `RIDGE` 0.1, `WINDOW_DAYS` 730, matches before `as_of` only) over football-data's top and
  extra European leagues plus every UEFA cup result with a 90-minute score from ESPN
  (`european_events`, `team_map` via `cups.match_team`; unmatched clubs keep `UEFA:<id>`).
  `Named` (cards' ESPN names), `walk_forward` (Monday refits), `history()`, `load_closes`,
  `evaluate`, `blend_entry` (UCL's `match_blends.json` entry). Live: `Budget` (90 s, 3 failures),
  `upcoming` (14 days), `pair_odds`, `no_model_card` (`no_model`, `low_data`, `no_model_reason`),
  `cup_results` (90-minute scores for settlement).
- `cups.py`: UEFA calendars from ESPN (`season_calendar`, `parse_events` with `score90`,
  `neutral`, `extra_time`), cost estimates (CLI `estimate-cups`), `team_coverage`, `summary_corners`.
- `cup_odds.py`: the bought Champions League closes (CLI `backfill-cup-odds`, `log-cup-odds`):
  one historical h2h snapshot per kickoff slot, Pinnacle and DraftKings, through
  `player_goal_odds.CappedBudget`; never buys a slot already held; key redacted (`redact`).
- `cup_corners.py`: research only (cup corner model; failed, `docs/totals.md`).

**Trades and portfolio**
- `trades.py`: the trade rules; keep it free of network code.
  - Match bets: `best_pick`/`select_trades` (12% by default), $10, one per match.
  - `PAPER_RULE = "fixed_raw"` (`PAPER_RULES` also has `"learned"`; reverting is one line).
    `paper_threshold(dk, league)` under `fixed_raw` = `{threshold: 0.12, source: "owner_fixed",
    note: OWNER_FIXED_NOTE, rule: "fixed_raw", p_source: "model"}` in every league; the trade uses
    the card's raw `p`, edge = `p` × DraftKings' decimal − 1. Under `learned` the threshold is
    `edge_threshold.min_edge` from `<code>_dk.json` or `lab/min_edge.json` (null = no new trades)
    with `p_source: "blend"`. `paper_rule_line()` prints the rule.
  - Blend Lean: every match trade has `strategy` `"lean"` or `"edge12"` (`strategy_of`: missing =
    `edge12`); `STRATEGY_ORDER = ["lean", "edge12"]`, `STRATEGY_LABELS`. `side_z`, `lean_pick(p_bet,
    odds, sigma, min_z=LEAN_Z, z_market=None)` → `{market, odds, p_bet, model_p, edge, z, tier
    ("strong" at `STRONG_Z` 2), sigma, z_market?, driver?}`; `market_side_z(coef, odds, sigma)`
    (c = 0); `lean_driver` ("market" at z_market ≥ 1, else "model"); `DRIVER_NOTE`;
    `lean_rule(match_blends, league)` = `{key, rule: "lean_1sigma", min_z, strong_z, p_source:
    "blend", stake, sigma, matches, coef, source, note, fit_made_at, odds_range,
    odds_range_from, odds_range_note, rule_version}` (no fit = σ None, no picks). Odds range (owner, 24 Oct):
    `LEAN_ODDS_RANGE` (1.50, 3.00), `in_lean_range(odds)` (lo < odds ≤ hi), `LEAN_RANGE_FROM`
    (the commit adding it, 2026-10-24T07:32Z), `LEAN_RULE_VERSION` 2, `LEAN_RANGE_NOTE`,
    `SHADOW_SUFFIX` "|shadow". `lean_pick` itself is unchanged (the backtest replays it).
  - Corners: `corners_rule(book="pinnacle")` = `{threshold: 0.12 (CORNERS_EDGE), source:
    "owner_fixed", note, rule: "fixed_raw", p_source: "model_f", book, stake: 10}`;
    `CORNER_STRATEGY_ORDER = ["pinnacle", "draftkings"]`, `CORNER_STRATEGY_LABELS`,
    `corner_strategy_of(t)` (`strategy`, else `bookmaker`, else Pinnacle). Shadow variants:
    `SHADOW_DEFINED_AT`, `chance_no_push(t)` (model_p / (1 − p_push) on corners), `FILTER_FIELDS`
    / `filter_value` / `passes_filter(t, {field, min?, max?, note})` (min ≤ value < max; missing =
    out), `CORNER_VARIANTS` (`{parent: [{key, label, filter, defined_at}]}`: `<book>_below60`,
    max 0.60, `BELOW60_NOTE`), `variant_rule`. `new_corner_trade(...,
    book=)` (DraftKings ids end `|dk`, `CORNER_ID_SUFFIX`); `settle_corner` (over wins at count >
    line; exact whole line = push, `status: "void"`, `push: true`).
  - Players: `player_picks` (best line per player and market, ≤ 4 a match); `PLAYER_PAPER_TRADES
    = False` (published as `players_status.paper_trades`).
  - Portfolios: `PORTFOLIOS` registry (`id`, `name`, `status` live/testing/retired, `note`,
    `backtest` stem): `moneyline` (live, `dk`), `goalscorer` (testing), `corners` (testing),
    `player_shots` (retired). `portfolio_of(trade)`.
  - `summarize` (adds `close_early`: closes taken over 60 min before kickoff), settlement, `clv`.
- `paper.py`: the live ledgers and `data.json` → `portfolio`.
  - Leagues in play (`leagues_in_play`): E0, every league with fixtures this build, and every
    registry league with a ledger on data-log (`ledger_leagues`), so a league between matchdays
    still closes and settles.
  - `update_ledger(..., rule=, lean=, espn=)`: gates = before kickoff, DraftKings odds ≤
    `FRESH_HOURS` 3 old, not `low_data`, priced. Model 12% opens on raw `p`; Lean opens on
    `p_bet` (id `<code>|<season>|<home>|<away>|lean`; records `z`, `tier`, `sigma`, `p_bet`,
    `p_model`, `z_market`, `driver`, `rule: "lean_1sigma"`, `p_source: "blend"`,
    `model_ref.probs: "blend"`; `LEAN_ENTRY_FIELDS`), plus `odds_range` and `rule_version: 2`
    since 24 Oct. A Lean pick outside `LEAN_ODDS_RANGE` opens no trade (no other side is
    tried); with `shadow=` (the league's shadow ledger) it becomes a shadow entry (`shadow:
    true`, `excluded_by: "odds_range"`, id + `|shadow`) when the match has no Lean trade or
    shadow yet. Shadows live in `paper_trades/<code>_lean_shadow_<season>.jsonl`
    (`load_lean_shadow`; `load_ledger` skips them, `append_events` routes them by id) and
    close and settle by `update_ledger` run on the shadow ledger (no threshold, no lean);
    `awaiting_results` includes them. Every new trade records `threshold`,
    `rule`, `p_source` and `strategy` (`ENTRY_FIELDS`; the 4 E0 trades of 5 Oct predate them).
    Lean trades opened before drivers existed got `z_market`, `driver` and `driver_from:
    "odds_log"` once as an update (`_lean_driver_update`). Entry fields never change; closes
    (from the odds log's last quote before kickoff: `close_odds`, `close_prices`,
    `close_fetched_at`, `close_minutes_before`, `clv_dk`, `beat_close_dk`) and settlement are
    update events (`UPDATE_FIELDS`).
  - Settlement: football-data's result when it has one, else ESPN's; every settle records
    `result_source`. When football-data later arrives for an ESPN-settled trade, one update adds
    `result_check` ("ok"/"mismatch"), `check_score` and (match trades) `clv_pinnacle`; a mismatch
    prints "Result check"/"Corner check". `awaiting_results(...)` lists trades waiting;
    `cli.espn_settlement` fetches them before `paper.run`.
  - Corners: `update_corner_ledger(..., espn=)` per corner league: each book's newest quote ≤ 3 h
    old and before kickoff, `corners_live.pick` on the card's cdf; one trade per match, team, line
    and book ever (id `<code>|<season>|<home>|<away>|corners|<home/away>|<line>`, `|dk` for
    DraftKings). `new_corner_trade` = `new_trade`'s shape with `market: "team_corners"`,
    `bet_type: "corners"`, `portfolio: "corners"`, `strategy`, `team`, `team_side`, `line`, `side`
    (over/under), `odds`, `model_p` (P(win)), `p_push`, `edge`, `threshold`, `rule`, `p_source`,
    `look` (the quote's snapshot), `bookmaker`, `model_mean` (`CORNER_ENTRY_FIELDS`). Close = the
    newest quote at its line before kickoff (`close_odds`, `close_prices {over, under}`,
    `close_fetched_at`, `close_minutes_before`, `close_snapshot`; CLV = entry odds × Shin-fair
    close − 1). A moved match or no result in 14 days: void. Ledger
    `paper_trades/<code>_corners_<season>.jsonl` (`load_corner_ledger`, `append_corner_events`).
    Pinnacle trades close on Pinnacle's quotes (`clv_pinnacle`,
    `beat_close_pinnacle`); DraftKings trades on DraftKings' (`_dk_close`: `clv_dk`, null on a
    one-sided close) plus Pinnacle's at the same line (`_pinnacle_ref`: `pinnacle_close_prices`,
    `clv_pinnacle`). `CORNER_UPDATE_FIELDS`. A corner failure costs only the corners section.
  - `run(...)` → `portfolio`: `rules[code]` (the 12% dict plus `sigma` and `lean`), `rule` (E0's),
    `strategy_order`, `portfolios` (`portfolios_section`: per portfolio `id, name, status, note,
    live, backtest`). Moneyline `live.strategies` (`match_strategies`) = `[{key, label, rule,
    summary, breakdowns, by_league, by_driver (Lean), trade_ids}]`, Lean first, beside the combined
    `trades`/`summary`; Lean has `driver_note`. Lean's section is "Blend Lean (new rule)"
    (`LEAN_NEW_LABEL`) over `new_rule_trades` (real Lean trades inside the range: the new rule
    backcast from Lean's start) with `backcast` (`lean_backcast`: `{applied, until, trades,
    settled, from, to, lean_started, note, new_rule, old_rule, cut, descriptive}`, the last
    three pre-rule summaries); the combined `trades`/`summary` keep every Lean
    trade. Right after Lean: `lean_shadow_section` (key
    `lean_all_odds`, "Blend Lean, all odds (old rule)") over `old_rule_trades` (every shadow
    plus every real Lean trade on a match without one), with `variant_of: "lean"`, `adds:
    "shadow"`, `filter: None`, `defined_at` = `LEAN_RANGE_FROM`, `note`, `shadow_trades`
    (count) and `since_defined` = `{defined_at, parent (real Lean, rule_version 2), variant (old
    rule: shadows + rule_version 2 trades), old_code, clv_diff: {clv_dk, clv_pinnacle}}` (each
    `clv_difference` with kept = in range, plus `kept_is`) and `before_defined` (the same
    shape for trades opened before the rule, `descriptive: true`). Moneyline's live section also has
    `shadow_trades` (the entries, newest first), never in its `trades` or summary. Corners `live.strategies` (`corner_strategies`),
    Pinnacle first, each followed by its shadow variants (`with_variants(sections, trades,
    variants, of)`, generic for any strategy list; `variant_section`): a variant is the parent's
    trades passing `filter`, no ledger rows of its own, with the usual section fields plus
    `variant_of`, `filter`, `defined_at`, `rule` (the parent's plus `variant_of`, `filter`,
    `defined_at`) and `since_defined` (`variant_since`: `{defined_at, parent: {trades, summary},
    variant: {trades, summary}, clv_diff: {clv_pinnacle, clv_dk}}` on trades opened at or after
    `defined_at`; `clv_difference` = mean of the kept minus mean of all, on the summaries' CLV
    trades (won or lost with a value; pushes and voids counted as `left_out`), 95% range
    resampling matches, 2,000 draws, seed 0, `{field, trades, left_out, kept, matches, mean_all,
    mean_kept, diff, ci95, draws}`). A failing variant is `{key, label, variant_of,
    filter, defined_at, error}`. `by_league` per section when trades span 2+ leagues. Corners' backtest =
    `corners_research()` (`kind: "research"`, from `lab/markets_research.json`).
    `strategy_backtests.lean` / `.edge12` = the backtest files via `app_section`. Every
    `strategy_section` (both Moneyline and both corner strategies) has `calibration` =
    `trade_calibration`: settled won/lost trades' `model_p` (a corner's given no push, ÷ (1 −
    `p_push`)) vs 1/odds vs won, `pushes`, `voids`, `by_league`, `note`; live bins (owner, 24 Oct)
    merge only under `LIVE_MIN_BIN` 3 trades and carry `few: true` under `LIVE_FEW` 10 (the
    backtests keep 20). Every `strategy_section`, variants included, also has `by_odds_band`
    (`live_odds_bands`: `bands.ODDS_BANDS`, every band kept, `{lo, hi, bets, share, settled, wins,
    win_rate, breakeven, roi, roi_ci95, clv_trades, clv, clv_ci95}`; `bets` = every trade, win
    rate and ROI per $1 on settled won/lost, CLV on trades whose close is final (`_close_final`:
    settled, or open with its kickoff at or before the build's `now`; voids out), ranges
    resampling matches), `odds_bands` = `{clv_field, note}` (`live_clv_field`: `clv_pinnacle` on
    both corner tracks, `clv_dk` on Moneyline) and `by_league[code].by_odds_band` when the
    trades span 2+ leagues. `now` threads from `run` through `portfolios_section`,
    `corner_strategies`, `with_variants` and `variant_section`. Every `strategy_section`
    (variants included) also has `sizing` (`sizing.live_sizing` on the section's own trades and
    rule; `{error}` on failure). Model 12% trades record `p_blend` (the card's blend `p_bet` for
    their side) and `p_blend_from: "open"` since 24 Oct; older ones get it once as an update
    (`_blend_update`: the logged entry quote's `p_bet` = "odds_log", else the league's current
    coefficients on its `fair` and `p` = "recomputed" when that fit was made before the trade
    opened (`lean_rule`'s `fit_made_at`), else "approx_current_fit" (look-ahead, flagged and
    counted in `sizing.blend_from`); a totals trade "not_h2h"; no quote = no chance, stake 0). Corners' live
    section has `quote_calibration` (`corners_live.quote_calibration`: per book the newest pre-kickoff quote
    of every logged team line, `p_model_over` and `fair_over` vs over won, from HC/AC or ESPN
    counts; half lines only, whole lines and pushes counted; `quotes`, `settled`, `unsettled`,
    `whole_lines`, `pushes`, `matches`, `model`, `price`; `{error}` on failure). `models` =
    `equations.section(data)`. Legacy
    `portfolio.live`/`backtest`/`player_model` stay until Record stops reading `player_model`.
  - Paper CLI lines: "Paper rule: …", "Match strategy <label>: N trades, M open, S settled",
    "Corner paper trades: …", "Corner strategy <label>: …".

**Player bets** (shots and the live player lines: Premier League; goalscorer research: five leagues, E0, SP1, D1, I1, F1; details and results in `docs/player_props.md`)
- `player_data.py` (Understat shots, name matching, season stats, FPL active players), `factors.py`
  (features from earlier matches), `models/player_counts.py` (NB2 shots/on target, start/sub
  mixture, `prob_over`).
- `player_backtest.py`: stage-1 walk-forward and `priced_trades` (`STRATEGIES`, `MAIN_STRATEGY =
  "blend_lineup"`); compact 12% rows in `E0_players.json` → `priced.strategies.<name>.trades`
  (`priced.trade_fields`); `score_by_season`. CLI `backtest-players --seasons 2023-now`.
- `player_calibration.py`: logit blend of implied and model, refitted every 28 days;
  coefficients in `E0_players.json` → `priced.calibration.{look,close}.coef`.
- `player_odds.py` (FanDuel live and historical, 3,000 reserve), `player_live.py` (live lines:
  candidates from the last 5 matches, transfers via FPL; `players_status` with
  `teams_without_history`, `moved_in/out`, `unmatched_odds`, `unmatched_names`, `blend_note`; `p`
  = blend, `p_model` = raw; no coefficients = no picks).
- `models/player_goals.py`, `player_goals.py`: anytime goalscorer (`GOAL_FACTORS`, walk-forward on
  `lab.harness`, benchmarks season goals and xG), saved as `E0_goals.json`. `lock_forward` drops
  appearances from `FORWARD_START` (2026-10-10) unless opened with a reason; the file carries
  `forward_window` and `holdout_log`.
- `player_goal_lab.py`: goalscorer research (candidates A–H, multi-league `goal-league`/`goal-pool`,
  B-cal recalibration, `forward_report` with the pooled gate at ≥150 matches). B is the primary.
- `player_goal_odds.py` (goalscorer probe and pilot; `CappedBudget`), `player_lab.py` (shot lines on
  the lab harness, development only), `player_segments.py` (segment search).

**Research lab and edge research** (`lab/`, `edge/`; rules and results in `docs/lab.md`,
`docs/edge.md`, `docs/totals.md`)
- `lab/harness.py`: `walk_forward` (refit every 28 days on earlier rows), `nested`, `Holdout`
  (raises `HoldoutLocked` until `unlock(reason)`, which logs the time). `lab/metrics.py`: log loss,
  Brier, calibration, blend weight `c` with bootstrap ranges, the 12% rule's CLV and ROI, `passes`
  (blend-weight range > 0 and CLV range > 0). `lab/features.py`, `lab/models.py`, `lab/run.py`
  (1X2 bake-off, any league; `--holdout-season`, `--coverage-only`, `--edge-only`).
- `lab/thresholds.py`: the learned minimum edge (`edge_threshold(bets)` → `{min_edge, confidence,
  method, n_bets, seasons, note, by_bucket, development_scan, check}`; null with a `note` when no
  band passes). Written into `E0_dk.json` (`edge_threshold`, `strategies.<raw|blend>.edge_threshold`,
  `edge_threshold_pinnacle`) and `E0_players.json`; per-league levels in `lab/min_edge.json`
  (`league_levels()`). Every level is null today.
- `lab/edge12_filters.py` (CLI `backtest-edge12-filters`) and `lab/band_edges.py` (CLI
  `backtest-band-edges`): the Model 12% rule research; neither passed.
- `lab/markets_research.json`: goal and corner research numbers for Record → Goals & corners,
  verbatim from `docs/totals.md` with run ids; edit it when a result changes.
- `edge/`: `signals.py`, `totals.py`, `corners.py` (bake-off 1), `corner_recal.py`, `corners2.py`
  (bake-off 2; its 2026/27 test is locked until `EARLIEST_OPEN` 3 Feb 2027), `corner_history.py`
  (historical corner prices: not available), `market_probe.py` (live market discovery, `--leagues`,
  `--coverage`, `--names`), `lineups.py`, plus line-shopping and prop helpers (`run.py`).
- Data warnings: football-data's average/maximum Asian handicap columns are unreliable (use
  `match_markets.clean_prices`; Pinnacle's are fine). football-data's 2025/26 files have Pinnacle
  1X2 prices for only about half the season (E0 52%, E1 47%, E2/E3 30%): check coverage before
  fitting or pre-registering on 2025/26.

**Site and CLI**
- `publish.py`: builds `data.json`, `players_stats.json`, `players_stats_<code>.json` and the site.
  No Odds API calls except through the modules above.
  - Per live league `league_fixtures` → `(cards, source, block)`; UCL via `cup_league_fixtures`.
    Every card has `league`; `data.json` has `league_code`, `leagues` (`[{code, name, live,
    fixtures, odds}]`; an off league without fixtures is left out, `league_list`), `odds_sources`,
    `teams_by_league[code]` (`team_block`; a failure drops only that block), and
    `players_stats_by_league` (`league_season_stats`, SP1/D1/I1/F1 from Understat's cached file).
  - Names: `align_to_schedule` and `_season_teams` keep DraftKings events on scheduled cards;
    unknown pairs go to the source's `unmatched` list; the log prints "Duplicate fixtures: N".
  - `add_match_blend` (E0's `p_bet` from `E0_dk.json`), then `add_lean(data, match_blends())`: per
    priced card with a league fit, `p_bet` home/draw/away from that fit, `z`, `z_market`,
    `lean_pick` (None on `low_data` cards or without a fit; with `in_range` (bool) and
    `odds_range` [1.50, 3.00] since 24 Oct: `in_range` false = the pick opens no trade); `data.match_blends` = `{generated_at,
    note, leagues: {code: {coef, sigma, matches, fit_from, fit_to, fit_date, refit}}}`. Log
    "Match blends: … Lean picks: N".
  - Cards also carry `goals_cdf` (P(team goals ≤ k), k = 0..6), `total_goals_cdf` (P(home + away
    ≤ k) from the full score matrix), `team_totals` (`add_team_totals`, file reads only: `{book:
    "fanduel", fetched_at, home/away: [{line, fair_over, over, under}]}`, the newest FanDuel quote
    per side and line before kickoff), `corners` (`add_corners`: model, then the fetch on the
    default branch, then `add_quotes` per book; each step in its own try), `team_news` (ESPN, not
    the top-level FPL `team_news` dict) and `news` (FPL absences, E0, applied to the model).
  - `markets_research` (`lab/markets_research.json` + `team_total_progress`, counts only).
  - Every optional step is wrapped so it can't stop the build.
- `cli.py`: `soccer-stats` commands (publish, log-odds, log-news, log-team-news, log-team-totals,
  log-corners, log-cup-odds, paper, close-plan, estimate-credits, estimate-team-totals,
  estimate-corners, estimate-cups, fit-match-blends, backtest-dk, backtest-lean, backtest-edge12,
  backtest-edge12-filters, backtest-band-edges, backtest-players, backfill-odds,
  backfill-player-odds, backfill-cup-odds, match-markets, team-totals-report, espn-probe,
  goalscorer-pilot, player-lab, goal-lab, goal-league, goal-pool, player-odds-check,
  player-segments, corner-history-probe, cup-corners). `cup_and_league_results` gives the
  paper step football-data and ESPN cup results (tagged `result_source`).

## App (`web/`)

- One vanilla JS file (`app.js`), plus `style.css`, `index.html`, `sw.js`. Name "Soccer Model";
  the header subtitle names the league, or "N leagues".
- Bump the cache name in `sw.js` (`soccer-model-vNN`, now v54) on every web change, and
  `APP_VERSION` in `app.js` with it (the smoke test checks they match), or phones keep the old
  app. Updates reach a home-screen app without killing it: `sw.js` skips waiting and claims
  the page; `app.js` reloads once on `controllerchange` (not on the first install; `reloadOnce`
  can't loop), calls `registration.update()` when the app comes back to the front and on ↻, and
  on ↻ also reloads when the deployed `sw.js` names another version (`newerAppDeployed`). The
  header subtitle ends "App vNN". The smoke test's `updateCheck` runs with the service worker
  allowed: first install no reload, a new `sw.js` one reload, ↻ on a newer `sw.js` one reload.
- Tabs: Matches; Teams (per league, with Players season stats); Record (Match bets, Goals &
  corners, Player shots); Portfolio; Explore.
- Competition filter (`leagueFilter`, localStorage `league`): chips for every league with fixtures
  or trades (on Moneyline → Backtest also every league in the backtest files); `LEAGUES` names;
  "xG" only in Understat leagues (`XG_LEAGUES`), else "exp. goals".
- **Matches** (with `portfolio.strategy_order` containing `lean`, `leanOn()`): cards show only the
  Lean badge (`leanPick(fx)` = the card's `lean_pick`; "Strong · 2.6σ: Away @ +650 · Market-driven",
  or "Would pick now" when its Lean trade is on another side) and the open Lean trade. Open
  trades are keyed by match and strategy (`tradeKey(home, away, strategy)`). The note counts Lean
  picks and says Model 12% runs under Portfolio. Started matches: "In play"/"Finished", no pick.
  Without `strategy_order` (older data) the 12% live test shows instead (`bestPick` on
  `pickProbs(fx)`: raw `p` under `fixed_raw`, else `p_bet || p`; must match `trades.best_pick`).
- **Match sheet**: Markets (Model, Blend `p_bet`, DK margin-free, Odds, Edge = blend × payout − 1,
  σ column from `fx.z` with "mkt ±xσ" from `fx.z_market`), `leanPickText`/`driverText` with
  `LEAN_CAVEAT`; Team news (FPL first, ESPN injuries not already listed, Confirmed XI or "Not out
  yet"); Goals (model total-goals over/under 0.5–4.5 from `total_goals_cdf`, a fold with each
  team's goals and FanDuel's margin-free over); Corners (`cornersSection`: per team expected
  corners, one row per line and book with Model, Fair, Over, Under, Edge; "Live-test pick" and
  "Paper trade open" marks matched by match, team side, line and book via `cornerTrade`;
  `WEAK_CORNER_FIT` note for SP1, D1, E1); Player shots (Premier League; player trades are off).
  Cards with `no_model` (UCL clubs no league covers) show "No model chance: odds only"
  (`noModelHtml`).
- **Portfolio**: switcher (localStorage `pfId`; retired Player shots behind a link), status and
  note, competition filter, Live / Backtest, then "Paper strategies" (`strategyList`; Moneyline
  Lean then Model 12%, selection `pfStrat`; Team corners Pinnacle then DraftKings, selection
  `pfStratCorners`). Live (`liveStrategyHtml`): tiles counted from the strategy's own trades after
  the filters (`tradeStats`), the <200-settled caveat, paper.py's ROI range only when its summary
  covers exactly the trades shown, Lean's driver panel (`by_driver`, filter `pfDriver`), "Profit
  over time" (`curveChart`: scrubbable, see below), "Where the bets sit on the odds" (owner, 24
  Oct; `liveOddsBandsHtml`: the backtest's `oddsBandChart` on the section's `by_odds_band`, or
  `by_league[lg].by_odds_band`, rows `lob-<key>-<lg|all>`, CLV on the section's
  `odds_bands.clv_field`; the ROI table; on Moneyline a table beside the same strategy's
  backtest bands, "In the backtest … CLV was x"; a note that live samples are small and the
  backtest bands answer the edge question; corners have no priced backtest, so no
  comparison), Open / Awaiting result / Settled lists (`tradePhase`:
  upcoming, in play under `IN_PLAY_MIN` 135 min, awaiting, settled; started trades show their
  close and CLV), the rule note. Shadow-filter variants (owner, 23 Oct; any live strategy
  with `variant_of`, today the corners' `pinnacle_below60` / `draftkings_below60`): an
  indented "↳ below 60%" row under the parent (`pfStrategies` files it there; Live only,
  Backtest and Model show the parent), its own trades and tiles via `trade_ids`, one box
  (`sinceDefinedHtml`) with a descriptive "Whole period, from <first trade>" part on top
  (the variant's and parent's full summaries, and the CLV difference kept minus all computed
  in the app from the trades' `clv_pinnacle`, won or lost with a value (as the summaries), with n; "includes the
  trades the 60% cut was chosen from, so it can't prove the filter") and the pre-registered
  "Judged from <date>" part below: `since_defined` variant vs parent, each
  `clv_diff` difference and range with `trades`, `kept`, `matches`: `clv_pinnacle` is the
  judged measure on both corner tracks, `clv_dk` a descriptive line on DraftKings'; "Not
  enough trades yet" under `JUDGE_MIN` 30 kept; N in "picked after the first N settled
  trades" = the parent's settled trades opened before `defined_at`; an `error` variant shows a banner); with a parent or its variant selected, "Profit over time" is
  `overlayCurveChart` (parent solid, variants dashed, end labels, legend, a "filter set
  (judged from here)" marker at `defined_at`; readout "Sat 18 Oct · All +$23.40 · Below 60%
  +$31.10 (difference +$7.70)"). The fixture builds them through `paper.corner_strategies`, moving `defined_at` to 20 Sep
  (`FIXTURE_DEFINED_AT`; the real 23 Oct is after its `NOW`). Corner CLV is the trade's own book's (`tradeClv`). Backtest
  (`strategyBacktestHtml(bt, lg, key)` for `strategy_backtests.lean` and `.edge12`): verdict, units
  tiles, per-league ROI and CLV range plots, seasons, coverage and fits, bet list; UCL (`kind:
  "close"`) gets `closeVerdict`; Model 12% under the Premier League shows the DraftKings backtest
  first. Bands (owner via the Research lab, 23 Oct; `backtestBandsHtml`, in the full and brief
  views, from the part's `by_odds_band`, `by_edge_band`, `by_clv_band` and the file's `bands`):
  "Where the bets sit on the odds" (`oddsBandChart`, rows `ob-<key>-<lg|all>`: share bar,
  CLV dot and range with 0 marked, "no bets" for empty bands; ROI, range, bets and won vs
  needed in a table below, not plotted), "Claimed edge vs what the price did" (`edgeClvChart`,
  `ce-…`: x = the band's mean `claimed`, y = CLV with range, dashed y = claimed and y = 0; a
  verdict from `edgeVerdict`/`bandTrend`, the bet-weighted slope and the first and last bands
  agreeing; a table with claimed, σ for Lean, CLV, ROI, bets) and the fold "Does beating the
  close predict profit?" (`clvProfitFold`, a `rangeChart` of ROI by CLV band, `cb-…`). UCL Lean
  (0 bets) keeps its note and draws none; UCL's CLV says "Pinnacle's close at the same moment". Corners' backtest is the research view (`researchBacktestHtml`). Model (third view,
  Moneyline and Team corners only; `modelViewHtml(cur, x, lg)`): equation cards from
  `portfolio.models` (`MODEL_KEYS`: Lean → `blend` then `match`, Model 12% → `match`, corners →
  `corners_f`; `equationCard`: equation lines, terms folded, the chosen competition's fitted
  numbers in words via `modelNumbers`, or a per-league table under All via `modelTable`); for
  Lean the blend's shape (`shapeChart`: p_bet's home chance against the market's fair one,
  draw and away sharing the rest 45 : 55, the model equal / 10 points higher / lower, plus the
  diagonal; under All the Premier League's fit); then "Lined up with results": (a) every
  outcome (`reliabilityChart`, SVG, x predicted, y happened, model ring / blend dot / price
  tick with Wilson ranges, from `strategy_backtests.<lean|edge12>` → `calibration.all_outcomes`,
  `all` under All), (b) the strategy's own backtest bets and (c) live paper trades
  (`binRowsChart` rows: said ring, break-even tick, won dot and range, blue when it won at least
  as often as the price implied; live bins merge only under 3 trades and a `few` bin, under
  10, is greyed and labelled "few trades"; `calibration.bets`, the live section's `calibration` and its
  `by_league`), (d) corners: every logged half line per book (`quote_calibration[book].model`,
  with the model's and the price's slopes). Slopes in words (`slopeWords`: 1 = right, below 1
  over-confident). Corners also get model (f)'s shape (`cornerShapeChart`: the chance of over
  3.5 / 4.5 / 5.5 corners against the team's expected corners, 2 to 9, NB2 via `nbOver` with the
  league's `alpha_home`; under All the Premier League's fit) and the diagonal reliability chart
  over every logged half line (model (f) ring, the book's fair price tick), with the per-bin rows
  folded below it. Every chart scrubs (see below). Missing blocks or `error` give a plain note; the data files' own notes are
  folded under "How this is measured". On Moneyline's Model view the chips list every league in
  the backtest files, as on Backtest.
- **Scrubbable charts** (owner, 23 Oct; every chart since v52): a wrapper with
  `data-scrub="<id>"` (role slider, focusable, `touch-action: pan-y` so vertical swipes still
  scroll the page); `scrubCharts[id]` = `{n?, readout(i), readoutId, at}` plus how a pointer
  finds a stop: `xs` (nearest by x; `points(i)` place the `.cdot` circles and the `.cursor`
  line follows), `pick(x, y)` (point charts, SVG units) or `rows: true` (the `[data-stop]`
  element nearest the pointer's height; Up/Down step too). Any `[data-stop="i"]` element gets
  `.sel` while stop i is on. Delegated pointer events (`scrubIndex`, `scrubTo`; the readout and
  legend inside a wrapper are ignored): a mouse scrubs on hover, a finger or pen while pressed,
  release keeps the spot; Left/Right/Home/End step when focused. No tooltips: the readout line
  follows. The hit area (`scrubPad`) spans the whole SVG, so a pointer past either end selects
  that end. Charts:
  - line (`xs`): "Profit over time" (`curveChart`, id `cv`; `settledCurve` days carry
    won/lost/void; readout "Sat 18 Oct · +$23.40 to date · that day +$8.20 on 5 trades (3–2)",
    the latest day by default; its $0 origin is a stop too, labelled with the first trade
    opened, `firstOpened`, readout "Thu, Oct 9 · first trade opened · …"), `overlayCurveChart`
    (same id), Record's running profit (`profitChart`, id `pc`, readout under the chart), the
    blend's shape (`shape-<code>`, 1-point steps 4–94%, a dot on each of the three curves) and
    model (f)'s shape (`cshape-<code>`) and the backtest's claimed edge vs CLV (`ce-…`);
  - point (`pick`): `reliabilityChart` (`rel-…`, `qrel-<book>`): one stop per bin of chances
    holding every series' point, nearest by x then distance, all series' marks highlighted and
    compared in one readout;
  - row (`rows`): `rangeChart` (per-league ROI/CLV, corner development, Record → Goals &
    corners, the backtest's CLV bands), `binRowsChart` (bets and live trades), `oddsBandChart`
    (`ob-…`), `edgeBucketsHtml` (`eb-<pfId>`) and the
    Teams players chart (`devChartHtml`, id `dev`, bars as stops).
  Not charts: the match card's 1X2 bar, the Teams table's net bars and the team-total progress
  bar (numbers printed beside them); Record's threshold sweeps are tables.
- **Record**: Match bets ("Against DraftKings' prices" from `E0_dk.json` strategies, then the
  Pinnacle replay; other leagues get a note), Goals & corners (`data.markets_research`: verdicts
  and `rangeChart` per league, team-total progress), Player shots (strategy switch, threshold
  sweep, predicted vs actual).
- Live trade fields the app reads (Moneyline contract): per trade `close_odds`,
  `close_fetched_at`, `close_minutes_before`, `clv_dk`, `beat_close_dk`; summary `clv_dk`,
  `beat_close_dk`, `close_early`. A close over 60 min before kickoff is "approx."
  (`CLOSE_APPROX_MIN`). Missing fields show "–". Not mirrored from `paper.update_ledger`: the
  3-hour odds freshness gate (the app flags on older odds and shows the odds age).
- Teams: `teams_by_league[code]` with a chip row (`teamsLeagueChips`; "All" shows the Premier
  League); season stats per league from `players_stats_<code>.json`, loaded on demand
  (`state.psBy`; the smoke test fails if a file is fetched twice); no UCL block. The shot model,
  player lines and the player sheet are Premier League only.
- Edge panel: under Lean, "Blend Lean rule" (`leanEdgeRec`); under the fixed test `fixedEdgePanel`
  with the learned finding folded; the learned-level machinery (`edgeRule`, `edgeInfo`,
  `edgeBucketsHtml`, `edgeChartSource`, `noEdgeText`) still serves `edge_threshold` data
  (`by_bucket[{edge_lo, edge_hi, n, implied, model, realized, realized_lo, realized_hi, roi, roi_lo,
  roi_hi}]`; null `min_edge` shows its `note`).
- Test data: `tests/fixtures/web/` (all JSON files together under 1.5 MB: the cap in
  `tests/test_web_smoke.py` was raised from 1 MB on 24 Oct rather than trim more real data). Rebuild with
  `uv run python tests/web/make_fixture.py` (synthetic leagues through `publish.build_data`, the real
  ledger and backtests from `origin/data-log` through `paper.run`; fixed `NOW`, reproducible).
  To stay small it trims lists (`LEAN_BETS`, `EDGE12_BETS` 30, `DK_BT_TRADES` 20,
  `PLAYER_BT_TRADES` 25, `SAMPLE_PLAYERS`); `add_models` rebuilds `portfolio.models` once every
  block is in (the synthetic leagues' fits via `publish.safe_fit_info`, data-log's real
  `match_blends.json` for the blend's numbers) and gives the corners live section a synthetic
  `quote_calibration` (the fixture logs no quotes); synthetic blocks (`add_lean_strategy`, `add_corners`,
  `add_champions_league`, `STARTED_OPEN`, `add_second_league`) cover states the real data lacks.
- Smoke test: `node tests/web/smoke.mjs [--shots DIR]` visits every tab, view and sheet at 390px
  light and dark and at 1280px; fails on console errors, horizontal scroll, or "undefined", "NaN"
  or "${" in the text. `tests/test_web_smoke.py` runs it under pytest (skips without node or
  Chromium at `/opt/pw-browsers/chromium` or `CHROMIUM`).

## Agent team (`.claude/agents/`)

| Agent | Owns |
| --- | --- |
| `ui-designer` | `web/*` and the display fields in `publish.py`. Screenshot-tests at 390px, light and dark. |
| `player-props` | Every player market (shots, on target, anytime goalscorer, assists, cards): models, backtests, calibration, live player lines and new player ideas (`player_*.py`, `factors.py`, `models/player_counts.py`, `players.yml`). Formerly `player-shots`. |
| `moneyline` | The match model and match bets (`models/dixon_coles.py`, `backtest.py`, `odds*.py`, match parts of `trades.py` and `paper.py`, `backfill.yml`). |
| `research-lab` | The shared evaluation harness and pass rules (`src/soccer_stats/lab/`, `docs/lab.md`), the machine-learning model bake-off, and signal/market research (`src/soccer_stats/edge/`, `docs/edge.md`, `odds-check.yml`). Proposes; owners build. Formerly `edge-finder`. |
| `lead-reviewer` | Reviews and merges specialist branches, CI, CLAUDE.md, README, `docs/history.md`, the work plan, and credit budgets. |

**Before every task** (every session, every time):
1. `git fetch origin claude/soccer-stats-scaffold`, then read "Current state" in CLAUDE.md from
   `origin/claude/soccer-stats-scaffold` (`git show origin/claude/soccer-stats-scaffold:CLAUDE.md`),
   not from memory or from an old branch.
2. Cut `team/<name>` fresh from the default branch (or merge the default branch into it first).
3. A long-running session re-reads before each new task: its earlier context may be stale.
4. If something in the notes looks wrong, tell the lead rather than acting on an older belief.

Process:
- Specialists work as separate cloud sessions on `team/<name>` (`team/moneyline`,
  `team/research`, `team/player-props`, `team/ui`). They push only their branch, never merge,
  never push `data-log` by hand, and open a pull request into `claude/soccer-stats-scaffold`.
- Each PR updates the parts of CLAUDE.md it changes (code map, App, and "Current state" when a
  live fact or verdict changes). Dated round notes go in the PR body and, if worth keeping, in
  `docs/history.md`, not in CLAUDE.md.
- Only the lead sets Odds API credit caps. The default cap is 0.

## Lead handoff

The lead runs in the owner's main session ("Soccer stats prediction model"). Past rounds are in
`docs/history.md`.

**Team sessions** (message with claude-code-remote `send_message`, check with `get_session`):

| Session title | Session id | Branch |
| --- | --- | --- |
| Soccer team: Moneyline | session_01SHL1YZ5knBRSduL7vurWN7 | team/moneyline |
| Soccer team: Research lab | session_019c4cFKhKCuU6mdEtYqoUxp | team/research |
| Soccer team: Player props | session_01C8B5tL2iAqVcZ7cYbz2bdc | team/player-props |
| Soccer team: UI Design | session_01YUi3o1ueWm8BePMSWeB2hb | team/ui |

**Process:**
- The lead reviews each PR (diff, CI on the PR, local checks), leaves a review comment (GitHub
  won't take a formal self-approval: all sessions post as the owner) and merges ONLY through the
  GitHub API (`merge_pull_request`, method `merge`). A local merge pushed to the base shows the PR
  as Closed instead of Merged.
- After merging, run `players.yml` / `backfill.yml` (0-credit inputs) if backtests changed, then
  `publish.yml`. Keep "Current state" and the work plan's "Status and next steps" current.

**Owner preferences:**
- Plain English, no branch names in parentheses in session titles or summaries.
- Session titles follow "Soccer team: <Role>", like the baseball team.
- One shared Odds API key on the 100K plan (the owner decided against two keys on cost).
  Baseball usage is small in its post-season, which ends late October.
- Credit caps are 0 unless the owner approves a spend.

**Open threads:**
- Player shots are parked (owner, 9 Oct): no new work or spend; the free display and weekly
  model keep running. A ≤20-credit check for any book offering unders may run every few
  months, only when the lead asks.
- Name watch: check each publish log's "Duplicate fixtures" and "odds events unmatched" lines; a
  promoted or relegated club needs its spelling checked (`odds-check.yml` `task=markets` /
  `task=espn` with `leagues=…`, free).
- Housekeeping: a step-level time limit on the Playwright install in `ci.yml` (it hung twice);
  drop the legacy `portfolio.live` / `portfolio.backtest` keys once Record stops reading
  `player_model`.

## Conventions

- Checks before every push: `uv run ruff format src tests && uv run ruff check src tests && uv run
  pytest -q` (CI runs the same; `.venv/bin/pytest` if `uv` isn't on the path). JS: `node --check
  web/app.js`, and after web changes `node tests/web/smoke.mjs` (regenerate the fixture first if
  `data.json`'s shape changed).
- No look-ahead anywhere: features, refits, calibration, filters and settlement use only earlier
  kickoffs. Tests check this.
- Results out of sample, with bet counts and 95% ranges; pre-register before any run; losses
  reported.
- Commit messages: imperative summary, a body explaining why, and the attribution lines the
  session gives.
- App text is plain English for a non-specialist. Money in $ for portfolio views and units for
  the replays. Odds American plus decimal.
- Charts: blue `--pos` and red `--neg`; a readout line instead of tooltips; colour tokens for both
  light and dark themes.
