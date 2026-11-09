# Player props: anytime goalscorer and a costed odds plan

Owner: player-props. Round 4 (2026-10-06). **No API calls were made and no credits were
spent.** Every number below comes from The Odds API's pricing rules, the round-1 probes in
`docs/edge.md`, and the cached FanDuel history on `data-log`. Spending anything needs the
owner's approval and a cap set by the lead.

## 1. Anytime goalscorer model (free data)

- Code: `models/player_goals.py` (the model) and `player_goals.py` (features, walk-forward,
  scoring).
- Output: `E0_goals.json` on `data-log`, written by the weekly `players.yml` run.
- Data: Understat. Per match: goals (own goals excluded), xG, shots, minutes, starts.

**Model.** Goals per match are a regularised negative binomial count (Poisson in the limit),
with mean = minutes / 90 × exp(β · factors). The factors (`GOAL_FACTORS`) all come from
matches that kicked off earlier:

- his shrunk xG per 90, including penalties;
- his goals / xG, shrunk toward 1 by 8 xG;
- his shrunk shots per 90;
- penalty duty and position;
- the match model's expected goals for his team;
- the opponent's shots conceded;
- home or away, and expected game state.

P(scores ≥ 1 | plays) mixes "starts" and "comes on", weighted by his chance of starting, as
for shots. With the lineup known, that chance is 0 or 1. Bets on non-players are void.

**Test (stage 1).**
- Walk-forward, refitted weekly on the previous two years.
- Players need at least 3 earlier appearances.
- Two benchmarks:
  - his goals per appearance so far this season;
  - his xG per appearance so far this season.

  Each is converted to P(≥1) as 1 − e^(−mean), with the position average before his first
  appearance.
- Scores: log loss, Brier, and a calibration table (predicted vs scored by bucket).
- Ranges: the 95% range of the log-loss difference, resampling whole matches.
- Splits:
  - **development** (2023/24–2024/25);
  - **holdout** (2025/26, locked: the specification was fixed before any real data was
    scored, and nothing is tuned on it);
  - **live** (2026/27).
- Gate (reported, switches nothing on): the model beats both benchmarks on the holdout
  before lineups, with the upper end of both ranges below 0.

**Results.** Real numbers arrive with the next `players.yml` run (no credits). The sandbox
can't reach Understat. On simulated data with lasting finishing quality, the model beats the
season-to-date goals benchmark, with its range below 0, and is calibrated within 3 points.

**Harness.** The research lab's shared harness (`src/soccer_stats/lab/`) isn't merged yet.
This evaluation follows its stated rules: time-ordered splits, a locked 2025/26 holdout and
match-resampled ranges. It moves onto the harness once the harness merges.

## 2. Who prices EPL player props on The Odds API

| Market | Known so far | Both sides? |
| --- | --- | --- |
| `player_shots`, `player_shots_on_target` | FanDuel (us); 1xBet (eu, 165 lines on 40 players in one snapshot); Kambi books BetRivers, BallyBet, Unibet, betPARX (about 50 lines); TABtouch (au, on target only) | **No.** Over only at every book, in all five regions (round 1, three snapshots) |
| `player_goal_scorer_anytime` | **Not yet probed.** US books usually list it for EPL (FanDuel, the Kambi books, possibly BetMGM and Caesars), and 1xBet in eu. Pinnacle and the Betfair exchange showed no EPL player markets in round 1 | Unknown. US books list "Yes" only. A two-sided book (Yes and No) would give a real margin and a fair price; none is known |

Assists (`player_assists`) and cards (`player_to_receive_card`) can go on the same probe calls
(each market adds 1 credit per region live, 10 historical), if the owner wants them checked.

## 3. Pricing rules used

- **Historical event odds:** 10 credits × markets × regions per call. A `bookmakers=` list of
  up to 10 books counts as one region. Empty snapshots cost less.
- **Live event odds:** 1 credit × markets × regions per call.
- **Event lists:** 1 credit per historical timestamp. The FanDuel backfill already cached the
  event lists for every match in its history, so re-using those event ids costs 0.
- **Cached FanDuel history** (matches with priced, modelled lines; close snapshot):

  | Season | Matches |
  | --- | --- |
  | 2023/24 | 44 |
  | 2024/25 | 355 |
  | 2025/26 | 342 |

  A full season is 380 matches.

All of this sits on a shared balance: 22,712 credits on 6 Oct (the baseball app uses the same
key). Live FanDuel lines keep a 3,000-credit reserve. **Every run below needs its cost plus
3,000 left at the start.**

## 4. Costed plan

### Stage 0: live probe (recommended first step), about 10 credits

- **Calls:** two live event-odds calls on a 2026/27 match day, all five regions, market
  `player_goal_scorer_anytime` (add `player_assists` for 5 more credits each). That's
  1 × 5 = 5 credits per call.
- **Answers:**
  - which books price EPL anytime goalscorer;
  - whether any lists both Yes and No;
  - how many players each covers.
- **Code:** the existing `edge/props.probe` (`odds-check.yml`, `task=props`) with the market
  list widened. About 30 minutes of work.

### (a) Goalscorer at the best price across books

| Step | What | Calls | Credits |
| --- | --- | --- | --- |
| Pilot | 5 cached 2025/26 matches (Aug, Oct, Dec, Feb, Apr), close snapshot, the books stage 0 found (one `bookmakers=` list), 1 market | 5 | 50 |
| Sample | 100 more 2025/26 matches with a cached close | 100 | 1,000 |
| Full holdout season | all 342 cached 2025/26 matches, close | 342 | 3,420 |
| Per season, close only | 380 matches | 380 | 3,800 |
| Per season, 3-hour look + close | | 760 | 7,600 |

### (b) Best over price on shots and on target across FanDuel, 1xBet and Kambi

This repeats `docs/edge.md` §2: 2 markets, so 20 credits a call.

| Step | Calls | Credits |
| --- | --- | --- |
| Pilot | 5 | 100 |
| Sample | 100 | 2,000 |
| Per season, close only | 380 | 7,600 |

**Combined.** One call with all three markets costs 30 credits, the same as the separate
calls. The saving is one budget guard and one cache file per match. A combined
pilot-plus-sample (105 matches) is about **3,150 credits**. With the reserve, it needs about
6,200 left at the start.

### Run details, fixed before any call (2026-10-07, round 5)

The owner approved stage 0 plus the (a) pilot, with a hard cap of 60 credits.

- **Command:** `soccer-stats goalscorer-pilot --cap 60` (`player_goal_odds.py`).
- **Where it runs:** `odds-check.yml` with `task=goalscorer` and `cap=60`. The job never
  pushes and never deploys; the results go to the job log and a 7-day artifact.
- **Order:** the free goalscorer chances come first (Understat and the match model, holdout
  scoring as pre-registered). The calls follow, and the analysis comes last.
- **Stage 0:**
  - live event odds for upcoming matches in kickoff order, all five regions, one market;
  - at most 5 credits a call (an empty answer costs 0);
  - it stops after two matches with prices, or after 8 tries.
- **Pilot:**
  - the first cached 2025/26 FanDuel close in Aug, Oct, Dec, Feb and Apr;
  - the same snapshot time (one minute before kickoff);
  - one `bookmakers=` list: the books stage 0 found, topped up to ten with the known
    player-prop books (fanduel, draftkings, betmgm, williamhill_us, betrivers, ballybet,
    unibet, betparx, onexbet, pinnacle). Up to ten books cost the same as one region, so
    the top-up costs nothing and doesn't depend on stage 0 finding a book this early;
  - at most 10 credits a call.
- **Guards:**
  - a call is skipped if its maximum cost would take the running total past the cap;
  - a call is also skipped if it would leave fewer than 3,000 credits on the shared key
    (the live reserve);
  - the counted cost is each response's `x-requests-last`.
- **Measures:** exactly those in §5, computed by `player_goal_odds.evaluate`.
  - Players are matched by name to Understat within the match's two clubs (their whole
    2025/26 squads).
  - "Starters" are Understat starters.
  - The model's chance is the stage-1 walk-forward with the lineup known (the close comes
    after lineups).
- **Verdict:** `player_goal_odds.verdict` applies §5 as written. The go/kill table was
  written for the 100-match sample. On 5 matches the coverage rules (a second book, a
  two-sided book) can be decided; a gap or ROI on about 5 matches is an early read with a
  wide range.

### Smallest useful spend

Stage 0 plus the (a) pilot is **about 60 credits**.

## 5. What would justify the full spend

These are fixed now, before any data. All are measured on starters at the close; ranges
resample whole matches.

**Goalscorer, prerequisite (free).** The model must pass its stage-1 gate on the 2025/26
holdout. If it doesn't beat the season benchmarks, no odds are worth buying. Then it also has
to be calibrated within 2 points per bucket.

**Goalscorer, measured on the sample:**
1. **The gap:** the average implied chance at the best price (1 / best odds) minus the actual
   scoring rate.
2. **Back-all ROI** at the best price.
3. The blend rule (logistic blend of price and model, as in `player_calibration.py`),
   walk-forward at a 12% edge. This gives about 0.2–0.5 bets a match, so it is reported but
   not decisive.

| Decision | Rule |
| --- | --- |
| **Go** to the full 2025/26 holdout (3,420 credits) | The starters' gap at the best price is 3 points or less, with the upper end of its range at 5 or less; **or** a book prices both Yes and No with a margin under 6% (then the model can be tested against a fair price) |
| **Kill** | The gap is above 5 points; **or** no book beyond FanDuel covers at least half the starters |

My prior: about 10–15%. Yes-only goalscorer menus usually carry a 20–30% overround, the
same problem as FanDuel's shots. A two-sided book is the only clear path.

**Shots best-over:** the go/kill rules in `docs/edge.md` §2 stand (go if the starters' gap
at the best price is ≤ 4 points, upper end ≤ 6). Its prior is about 15%.

**Recommendation.** Ask the owner for **60 credits**: stage 0 plus the goalscorer pilot.
Run it only after the goalscorer stage-1 gate passes on the next `players.yml` run. Ask for
the 1,000-credit sample only if the pilot finds a second book or a two-sided book.

## 6. Results: goalscorer probe and pilot (round 5, 2026-10-07)

- **Run:** `odds-check.yml` `task=goalscorer` `cap=60`, run 37574303057, 05:02 UTC.
- **Dry run:** run 37573676963 (`cap=0`) spent 0 and checked the pipeline first.

### Credits

| Call | Credits | Left after |
| --- | --- | --- |
| Live event list | 0 | 22,696 |
| Live, Arsenal v Leeds United (10 Oct), 5 regions | 5 | 22,691 |
| Live, Aston Villa v Brentford (10 Oct), 5 regions | 5 | 22,686 |
| Pilot, Liverpool v Bournemouth, 2025-08-15 close | 10 | 22,676 |
| Pilot, Bournemouth v Fulham, 2025-10-03 close | 10 | 22,666 |
| Pilot, Bournemouth v Everton, 2025-12-02 close | 10 | 22,656 |
| Pilot, Aston Villa v Brentford, 2026-02-01 close | 10 | 22,646 |
| Pilot, West Ham v Wolves, 2026-04-10 close | 10 | 22,636 |
| **Total** | **60 of the 60 cap** | **22,636** |

### Who prices EPL anytime goalscorer

**Live (3 days before the first 2026/27 matches).** 14 books price it:

- espnbet, bovada, fanatics, mybookieag, rebet, williamhill_us (Caesars), betmgm (us, us2);
- betrivers, betparx, ballybet, unibet (Kambi);
- tab, tabtouch, pointsbetau (au).

Each book covers 27–46 players a match.

- **None lists a "No" price.** Every line is Yes only, so no book gives a measurable margin
  or a fair price.
- FanDuel and DraftKings were not among them (none of the five regions returned them).
- 1xBet and Pinnacle were not among them either.

**Pilot (history).** The ten books asked for were the first ten found live. Stage 0 found more
than ten, so the known-books top-up didn't apply, and FanDuel was not asked for. Four books
returned prices at the close:

| Book | Matches | Players |
| --- | --- | --- |
| BetRivers | 5 of 5 | 91 |
| Bovada | 3 | 84 |
| ESPN BET | 2 | 54 |
| MyBookie | 1 | 32 |

Again, none had a No price.

### Name matching

- 141 of the 169 names (83%) matched the two clubs' Understat squads.
- Most misses are full legal names, e.g. "Francisco Evanilson de Lima Barbosa", "Igor Thiago
  Nascimento Rodrigues", "Emiliano Buendia Stati". The rest are players with no Understat
  appearance that season.
- `player_names.csv` overrides would fix the legal names if this market is ever traded.

### Measures

These are the pre-registered measures on Understat starters at the best of the four books'
prices: 83 starters, 5 matches, ranges resampling matches.

| Measure | Value |
| --- | --- |
| Implied chance at the best price (1 / odds) | 16.4% |
| Scored | 10.8% |
| **Gap** | **5.6 points (95% 2.3 to 8.8)** |
| Back-all ROI at the best price | −48.9% (95% −68% to −27%) |
| Starters priced by BetRivers / Bovada / ESPN BET / MyBookie | 86% / 58% / 41% / 13% |
| Model (stage 1, lineup known), 79 starters | mean 9.6%; log loss 0.267 vs the price's 0.285 |
| Model bets at a 12% edge | 2, both lost |

- The model's lower log loss is against a price with the margin in it, so it is no evidence
  of an edge.
- The FanDuel price lift couldn't be measured, because FanDuel wasn't requested.

### Verdict, by the rules in §5 as written: **kill**

- **The kill rule fires:** "the gap is above 5 points" (5.6).
- **The go rules don't:**
  - the gap is not at most 3 points;
  - there is no two-sided book.
- The pilot-step rule (ask for the sample if a second book or a two-sided book turns up) is
  overridden by the kill.

**Caveat, stated before the run** (§4 run details): the go/kill table was written for the
100-match sample. On 5 matches the gap's range (2.3 to 8.8) still includes values at or
below 5. The rule is applied as written regardless.

- **No 1,000-credit sample is recommended.** The owner can still choose one, knowing this
  caveat.
- The structural finding stands either way. Every book on the API prices goalscorer Yes
  only, at roughly 50% more than the scoring rate (16.4% vs 10.8%). That is the same wall
  as FanDuel's shot overs.

## 7. Player shots through the lab harness (round 5, development seasons only)

- **What ran:** `soccer-stats player-lab` (`odds-check.yml` `task=player-lab`, run
  37573679173, 0 credits) on `data-log:backtest/E0_player_lines.csv.gz`.
- **Holdout:** the 2025/26 holdout stays locked (only rows before 2025-07-01). It was not
  reopened for shots.
- **Same numbers locally:** the run in Actions reproduced the local run exactly.

**Why the market needs de-margining.** FanDuel lists overs only, so 1 / odds carries its
margin. The lab's rule needs a margin-free market and a fair close. Two things go wrong
without them:
- any calibrated model "adds information" against the raw price;
- price drift reads as CLV.

So the market's chance is FanDuel's price de-margined by a walk-forward logistic fit on
earlier lines (`player_lab.recalibrate`), and the close is de-margined the same way. The
raw-price numbers are kept beside them, labelled as not a valid test. On raw prices, both
3-hour strategies would "pass" with CLV of +3.0% and +2.6%. That is an artifact: their ROI
is −32% and −36%.

| Strategy (development 2023/24–2024/25) | Lines | Log loss model / de-margined price | Blend weight c (95%) | Bets at 12% | CLV vs de-margined close (95%) | ROI (95%) | Passes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Raw model, 3 h before | 53,552 | 0.334 / 0.352 | 0.84 (0.78 to 0.89) | 817 | −37.4% (−38.5 to −35.9) | −32% (−55 to −5) | no |
| Blend, 3 h before | 51,233 | 0.331 / 0.350 | 0.90 (0.82 to 0.96) | 286 | −37.3% (−38.8 to −34.8) | −36% (−74 to +12) | no |
| Starters after lineups (blend) | 36,123 | 0.366 / 0.372 | 0.75 (0.62 to 0.86) | 186 | – (bet at the close) | −15% (−75 to +58) | no |

**What it shows.**
- The shot model does carry information the price lacks:
  - it beats FanDuel's de-margined price on log loss;
  - it earns a large blend weight.
- But the 12% edges the rule finds are long shots (average odds about 15). There FanDuel's
  de-margined close is far below the price's break-even, so CLV is about −37%.
- Nothing passes, consistent with data-log's losing backtest.

**Consistent with data-log.** The weekly backtest's own trades (trade rule: best line per
player, at most 4 a match), development rows only:

| Strategy | Bets | ROI | Price-move CLV |
| --- | --- | --- | --- |
| Raw model, 3 h before | 510 | −28.3% | +3.4% |
| Blend, 3 h before | 189 | −35.9% | +3.5% |
| Starters after lineups | 125 | −11.6% | – |

The lab's simpler bet rule (every line at 12%, no cap) makes more bets with similar ROI.
The backtest's "CLV" is the raw price move, the same artifact as above.

**Goalscorer stage 1 on the harness.** `player_goals.walk_forward` now runs through
`lab.harness.walk_forward` with the lab's `Holdout`. Its predictions are identical to the
old loop (checked on simulated data, 2,874 rows). So the round-4 numbers on data-log stand,
and the weekly run logs each opening of the holdout.

## 8. Goalscorer model improvements: pre-registration (round 6, 2026-10-07, before any run)

The owner's call: keep Anytime goalscorer and improve the model. Credit cap 0 this round.

**What "better" has to mean.** The model is already calibrated on average. A model pays
only if it finds players the bookmakers underprice by more than their margin: the
pilot's best prices implied 16.4% for starters who scored 10.8%. So improvements are
judged on sharper, better-separated chances, where a bet would sit (starters, after
lineups), and above all on paired log loss. Averages matching is not enough.

### Data and split

- **Data:**
  - Understat EPL appearances 2021/22–2024/25 (2021/22 only warms up features and
    training);
  - the match model's walk-forward expected goals and game state (as in
    `backtest-players`).
- **Predictions:** walk-forward through `lab.harness`, refitted every 28 days on all
  earlier appearances (730-day window). 2022/23 is predicted only to tune H (nested); it
  is not scored.
- **Development (the only data that chooses anything):** 2023/24 and 2024/25.
- **Population scored:** starters (Understat), at least 3 earlier appearances, the
  lineup known (bets are placed after lineups). Candidate A is the one exception: it is
  scored before lineups, on the same rows.
- **2025/26:**
  - not clean any more, because it has been scored every week since round 4;
  - it chooses nothing;
  - after selection, the chosen candidate and B are reported there, labelled "seen".
- **Forward check (the verdict):** 2026/27 matches kicking off on or after
  **2026-10-10 00:00 UTC**.
  - They are locked with the lab's `Holdout` pattern from this commit on.
  - They are scored once, when at least 150 of those matches have been played (about
    early December), by a run that prints and logs the reason and time.

### Candidates (fixed now)

Every feature uses only earlier kickoffs. C–H start from B.

| # | Candidate | What changes |
| --- | --- | --- |
| A | Current model, before lineups | The live spec (`GOAL_FACTORS`), chance of starting from his history |
| B | Current model, lineup known | Same model; starters known (p_start = 1), expected minutes as a starter. **The reference for C–H** |
| C | + set-piece and penalty role | Shrunk set-piece xG per 90 (corners, set pieces, direct free kicks; no penalties); took his team's most recent penalty (0/1) |
| D | + opponent's defensive xG | Opponent's recency-weighted xG conceded per match over league average; opponent's share of xG conceded to his position over the league share |
| E | Share structure | His shrunk share of his team's xG in the matches he played, beside the team's expected goals for this match (replaces his own xG per 90) |
| F | + form at two speeds | Shrunk xG per 90 with a half-life of 4 appearances and of 40 (beside the current 10) |
| G | All of C, D, E (added, not replacing) and F | One regularised count model, same L2 |
| H | LightGBM, Poisson objective | G's features, minutes offset; nested grid: leaves {4, 8} × min child {100, 400}, 200 trees at 0.03 (4 configs; each season tuned on the season before) |

### Metrics

- **Primary:** paired log-loss gain per starter-row against the reference (A for B; B for
  C–H), with ranges resampling whole matches.
- Also reported:
  - Brier score, and its Murphy decomposition (reliability, resolution) over ten bins;
  - AUC;
  - **sharpness:** the share of starters given 30% or more;
  - **tail calibration:** predicted vs scored in the 20–30% and 30%+ buckets, with each
    bucket's 95% binomial range;
  - log loss against the season-xG benchmark.

### Tests, correction and pass bar

- **Tests:** 7 comparisons (B vs A; C, D, E, F, G, H vs B), so every range is **99.29%**
  (Bonferroni, 0.05 / 7).
- **Pass (development):** the gain's 99.29% range lies above 0, **and** the 20–30% and 30%+
  buckets each have predicted within their observed rate's 95% range (or within 3 points
  when the range is wider).
- **Tail rule, as coded before any run (`player_goal_lab.tail_rule`):** when a bucket's
  95% range is at most ±3 points, the predicted rate must lie inside it; when the range is
  wider, the predicted rate must be within 3 points of the observed one.
- **Selection:** among C–H that pass, the lowest development log loss is "the improved
  model". If none passes, B stays.
- **Forward verdict:** the improved model (or B) against B and the season-xG benchmark on
  the forward window. It passes if its gain over B has a 95% range above 0 and the same
  tail rule holds. If B was kept, the forward check confirms B's calibration only.

### What this can and can't show

- Sharper chances are necessary for a goalscorer edge, but not enough. With Yes-only
  prices about 50% above the scoring rate, the model has to find starters priced at 1.5×
  their true chance or worse in the other direction.
- No price test happens this round (0 credits). The 5 cached pilot matches are used only
  to describe where the model and the best price disagreed. **5 matches decide nothing.**
- A priced test is proposed, with a cost and a go/kill rule, only if the improved model
  passes on development.

### Run

- `odds-check.yml` `task=goal-lab` (`soccer-stats goal-lab`): print-only, never pushes, no
  credits.
- The 2026/27 forward window is dropped before any computation unless `--open-forward`
  is given with a reason.

## 9. Goalscorer improvements: results (round 6, 2026-10-07)

- **Runs:** `odds-check.yml` `task=goal-lab`, run 37661673868 (and run 37660385945, whose
  pilot step failed: `gh` refused to print the log).
- **Cost:** 0 credits.
- **What was scored:** development 2023/24–2024/25 only; the forward window stayed locked.
- **Rerun differences:** the two runs differ only in the fifth decimal (C's gain −0.00006
  both times; D −0.00009 / −0.00010; G's resolution 0.01052 / 0.01049). The verdicts are
  the same.

### Development: 2023/24–2024/25, starters with at least 3 earlier appearances, lineup known

Each candidate's starters are scored against its reference: A for B, B for C–H.

| # | Log loss | Gain vs reference (99.29% range) | Brier | Resolution | AUC | Share 30%+ | Tail rule | Passes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Season-xG benchmark | 0.3160 | – | – | – | – | – | – | – |
| A current, before lineups | 0.2900 | – | 0.0850 | 0.0099 | 0.779 | 3.8% | no (20–30%: 24.3% predicted, 29.9% scored) | – |
| B current, lineup known | 0.2880 | +0.0020 (−0.0002 to +0.0040) | 0.0844 | 0.0105 | 0.781 | 7.6% | yes | no |
| C + set pieces, penalty taker | 0.2880 | −0.0001 (−0.0002 to +0.0001) | 0.0845 | 0.0106 | 0.781 | 7.5% | yes | no |
| D + opponent xG conceded | 0.2881 | −0.0001 (−0.0002 to +0.0001) | 0.0844 | 0.0106 | 0.780 | 7.6% | yes | no |
| E xG-share structure | 0.2875 | +0.0005 (−0.0003 to +0.0012) | 0.0843 | 0.0106 | 0.782 | 7.3% | yes | no |
| F + form at two speeds | 0.2874 | +0.0005 (−0.0000 to +0.0011) | 0.0843 | 0.0106 | 0.782 | 7.5% | yes | no |
| G all of C–F | 0.2875 | +0.0005 (−0.0001 to +0.0011) | 0.0843 | 0.0105 | 0.782 | 7.5% | yes | no |
| H LightGBM (nested) | 0.3020 | −0.0141 (−0.0167 to −0.0112) | 0.0863 | 0.0101 | 0.779 | 9.2% | no | no |

**Verdict, by the pre-registered rule: nothing passes, so B stays.**

- **Lineup known (B vs A):** knowing the lineup is the biggest lever, as expected. It
  doubles the share of starters given 30%+, from 3.8% to 7.6%, and fixes A's
  under-prediction in the tails. But its log-loss gain (+0.002) misses the Bonferroni bar
  by a hair. B is still how the model would be used after lineups.
- **C, D:** set-piece and penalty role, and the opponent's xG conceded, add nothing that
  the model's own penalty share and the match model's team xG don't already carry.
- **E, F, G:** the xG-share structure and two-speed form each add about +0.0005, with
  ranges reaching 0. That is a real but tiny sharpening, not a step.
- **H:** LightGBM is clearly worse and over-confident. 9.2% of starters get 30%+, and the
  tail rule fails.

### 2025/26, reported as "seen"

This season chose nothing.

| # | Log loss | AUC | 20–30% predicted / scored | 30%+ predicted / scored (n) |
| --- | --- | --- | --- | --- |
| A | 0.2709 | 0.770 | 24.1% / 26.8% | 36.3% / 37.6% (141) |
| B | 0.2691 | 0.771 | 24.6% / 26.0% | **35.6% / 29.6% (375)**: fails the tail rule |

- A vs B: −0.0018 (95% −0.0038 to +0.0004).
- In 2025/26, B's most confident calls (30%+) scored 6 points less often than predicted.
  The forward check will show whether that repeats.

### The pilot, descriptively

5 matches decide nothing. The round-5 pilot's prices were read back from that job's log.

- **Starters matched:** 51 of the 5 matches' starters to a pilot price. That's fewer than
  round 5's 83, because this matching uses only the players who appeared in each match;
  the misses are still mostly full legal names.
- **Best price vs scoring:** the best price implied 19.8%, and 7.8% scored.
- **Model A:** rated no starter above the price.
- **Model B:**
  - rated 2 starters above the price, and neither scored;
  - one of them was at a 12% edge, and didn't score.
- The model mostly agrees with the books that these players are less likely to score than
  priced. It rarely finds a price too long.

### What follows

- **No priced test is proposed.** The pre-registration asked for one only if an improved
  model passed on development, and none did.
- The structural gap stands either way: Yes-only prices sit about 50% above scoring
  rates. A model with a log-loss gain of +0.002 can't close that.
- **The forward check stays as registered, on B:** 2026/27 matches from 2026-10-10, opened
  once when at least 150 have been played (about early December). Run it with
  `odds-check.yml` `task=goal-lab` and `reason` set to the pre-registered reason.
  - It reports B against A and the season-xG benchmark, and B's tail rule.
  - It checks whether B's 2025/26 over-confidence at 30%+ repeats.
  - If it does, the next step is a pre-registered shrinkage of the top tail (e.g. the
    blend's recalibration), not a new model.
- **Ideas not tried, for a later round:**
  - assists (`player_assisted` is in Understat's shot data), as a second Yes-only market
    with possibly different pricing;
  - "to score or assist";
  - live lineups an hour before kickoff from ESPN. That is the only way to bet B's
    lineup-known chances in practice.

## 10. Multi-league goalscorer test: pre-registration (round 8, 2026-10-08, before any new-league result)

The owner approved it on 8 Oct. Credit cap 0, and player paper trades stay off. The model
spec is **unchanged**:
- **B:** round 6's reference, the current `GOAL_FACTORS` model with the lineup known.
- **A:** the same model before lineups.

Nothing is tuned on any of the data below.

### (a) Historical test: La Liga, Bundesliga, Serie A, Ligue 1

- **Data:** Understat appearances 2022/23–2025/26 per league (2022/23 only warms up
  features and training), and each league's walk-forward match model for team expected
  goals and game state. All of it comes from the round-7 multi-league plumbing.
- **Fits:** walk-forward through `lab.harness`, refitted every 28 days on that league's
  earlier appearances (730-day window). Each league has its own model; nothing pools
  across leagues in training.
- **Scored seasons:** 2023/24, 2024/25 and 2025/26. None of these leagues' goalscorer data
  has been looked at before, so all three are clean.
- **Benchmarks:** the same as round 4/6. The season-to-date goals and xG per appearance
  are each turned into P(≥1) = 1 − e^(−mean), with the position average before a
  player's first appearance.
- **Two comparisons per league, both pre-registered:**
  1. **B on starters with the lineup known** (the betting view, as in round 6). The
     benchmarks are per appearance, substitute appearances included, so on starters they
     lean low. That favours B, and this comparison is reported but **is not the gate**.
  2. **A on every appearance before lineups** (the round-4 stage-1 view, where the
     benchmarks are on equal terms). **This is the gate.**
- **Metrics (same as before):**
  - paired log-loss gain against each benchmark (benchmark minus model), with ranges
    resampling whole matches;
  - Brier score, AUC, the share given 30%+, and the round-6 `tail_rule` for B;
  - per league, and pooled over the four leagues.
- **Correction:** 4 leagues × 2 benchmarks = 8 tests, so per-league ranges are
  **99.375%**. Pooled ranges are 95% (one pooled test per benchmark).
- **The answer the owner asked for** ("does it beat both benchmarks in each league?"):
  a league counts as **yes** when A's gain over **both** benchmarks has its 99.375% range
  above 0. B's numbers are reported beside it.
- **The Premier League** is shown beside them for reference: the same run, 2023/24–2025/26.
  Its 2025/26 has been seen since round 4.

### (b) Widened forward check: all five leagues

- **Scope:** round 6's locked forward check (E0, 2026/27 matches from **2026-10-10
  00:00 UTC**) widens to **E0, SP1, D1, I1 and F1**.
- **Locked:** in every league, matches kicking off on or after 10 Oct are dropped before
  any computation, unless the run is given `--open-forward` with a reason. Opening prints
  and logs the reason and the time.
- **When:** opened once, when at least **150 matches pooled** across the five leagues
  have been played. The owner's trigger is 7 Dec.
- **Gate, pinned now, on the pooled forward starters:**
  - B (lineup known) beats **both** season benchmarks, each paired log-loss gain having a
    95% range above 0;
  - and B passes the round-6 tail rule (20–30% and 30%+ buckets).
- **Also reported:**
  - per league (not gated, because samples are small);
  - A vs the benchmarks on all appearances;
  - B vs A.
- **What a pass means:** the model's calibration and sharpness hold on unseen 2026/27
  matches in five leagues. It doesn't mean the model can beat bookmakers' prices; that
  needs priced data and a separate, costed proposal.

### Run

- **Workflow:** `goal-leagues.yml`, dispatch only. It never pushes or deploys and uses no
  credits.
- **Jobs:** one job per league runs `soccer-stats goal-league` and uploads its scored rows.
  A pooling job then runs `soccer-stats goal-pool`.
- **Cache:** each league's Understat files go to their own cache key prefix
  (`understat-<league>-`), so the Premier League jobs' `raw-data-` cache is never
  replaced.

## 11. Multi-league goalscorer test: results (round 8, 2026-10-08)

**Runs:** `odds-check.yml`, 0 credits, the forward window locked throughout.
- `goal-league` runs 37852786515 (SP1), 37852789463, 37852793011 and 37852796818 (the
  other three) downloaded and cached each league's Understat files.
- `goal-leagues` run 37857461571 scored all five leagues and pooled them.

**Data:** 2023/24–2025/26, appearances with at least 3 earlier ones. 0 Understat match
files were missing in any league.

**Match-model coverage:** the match model's team xG reached 75–77% of appearances in the
new leagues. The rest fall back to neutral, mostly because the match model skips teams
with fewer than 6 earlier matches (promoted sides early in a season).

### The answer: does the model beat both benchmarks in each league?

**Yes, in every league.** This is the gate: A, before lineups, on every appearance, with
99.375% ranges.

| League | Appearances (matches) | Log loss | Gain vs season goals | Gain vs season xG | Beats both |
| --- | --- | --- | --- | --- | --- |
| La Liga (SP1) | 34,235 (1,140) | 0.2305 | +0.123 (+0.108 to +0.138) | +0.023 (+0.018 to +0.029) | **yes** |
| Bundesliga (D1) | 27,179 (917) | 0.2673 | +0.144 (+0.127 to +0.163) | +0.024 (+0.018 to +0.031) | **yes** |
| Serie A (I1) | 34,103 (1,140) | 0.2331 | +0.129 (+0.115 to +0.144) | +0.023 (+0.018 to +0.028) | **yes** |
| Ligue 1 (F1) | 26,465 (918) | 0.2492 | +0.144 (+0.126 to +0.163) | +0.028 (+0.021 to +0.035) | **yes** |
| Pooled four (95%) | 121,982 (4,115) | 0.2435 | +0.134 (+0.128 to +0.140) | +0.024 (+0.022 to +0.026) | **yes** |
| Premier League (reference; 2025/26 seen) | 33,067 (1,140) | 0.2586 | +0.133 (+0.118 to +0.151) | +0.025 (+0.019 to +0.031) | yes |

### The betting view: B on starters with the lineup known

| League | Starters | Log loss | vs season goals | vs season xG | vs A | AUC | 30%+ | 20–30% predicted / scored | 30%+ predicted / scored | Tail rule |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| SP1 | 24,374 | 0.2482 | +0.136 | +0.024 | +0.0018 | 0.791 | 5.5% | 24.5% / 22.7% | 38.6% / 37.2% | ok |
| D1 | 19,533 | 0.2885 | +0.164 | +0.027 | +0.0030 | 0.766 | 6.9% | 24.4% / 22.4% | 39.9% / 37.1% | fails |
| I1 | 24,368 | 0.2520 | +0.143 | +0.024 | +0.0019 | 0.773 | 3.9% | 24.3% / 22.4% | 37.3% / 35.0% | fails |
| F1 | 19,371 | 0.2672 | +0.160 | +0.029 | +0.0019 | 0.778 | 5.5% | 24.4% / 22.8% | 38.6% / 34.3% | fails |
| Pooled four | 87,646 | 0.2624 | +0.149 (+0.142 to +0.157) | +0.026 (+0.023 to +0.029) | +0.0021 | 0.778 | 5.4% | 24.4% / 22.6% | 38.7% / 36.1% | fails |
| E0 (reference) | 24,446 | 0.2817 | +0.148 | +0.027 | +0.0019 | 0.778 | 6.6% | 24.6% / 24.0% | 37.3% / 35.6% | ok |

All of B's gains over both benchmarks have 99.375% ranges above 0.

### What it means

- **The free-data model travels.** Unchanged, it beats both season benchmarks in all four
  new leagues by about the same margin as in the Premier League: log-loss gain +0.023 to
  +0.028 over the season-xG benchmark. It discriminates about as well too (AUC 0.77–0.79).
- **Knowing the lineup helps a little everywhere** (B vs A +0.002 to +0.003), as in the
  Premier League.
- **B is slightly over-confident at the top outside England and Spain.** It predicts its
  confident starters about 2–3 points too high: pooled 38.7% vs 36.1% at 30%+, and
  24.4% vs 22.6% at 20–30%. That fails the round-6 tail rule in Germany, Italy, France and
  the pool.
  - The same thing showed in the Premier League's seen 2025/26 (round 6).
  - It matters for betting, because those confident starters are exactly the ones a bet
    would be on.
  - It is a reason to expect the December forward check to fail its tail rule unless it
    shrinks. Any fix (such as recalibrating the top tail) must be pre-registered before
    that check is opened, not fitted on it.
- **No prices were used.** Beating season averages says the model ranks scorers well. It
  doesn't say bookmakers misprice them. Round 5 found every book's Yes-only prices about
  50% above scoring rates.

### The forward check stays as registered (§10b)

- **What:** five leagues, 2026/27 matches from 10 Oct, locked until at least 150 pooled
  matches have been played (the owner's 7 Dec trigger).
- **How:** `odds-check.yml` `task=goal-leagues` with `reason` set. The four league caches
  now exist, so the run takes about 10 minutes.
- **Gate:** B pooled beats both benchmarks (95%), and passes the tail rule.

### Lead review notes (round 8)

- **Timing:** §10 was committed at 2026-10-08 22:14 UTC (4bbf13f), before the code
  (bb25d7f, 22:19) and before every run: the four `goal-league` runs started at 22:19–22:20
  and the scoring run 37857461571 at 23:05, all on bb25d7f. No other run of this task exists.
  Every number in §11 matches that job log.
- **Two departures from §10's wording, neither a change of test:**
  - The code loads Understat from 2021/22, not 2022/23, so the 730-day training window
    that §10 names is full from the first scored block (2023/24). The scored seasons,
    benchmarks, levels and gate are as registered.
  - The run is `odds-check.yml` `task=goal-league` (per league) and `task=goal-leagues`
    (all five, pooled), not a separate `goal-leagues.yml`. Opening the forward window in
    `goal-leagues` logs one opening line per league (five lines in one run).
- **Forward lock:** while locked, `goal-league` never loads the 2026/27 season at all and
  also drops rows from 2026-10-10 before any feature or fit; a test checks the seasons
  loaded for all five leagues.

## 12. Recalibrated B (B-cal): pre-registration (2026-10-09, before any B-cal number)

**Why:** §11 found B 2–3 points over-confident on its likeliest scorers in Germany, Italy,
France and the pool (pooled 30%+: 38.7% predicted vs 36.1% scored), so the forward check
(§10b) will probably fail its tail rule. The lead asked for one simple, fixed
recalibration, registered before the forward window opens and scored beside B. Credit
cap 0; player paper trades stay off; the live weekly Premier League scoring
(`E0_goals.json`) is unchanged.

### The method, fixed now

- **B-cal** = a logistic recalibration of B's chance:
  logit(p_Bcal) = a + b · logit(p_B), fitted by maximum likelihood.
- **Per league**, not pooled. The over-confidence differs by league (the Premier League
  and La Liga pass the tail rule, the other three don't), and each league has about
  20,000 scored starters a season, so two coefficients per league are well determined.
- **Fitted on starters only** (B is the lineup-known model, scored on starters), using
  the same scored rows as §11: appearances with at least 3 earlier ones, B's walk-forward
  chance and whether the player scored.
- **Earlier seasons only, refitted once a season:** season S uses every starter of that
  league in the scored seasons before S (from 2023/24), and the same (a, b) applies to
  the whole of season S. So:
  - 2023/24 has no B-cal (no earlier scored season);
  - 2024/25 uses 2023/24; 2025/26 uses 2023/24–2024/25;
  - **the forward window (2026/27 from 10 Oct) uses 2023/24–2025/26**, fitted from the
    locked history rows only, so nothing in the window touches it.
- **Floor:** a league with fewer than 2,000 earlier starters keeps B unchanged (B-cal =
  B). That doesn't bind for any season above.
- Coefficients are printed and saved per league and season.

### Forward check (§10b) with B-cal beside B

- **B stays the primary.** Its gate is unchanged: pooled starters, B beats both season
  benchmarks (95% ranges above 0) and passes the round-6 tail rule.
- **B-cal is scored beside it with the same gate:** pooled forward starters, both
  benchmark gains with 95% ranges above 0, and the tail rule (20–30% and 30%+ buckets).
  Also reported: B-cal vs B (paired log-loss gain, 95%), and B-cal per league (not gated).
- **Reading:**
  - B passes: nothing changes.
  - B fails and B-cal passes: the recalibration works on unseen matches. Making B-cal the
    model is then the owner's decision, not automatic.
  - Both fail: recalibration isn't enough; no further variant is tried on this window.
- The window is still opened once, at 150 pooled matches (the 7 Dec trigger), with a
  reason. Nothing here opens it.

### Development display (descriptive only)

- **Seasons:** 2024/25 and 2025/26 in all five leagues, starters with B-cal defined,
  per league and pooled. These rows have all been seen (§11 reported B's buckets on
  them), so this is a check that B-cal does what it is meant to, **not a test**.
- **Shown:** log loss, Brier, AUC, the share at 30%+, the tail rule and its buckets for
  B and B-cal; both models' gains over the two season benchmarks; B-cal vs B (95%,
  whole-match resampling); the coefficients.
- **Nothing shown here can change B-cal's method or either gate.** If it looks bad, that is
  reported as it is and the forward check still scores B-cal as registered.

### Run

- `soccer-stats goal-pool` computes B-cal from the league rows that `goal-league` writes
  (no new data, no refit of A or B). It runs in `odds-check.yml` `task=goal-leagues` with
  `reason` left blank, so the forward window stays locked.

## 13. B-cal: development display (2026-10-09, descriptive only)

**Run:** `odds-check.yml` `task=goal-leagues` with `reason` blank, run 37896011591, on
996e458. 0 credits, and the forward window stayed locked: no forward rows were written
and no window was opened. The history numbers it printed for A and B in every league
match run 37857461571 (§11) exactly.

**Coefficients**, logit(p_Bcal) = a + b · logit(p_B), per league, fitted on earlier seasons'
starters:

| League | 2024/25 (fit on 2023/24) | 2025/26 (fit on 2023/24–2024/25) |
| --- | --- | --- |
| E0 | a −0.125, b 0.944 (8,104 starters) | a −0.070, b 0.999 (16,280) |
| SP1 | a +0.009, b 1.037 (8,142) | a −0.080, b 0.995 (16,249) |
| D1 | a −0.146, b 0.980 (6,510) | a −0.146, b 0.985 (12,998) |
| I1 | a −0.074, b 0.982 (8,136) | a −0.110, b 0.975 (16,235) |
| F1 | a −0.297, b 0.912 (6,439) | a −0.160, b 0.946 (12,918) |

The correction is mostly a downward shift (a below 0) with a slope near 1: B is a little
too high on everyone, not only at the top.

**B vs B-cal, starters, 2024/25 and 2025/26 (95%, whole-match resampling):**

| League | Starters | Log loss B → B-cal | B-cal vs B | 30%+ share B → B-cal | Tail rule B → B-cal |
| --- | --- | --- | --- | --- | --- |
| E0 | 16,342 | 0.27267 → 0.27263 | +0.00004 (−0.00022 to +0.00031) | 6.2% → 5.0% | ok → ok |
| SP1 | 16,232 | 0.24817 → 0.24794 | +0.00023 (−0.00005 to +0.00050) | 5.6% → 5.1% | fails → ok |
| D1 | 13,023 | 0.28622 → 0.28581 | +0.00041 (−0.00017 to +0.00099) | 6.7% → 4.9% | fails → ok |
| I1 | 16,232 | 0.25264 → 0.25245 | +0.00019 (−0.00004 to +0.00042) | 3.7% → 3.1% | ok → ok |
| F1 | 12,932 | 0.26967 → 0.27000 | −0.00033 (−0.00095 to +0.00033) | 5.1% → 3.5% | fails → ok |
| **Pooled five** | **74,761** | **0.26484 → 0.26473** | **+0.00011 (−0.00007 to +0.00029)** | **5.4% → 4.3%** | **fails → ok** |

Pooled tail buckets, predicted / scored (95% range of scored):
- B: 20–30% 24.4% / 22.9% (21.9–23.9%), fails; 30%+ 38.3% / 35.2% (33.8–36.7%), fails.
- B-cal: 20–30% 24.3% / 24.5% (23.4–25.5%), ok; 30%+ 37.7% / 37.1% (35.4–38.8%), ok.

Both models still beat the season-xG benchmark by about the same margin (pooled +0.0257
for B, +0.0258 for B-cal).

### What it shows

- **B-cal does what it is meant to.** On these seen seasons it fixes the over-confidence
  at the top: the pooled tail rule goes from failing both buckets to passing both, and
  every league passes after recalibration.
- **It barely changes overall accuracy.** The pooled log-loss gain is +0.0001 with a range
  that includes 0; per league it's −0.0003 to +0.0004, none clearly different from 0. The
  calibration error B carries is small in log-loss terms. It matters only where a bet
  would be: the confident starters.
- It is fitted on earlier seasons only, so these are out-of-sample predictions. But these
  seasons were seen in §11, so this isn't a test; the forward check is.

### Departure from §12's wording (not a change of method)

§12 said each league has "about 20,000 scored starters a season". In fact each league has
about 6,400–8,100 a season (about 20,000 over the three scored seasons), so the fits use
6,439–16,280 starters. That is still far above the 2,000 floor, and nothing in the method
changes.

### For the forward check (§10b, §12)

Nothing changes. The window opens once at 150 pooled matches, with a reason. B is the
primary; B-cal is scored beside it with the same gate. Its coefficients are fitted on each
league's 2023/24–2025/26 starters from the locked rows, and the run prints and saves
them (`b_cal_forward_coefficients`).

### Lead review note (2026-10-10, before the forward window opened): match-model tail fix

`backtest.walk_forward` used to drop the final partial week of whatever data it was given
(PR #33 fixes it; the last window is fitted on earlier matches only, and every earlier
prediction is unchanged). The goalscorer only takes the match model's team xG and game
state from it, so the effect here is that appearances in that last week had team xG at the
league average and game state 0; they now get the match model's values.

- **Weekly `E0_goals.json`** loads the season in progress, so its tail was the latest
  2026/27 matchday (the `live` split). The locked 2025/26 holdout is not at the tail and
  should not move; the post-merge `players.yml` run checks this (Lead handoff in CLAUDE.md).
- **§11 and §13 (multi-league history, B-cal)**: with the forward window locked,
  `goal-league` loads up to 2025/26, so the tail was 2025/26's last matchday (about 10 of
  380 matches a league, under 1% of the three scored seasons). Those numbers were made with
  the old loop and are not re-run: the gate gains (+0.023 to +0.028, lower ends +0.018 or
  more) cannot flip on that share of rows. The B-cal coefficients the forward run fits on
  the locked 2023/24–2025/26 rows will differ slightly from a pre-fix run for the same
  reason. **This change to the predictions was made before the window opened** (it opens
  once at 150 pooled matches, about 1 November).
- **Forward lock unchanged:** while locked, nothing from 2026/27 is loaded and rows from
  2026-10-10 are dropped before any feature or fit; the fix only adds windows after the
  last loaded match, so it cannot reach the locked window.
- **Raised separately (not caused by PR #33):** the weekly `E0_goals.json` `live` split
  scores all of 2026/27, so from the next Monday run it includes Premier League
  appearances after 2026-10-10 (model A on all appearances, not the pooled B-starters gate
  of §10b). Player props should decide whether that split should stop at 2026-10-10 until
  the forward window opens.

### Weekly `E0_goals.json` stops at the forward window (2026-10-10, before it opened)

This answers the lead's 10 Oct note above. From now on, the weekly stage-1 file locks the
forward window the same way `goal-league` does:

- **How:** `player_goals.stage1_goals` drops Premier League appearances from 2026-10-10
  before any fit or score (`lock_forward`). Its `live` split (2026/27) therefore stops at
  9 Oct.
- **Nothing reaches the file:** goal features use earlier kickoffs only, so the rows kept
  are unchanged. A test changes the window's results and checks the saved report is
  identical.
- **The file records the lock:** it carries `forward_window`: start, open or locked, the
  number of appearances left out, and the opening log. The players.yml log prints the same.
- **After the window opens** (once, at 150 pooled matches, with its reason): the weekly
  file includes those matches again only when `players.yml` is dispatched with
  `goal_forward_reason` (CLI `--goal-forward-reason`), which prints and logs the opening.
  Scheduled Monday runs pass nothing, so they stay locked until the lead switches them.
- The development seasons and the 2025/26 holdout are unaffected, so the gate is unchanged.

## 14. Forward check, five leagues: result (2026-11-01, window opened once)

**Verdict: B passes and B-cal passes** the gate pre-registered in §10b and §12. This is a
model-quality result only: nothing goes live and nothing is bet unless the owner decides,
and it says nothing about beating bookmakers' prices (that needs priced data and its own
pre-registered, costed test).

**Caveats, read first:**
- **The tail rule fails in three of the five leagues on their own.** For B: D1 (20–30%
  bucket scored 16.7% vs 24.9% predicted), SP1 (19.2% vs 24.6%) and I1 (scored above
  predicted in both buckets). B-cal fixes SP1's tail but not D1's or I1's. These buckets hold
  only 35–81 starters each and the gate is pooled by design, but the pass is a pooled one.
- **The sample is 179 matches, not the 240 in the lead's count.** 240 was played matches in
  the odds logs; Understat had lineups for 179 of them when the run happened. 179 is still
  above the 150 gate, but the forward sample is smaller than the opening note implies.

### Opening

- **Reason (lead, 1 Nov), logged in both places:** "Lead, 1 Nov 2026: 240 matches in
  E0/SP1/D1/I1/F1 since 10 Oct, past the pre-registered 150 gate; open the forward window
  once."
- **`odds-check.yml` `task=goal-leagues`, run 41325824036** (default branch at 182a2c3, 0
  credits, 16:20–18:23 UTC): one "HOLDOUT OPENED … (rows from 2026-10-10)" line per league,
  E0 16:27:11Z, SP1 16:58:24Z, D1 17:23:36Z, I1 17:54:47Z, F1 18:22:46Z. Each league took
  about 30 minutes (its 2026/27 Understat files were downloaded for the first time).
- **`players.yml` with `goal_forward_reason`, run 41325826728:** "HOLDOUT OPENED at
  2026-11-01T16:22:54Z"; `E0_goals.json` on data-log carries `forward_window` "open"
  (data-log 609e817).
- PR #161 printed the whole forward report to the job log first, because the sandbox can't
  download artifacts; every number below is from the log.

### Sample

179 scored matches (E0 38, SP1 34, D1 33, I1 44, F1 30): 5,338 appearances, 3,840 starters.
That is above the 150 gate. The lead's count of 240 was played matches in the odds logs;
Understat had lineups for 179 of them by the run (no match file missing).

### Pooled result (the gate)

Log loss per appearance; "gain" = benchmark's log loss minus the model's (higher is
better); 95% ranges resample whole matches.

| | Rows | Log loss | Gain vs season goals | Gain vs season xG | Tail rule |
| --- | --- | --- | --- | --- | --- |
| A, before lineups | 5,338 appearances | 0.2726 | +0.251 (+0.214 to +0.290) | +0.048 (+0.035 to +0.061) | |
| **B, lineup known** | 3,840 starters | 0.2909 | +0.297 (+0.249 to +0.348) | +0.057 (+0.040 to +0.075) | ok |
| **B-cal** | 3,840 starters | 0.2910 | | +0.056 | ok |

- B's AUC is 0.767; B beats A on the same starters by 0.0065.
- B's tail buckets:
  - 20–30%: 377 starters, predicted 24.8%, scored 23.3% (19.4% to 27.9%);
  - 30%+: 236 starters, predicted 37.9%, scored 36.0% (30.2% to 42.3%).
- B-cal vs B: −0.00007 (−0.0009 to +0.0007), so no difference in log loss.
- B-cal's tail buckets are tighter:
  - 20–30%: 361 starters, predicted 24.5%, scored 24.7%;
  - 30%+: 188 starters, predicted 37.0%, scored 37.8%.
  - 4.9% of starters are at 30%+ under B-cal, against 6.2% under B.
- **Gate B:** `{"beats_both": true, "tail_ok": true, "passes": true}`.
- **Gate B-cal:** `{"beats_both": true, "tail_ok": true, "passes": true}`.

### B-cal's forward coefficients

Fitted on each league's 2023/24–2025/26 starters from the locked rows, as registered in §12:

| League | Rows | a | b |
| --- | --- | --- | --- |
| E0 | 24,446 | −0.070 | 0.999 |
| SP1 | 24,374 | −0.088 | 0.998 |
| D1 | 19,533 | −0.164 | 0.964 |
| I1 | 24,368 | −0.124 | 0.968 |
| F1 | 19,371 | −0.145 | 0.969 |

### Per league (descriptive, not gated: 30–44 matches each)

| League | Starters | B log loss | B gain vs xG (95%) | B tail | B-cal vs B (95%) | B-cal tail |
| --- | --- | --- | --- | --- | --- | --- |
| E0 | 822 | 0.2662 | +0.029 (+0.004 to +0.058) | ok | +0.0001 (−0.0010 to +0.0012) | fails (20–30%: 28.0% vs 24.5%) |
| SP1 | 737 | 0.2860 | +0.053 (+0.021 to +0.094) | fails (20–30%: 19.2% vs 24.6%) | −0.0003 (−0.0021 to +0.0016) | ok |
| D1 | 705 | 0.2860 | +0.030 (+0.000 to +0.068) | fails (20–30%: 16.7% vs 24.9%) | +0.0022 (−0.0001 to +0.0044) | fails |
| I1 | 941 | 0.3198 | +0.078 (+0.039 to +0.120) | fails (scored above predicted) | −0.0014 (−0.0028 to −0.0001) | fails |
| F1 | 635 | 0.2912 | +0.094 (+0.046 to +0.145) | ok | −0.0006 (−0.0034 to +0.0020) | fails (20–30%: 27.9% vs 24.1%) |

- B beats both benchmarks in every league, but D1's and E0's xG ranges only just clear 0.
- Per-league tail buckets hold 26–81 starters, so they are noisy; the gate is pooled by
  design. I1 is the one league where B-cal is worse than B with a range below 0.

### Same run, development seasons (context, seen before)

- SP1, D1, I1 and F1 pooled over 2023/24–2025/26: B's tail rule fails (it scores 1.5–2.7
  points under predicted in both buckets), and B-cal's passes on 74,761 starters.
- B-cal vs B there: +0.0001 (−0.0001 to +0.0003). This matches §13.
- The Premier League's weekly live split (`players.yml`, 2,410 appearances from 10 Oct):
  before lineups 0.2461, gain vs xG +0.042 (+0.024 to +0.061); lineup known 0.2393, gain
  vs xG +0.049 (+0.030 to +0.068); the 2025/26 holdout gate still passes (0.2464).

### What it means

- On 2026/27 matches it had never seen, in five leagues, the goalscorer model beats both
  simple benchmarks clearly. Its high-chance players score about as often as it says.
- B-cal makes no difference to log loss but trims B's over-confidence at the top. The
  development seasons showed the same, now confirmed out of sample.
- §12's reading applies: B passed, so nothing changes automatically. Whether B-cal
  replaces B, and whether a priced goalscorer test is proposed, are the owner's decisions.
- The window has been opened once. It is not re-scored or opened again on this question.
- The weekly `E0_goals.json` keeps the window open from now on (lead, 1 Nov): the opening
  is committed in `player_goals.FORWARD_OPENED` (start, time, reason, run ids), so
  scheduled `players.yml` runs include the window without passing a reason, and each run's
  `forward_window.log` repeats the recorded opening line. The lock code stays for any future
  window; a window with no record is still locked.

## 15. Priced goalscorer test: step 0, the probe (lead-approved 2026-11-01, cap 250)

The forward pass (§14) is about model quality only. The next honest step is a priced
test of B against bookmakers' anytime-goalscorer prices. The lead's decisions (1 Nov):
- B is the primary model, keeping §10b's order; B-cal is reported beside it.
- Step 0, this probe, is approved at ≤ 250 credits.
- The 2025/26 development season (~35,000 credits) is approved in principle if the probe
  shows the other leagues are priced in history. It may only set the edge threshold and
  check the plumbing.
- The pre-registration (the c-range and CLV rules, 600 matches or 31 Jan 2027, at least
  300 bets, read once) comes after the probe, for the lead's sign-off, before anything
  else is bought.

**What the probe asks** (`player_goal_probe.py`, `odds-check.yml` `task=goalscorer-probe`,
from the default branch):
- **Live, per league (E0, SP1, D1, I1, F1):** upcoming matches in kickoff order, all five
  regions, until one is priced (at most 5 credits a call; an empty answer costs 0).
- **History, per league, 2025/26 and 2026/27:** one historical events list on a Saturday
  (1 November 2025, 31 October 2026; 1 credit) and the first match from 11:00 UTC that day,
  at the planned entry (kickoff − 50 min, after lineups) and close (kickoff − 2 min), with one
  ten-book list (10 credits each). The list is the books found live in that league, topped
  up with the books seen pricing the Premier League in §6.
- **Worst case:** 25 live + 10 lists + 20 snapshots × 10 = 235 credits.
- **Reported:** per snapshot, which books price it and for how many players, any two-sided
  (Yes and No) lines, and how many priced names match the two clubs' Understat players.
- **Not read:** no result, start or model chance is joined. The bodies are kept in the paid
  `player-odds-history-` cache and never bought twice.
- **Departure from the scope:** the scope priced one close per league for 2023/24–2026/27.
  The lead approved 2025/26 only as development, so the probe spends that budget on
  2025/26 and 2026/27 at both snapshots the test would use.

### Probe result (run 41339771090, 1 Nov 2026, 235 of 250 credits)

Every planned call answered (HTTP 200). The cost headers summed to 235: live 25, lists 10,
snapshots 200. The key's balance was 4,999,734 before and 4,999,499 after. The bodies are
in the paid `player-odds-history-` cache (key `player-odds-history-41339771090`), and the
archive job dispatched `caches.yml`.

**Who prices anytime goalscorer** (players priced per book; one match per row):

| League | Live (all regions) | 2025/26 at kickoff − 50 min | 2026/27 at kickoff − 50 min |
| --- | --- | --- | --- |
| E0 | 20 books, incl. DraftKings 39, FanDuel 20, BetMGM 31, Caesars 38, Bovada 39, 1xBet 39, Kambi 31, Betfair exchange 22–38 | 1xBet 44, Bovada 43, DraftKings 43, Kambi 30 | 1xBet 40, TAB 34, Caesars 33, ReBet 32, Bovada 25, DraftKings 19, Betfair AU 19 |
| SP1 | 13, incl. Betfair exchange 41–47, 1xBet 24, DraftKings 20, FanDuel 19, BetMGM 19, Kambi 18 | 1xBet 45, DraftKings 44, FanDuel 41, Kambi 34 | DraftKings 39, FanDuel 38, William Hill 38, Sportsbet 38, Kambi 34, BetMGM 16 |
| D1 | 5: Kambi 20, TABtouch 20, PointsBet AU 14 | Bovada 38, Kambi 25 | Kambi 30, TABtouch 29, BetMGM 17 |
| I1 | 12, incl. DraftKings 34, Betfair exchange 32, FanDuel 31, Kambi 24, BetMGM 20 | DraftKings 32, Kambi 28, FanDuel 14 | Betfair exchange 45, DraftKings 45, William Hill 45, FanDuel 44, Kambi 32 |
| F1 | 13, incl. Betfair exchange 39, 1xBet 39, ESPN BET 35, DraftKings 21, FanDuel 20, BetMGM 20, Kambi 20 | 1xBet 42, DraftKings 35, FanDuel 35, Kambi 26 | Betfair exchange 35, ESPN BET 30, Kambi 19, FanDuel 18, DraftKings 17, BetMGM 15 |

- **All five leagues are priced, live and in both seasons' history**, at the entry and
  the close.
- **DraftKings and FanDuel now price it** (they didn't in the 7 Oct EPL probe, §6), except
  in the Bundesliga, where no US book but BetMGM and the Kambi books appear.
- **No book lists a "No" price: 0 two-sided lines in all 25 snapshots.** So there is
  still no fair price.
- **The Betfair exchange's numbers look wrong for this market:** average Yes odds of 1.7–7.0
  in 14 of its 18 book-snapshots (9.5–14.4 in the other four), when DraftKings averages
  9.7–14.5 in the same matches. Those
  can't be real back prices for "to score". They are left out of the judged price
  until checked (§16).
- **Fewer players are priced at the close:** books drop players not in the lineup, so a
  close holds 16–45 names where the entry held 17–45.
- **Name matching:** 75–97% of priced names land on the two clubs' Understat players.
  - Most misses are full legal names ("Carlos Baleba Noom Quomah", "Joao Pedro Junqueira de
    Jesus", "Gleison Bremer Silva Nascimento") or new and young players with no Understat
    row yet.
  - Every snapshot also carries a "No Scorer" line, which is not a player.
  - Overrides in `player_names.csv` and dropping "No Scorer" come before any scoring (§16).

## 16. Priced goalscorer test: pre-registration (2026-11-01; binding once merged with the lead's sign-off)

Written after the probe (§15), which read no result, and before any further price is
bought. Steps 1–3 buy nothing until this section is merged. A change after the merge is
recorded here as a dated departure, never made silently.

**Caveat, stated first:** the forward window's model-quality results (§14: B and B-cal
against the season benchmarks) were read on 1 Nov, and this test judges matches in that same
window. The priced test uses no choice made from those results:
- B is primary because §10b made it so on 8 Oct;
- the edge threshold T comes from the 2025/26 development season;
- no price has been joined to any result.

So the window stays usable for this test, but it is not untouched.

### Question

Does model B carry information about who scores beyond bookmakers' anytime-goalscorer
prices? And do its bets beat the closing price? Nothing goes live from this:
`PLAYER_PAPER_TRADES` stays False, and any live use is the owner's decision.

### Model and rows

- **Primary model: B** (lineup known; walk-forward fits on earlier matches only, exactly
  as scored in §10b and §14). **B-cal** gets the same measures beside it, descriptive only.
  A (before lineups) is not scored.
- **Rows:** Understat starters in E0, SP1, D1, I1 and F1 whose name matches a priced name
  at the entry snapshot.
  - Matching uses `match_in_fixture` within the match's two clubs, after `player_names.csv`
    overrides for the legal-name misses that §15 and step 1 show. The overrides are
    written before any result is joined.
  - "No Scorer" and unmatched names are dropped and counted.
  - A starter needs at least 3 earlier appearances (as §10b).

### Prices and snapshots

- **Books (one ten-book list, one region):** draftkings, fanduel, betmgm, betrivers,
  williamhill_us, bovada, espnbet, onexbet, williamhill, betfair_ex_uk.
- **Entry price:** the best Yes price among the first nine (the bookmakers) at the entry
  snapshot; implied chance = 1 / odds.
  - The Betfair exchange is logged but kept out of the judged price, because its §15
    numbers aren't plausible back prices.
  - A per-book breakdown is descriptive.
- **Entry snapshot:** kickoff − 50 min in history.
  - Live, it is the first call after ESPN's `first_confirmed_at` for the match, and no
    earlier than kickoff − 75 min.
  - If the lineup isn't confirmed by kickoff − 30 min, the entry is taken at kickoff − 30
    min.
- **Close:** kickoff − 2 min in history. Live, it is the last call before kickoff, which
  must be within 20 min of it; otherwise the bet has no close.
- **Judged CLV of a bet** = best entry odds ÷ best close odds − 1, where both are the
  maximum Yes price for that player over the same nine bookmakers (at the entry snapshot
  and at the close).
  - This compares like with like. Taking the best of nine prices at entry and then
    measuring against that one book's own close would favour a pass with no skill: the
    book that is briefly highest tends to fall back.
  - A bet with no close price at any of the nine has no CLV and is counted.
- **Same-book CLV** (entry odds ÷ the entry book's own close − 1) is descriptive only.

### Measures and pass rule (read once)

1. **Information (c):** logistic regression on the judged rows,
   logit P(scored) = a + b·logit(implied) + c·logit(p_B). Its 95% range comes from 2,000
   bootstrap draws resampling whole matches.
2. **CLV of the rule's bets, against a price-matched baseline** (amended 1 Nov 2026, before
   any judged number was read; see the second departure note below): a starter is a bet when
   p_B × best entry odds − 1 ≥ T, at the best price, one bet per player per match, $1.
   - The rule's CLV is the mean best-to-best CLV (above) of its bets.
   - The baseline is the same best-to-best CLV over every judged starter priced at entry and
     close, with no rule, **reweighted to the rule's mix over five entry-price bands**: the
     baseline's mean CLV in each band, weighted by the rule's share of bets in that band.
   - The bands are fixed now (decimal odds, lo < odds ≤ hi): up to 6.5, 6.5–11.5, 11.5–17.0, 17.0–23.0 and over 23.0. Their edges are
     the odds quintiles of the 2025/26 development rule bets at T = 20% (run 41350946771;
     volume only, no result and no CLV used).
   - **The judged measure is the excess:** the rule's mean CLV minus the matched baseline's,
     both on the same judged matches. Its 95% range comes from one match bootstrap shared by
     the rule and the baseline (2,000 draws, seed 0).
   - Code: `player_goal_priced.matched_excess` and `verdict`; edges in
     `PRICE_BAND_EDGES`, T in `T_JUDGED`.
3. **Descriptive, beside the verdict:** the plain best-to-best CLV of the rule's bets, the
   unweighted baseline (every judged starter, no rule) and the rule's excess over it, and the
   same-book CLV.

- **T, the edge threshold**, is fixed from the 2025/26 development season by volume alone.
  T is the **largest** of 5%, 8%, 12%, 16% and 20% that gives at least 0.3 bets a match
  there, pooled over the five leagues' development matches; if none reaches 0.3, T = 5%. It
  never uses a result or a CLV. (Corrected 1 Nov, before any development price was bought:
  see the departure note below.)
  - If development is not bought, or fails the plumbing checks, T = 12%.
  - Development may set nothing else. Its c, CLV and ROI are reported as development and
    cannot change this section.
- **Verdict:**
  - **pass:** c's range lies above 0, and the matched CLV excess's range (measure 2) lies
    above 0 on at least 300 bets with a CLV;
  - **inconclusive:** c's range lies above 0 but there are fewer than 300 bets with a CLV;
  - **fail:** otherwise.
  - The verdict is model-quality-against-the-market only; a pass is not a live decision.
- **Descriptive:**
  - measure 3 (plain CLV, unweighted baseline and excess, same-book CLV);
  - ROI at the entry price;
  - the gap (mean implied − scoring rate);
  - hit rate;
  - every measure per league, per book, and for B-cal.

### Judged sample and when it is read

- **Matches:** E0, SP1, D1, I1 and F1, kicking off from **2026-10-10**.
- **Stopping rule:** stop at the earlier of **600 judged matches** (a match with at least one
  judged row) or matches kicking off by **2027-01-31 23:59 UTC**.
- **The read:** once, after the last of them is settled. Nothing is computed on judged
  results before then; the plumbing checks read prices and names only.
- **Matches since 10 Oct** come from history (step 1). **Matches from the start of live
  logging** come from the live calls (step 2). History is bought only for matches the live
  log lacks.

### Steps and credits (approved at these estimates once this section merges)

| Step | What | Credits |
| --- | --- | --- |
| 1 | History for the forward window since 10 Oct (~240 matches × 2 snapshots × 10, plus ~100 event lists) | ~4,900 |
| 2 | Live logging in `publish.yml` (entry and close, 1 credit a call, call record on data-log, never fetched twice; monthly cap 3,000, 3,000 reserve) through the end of the judged window, then the lead decides | ~2,200–4,400 |
| 3 | 2025/26 development, approved in principle by the lead (sets T only) | ~35,000 |

### Expectations, stated now

My prior for a pass is about 10–15%, or about 20% for c alone. Every price is Yes only, with
an overround of roughly 50% (§6), so a profitable rule is unlikely even if B carries
information.

### Departure, 2026-11-01 (before any development price was bought or read)

As merged, §16 said T is "the smallest of 5%, 8%, 12%, 16% and 20% that gives at least 0.3
bets a match". A lower threshold always gives more bets, so that rule picks 5% whenever 5%
reaches 0.3. It would never choose anything else, which was not the intent. The intent was
the strictest threshold that still gives enough bets to reach 300 in the judged window. The
rule now reads "the largest … that gives at least 0.3 bets a match, pooled over the five
leagues; 5% if none does". Like the old rule, it uses volume only (no result, no CLV).
Nothing had been bought for development when it was changed.

### Departure, 2026-11-01, second (after the development read; before any judged number)

The 2025/26 development read (descriptive, run 41346853368) showed that the best Yes price
50 minutes before kickoff is about 3% longer than the best at 2 minutes for every starter,
with no rule: the no-rule baseline CLV was +2.85% (+2.63% to +3.06%), above 0 in every
league (E0 +1.5% to F1 +4.4%). Starter prices shorten into kickoff. As written, measure 2
judged the rule's CLV against 0, so that drift alone could have passed the test without any
skill. The lead approved changing the judged measure to the rule's CLV minus the no-rule
baseline's. The baseline is reweighted to the rule's mix over five entry-price bands, since
the rule picks a different mix of prices than the average starter and the drift may differ
by price. The bands are the development rule bets' odds quintiles, fixed before the judged
read. Pass now needs that excess's range above 0 on at least 300 bets, and c's range above 0
as before. Nothing had been computed on the judged window (prices, CLV or results) when this
was changed; its history was bought (step 1) but not read.

### Note, 2026-11-07: history for what the live log lacks (lead-approved; before any purchase or outcome read)

The first week of live logging (2–7 Nov; 43 calls, 26 matches) left gaps:
- the 17 five-league matches on 1 Nov, before the first live call (2 Nov 17:21), have
  neither snapshot;
- 9 matches have a live close but no live entry, because no publish run fell in the entry
  window (20–75 min with ESPN's lineups, or 20–30 min without them);
- 1 live close call got a 502 (Espanyol v La Coruna, 7 Nov).

**Rule.** §16 already buys history for what the live log lacks. That is now applied per
snapshot, not per match. For every judged match from 1 Nov, an (event, snapshot kind) with
no successful (2xx) live call is bought from history at §16's history times (entry kickoff
− 50 min, close kickoff − 2 min, the same ten books). It is paired with whatever the live
log holds for the other snapshot. So a close-only match gets the history entry plus its live
close, a match with neither gets both from history, and the 502 close is bought from
history. Where both exist, the live snapshot is used.

**Why not drop them.** Dropping matches without a live entry would bias the sample toward
matches whose lineups ESPN shows early. That depends on league, kickoff time and the
schedule, not on chance.

**Source recorded.** Every judged row records the source of its entry and its close
(`entry_source`, `close_source`: "live" or "history"; `player_goal_priced.combine` and
`join`). Live and history counts are reported beside the verdict. A per-source breakdown is
descriptive only.

**What runs it.** The rule is applied by `player_goal_backfill` design `gaps`
(`odds-check.yml` `task=goalscorer-backfill`, `design=gaps`), with data-log's live call
record. It is bought from the default branch inside §16's step-1 approval, with a cap of
500 credits (lead, 7 Nov). From the same date the live log retries a call that isn't
answered 2xx, at most 3 attempts per (event, snapshot), each one recorded with its cost.
closes.yml is to add a publish run at kickoff − 25 min (Moneyline), so later gaps should be
rare. The rule covers any that remain.

This note was committed before any of these snapshots was bought and before any outcome in
the judged window was read. It changes neither the measures nor the pass rule.

### Note, 2026-11-08: B's window rows through 1 Nov come from the rescued artifact (lead-approved; no outcome read)

Model evaluation's dev rerun on 8 Nov (goalscorer-test.yml run 42474646236) failed in SP1,
D1, I1 and F1 ("0 appearances (1530 match files missing)"). Their Understat match files
were in no cache. The `understat-<league>-` caches had been evicted before 1 Nov: the dev
run that day (41346853368) already missed them, downloaded every match file from Understat
and saved none. Understat has been down since 2 Nov, so the files can't be fetched again.

**What is kept.** The only rows built from those files are in the 1 Nov artifacts:
- B's scored rows for all five leagues through 1 Nov, from the forward check, run
  41325824036 (`rows_<L>.csv.gz`, `forward_<L>.csv.gz`);
- the 2025/26 development rows, from run 41346853368 (`judged_<L>.csv.gz`).

They were copied before expiry into artifact 13615572896 ("goalscorer-1nov-rescued", run
42476536162, with SHA256SUMS), then onto the release `paid-odds-archive` as
`goalscorer-1nov-rescued.zip`. Its sha256 is in `archive/extra_assets.json`, which
caches.yml's verify job checks. Nothing in them was read.

**Rule for the judged read.**
- **Through 1 Nov:** window matches kicking off by 1 Nov use B's chances from the rescued
  `rows_<L>.csv.gz`. Those rows were built at commit 182a2c3, before
  `models.player_goals.FIT_OPTIONS`, so a rebuild would not match them bit for bit.
- **From 2 Nov:** matches use B's chances rebuilt once Understat is back (goal-league on the
  default branch, SP1–F1, 2021/22 onward; E0's files are in raw-data and the new
  `understat-E0-` shard), with FIT_OPTIONS.
- **Matches in both:** matches kicking off by 1 Nov that the rebuild also scores keep the
  rescued rows, the copy made first.
- **No Understat by 31 Jan:** if Understat hasn't returned, the sample is the matches
  through 1 Nov. Under 300 bets with a CLV is "inconclusive" by §16's own rule, with no
  departure.

Measures, thresholds and the pass rule are unchanged.

**Wayback Machine, checked 8 Nov** (free CDX count, run 42477795332, one-off; the tool was
not kept). Played matches from 2021/22 with any 200 capture of the getMatchData endpoint or
the match page:
- all five leagues: 1,320 of 8,960 (14.7%); the endpoint our fetcher uses only 25 times;
- E0 521 of 1,988, SP1 496 of 2,004 (378 of them 2024/25), D1 204 of 1,600, I1 51 of
  1,614, F1 48 of 1,754;
- the 2026/27 window: I1 0, F1 0, D1 4 of 70.

B needs each player's run of earlier matches, so this can't rebuild the four leagues:
we wait for Understat.

**Protection from now on** (same PR as this note): the Understat match files are kept in
five shards (`understat-E0-` … `understat-F1-`, path `data/raw/understat_matches`). Every run
that works on a league restores all five, which together hold every file any run kept. A run
saves its own shard back from the default branch only, and only when it added files, so a
copy with fewer files than were restored is never saved. The shards are kept alive and
archived (`cache_archive.ARCHIVED_FREE`).

### Note, 2026-11-09: a live paper track beside the test (owner-approved; descriptive, no outcome read)

The owner asked for §16's rule to run live as a paper track under Portfolio → Anytime
goalscorer (`player_goal_paper.py`). It is display only: **the judged read above is
unchanged**. It is still read once, on 1 Feb 2027, from the rows this section defines, and
nothing in the track feeds it.

The track uses the frozen rule with no new parameters: B, T = 20%, the best Yes over the nine
bookmakers, $10, and the entry and close snapshots of the live log. Beside it a DraftKings-only
line runs the same rule at DraftKings' price (descriptive). It reads only what publish already
logs and costs 0 credits.

Where a live bet can differ from the judged rule's (sent to the lead before the build):
1. **Features while Understat is down.** B's features for a match after the last match file
   held see nothing later. Each trade records `features_through` and `features_stale`, and
   the sections count the stale ones. The judged read recomputes with full files. Entries are
   never rewritten; once Understat is back each settled trade gets a `result_check` against
   its goals (lead, 9 Nov).
2. **Starters.** ESPN's XI at the entry (data-log `team_news/`), not Understat's after the
   match. A listed starter who didn't start is void.
3. **Names.** ESPN's and the books' names map to Understat ids through each club's earlier
   appearances, not the match's own rosters.
4. **Position.** A starter's position is his last starting position.

Everything else is computed as the judged read computes it:
- B's 28-day blocks from 10 Oct;
- 730 days of training before the block's first kickoff;
- the match model's team xG from `backtest.walk_forward`'s weekly grid (a test checks they
  are equal);
- B rounded to 6 decimals.

Settlement uses ESPN's goal details (own goals left out); football-data has no scorers.
The track is backcast from 1 Nov, the live log's first day, on the logged entry snapshots
only (never the bought history), and starts at its `defined_at`.

### Development result (2025/26, run 41346853368; development only, descriptive)

**T = 20%**, by the volume rule. Every candidate clears 0.3 bets a match, so the rule takes
the largest; each league alone also gives 20%.

| T | Bets | Per match |
| --- | --- | --- |
| 5% | 4,649 | 2.73 |
| 8% | 4,000 | 2.35 |
| 12% | 3,246 | 1.91 |
| 16% | 2,648 | 1.55 |
| 20% | 2,113 | 1.24 |

1,704 matched development matches (E0 380 of 380, SP1 341 of 380, D1 306 of 306, I1 380 of
380, F1 297 of 306) and 27,167 starter rows with prices. At 1.24 bets a match, 600 judged
matches would give about 740 bets.

These numbers are development only. They are descriptive and cannot change this section:

- **c:** 0.49 (0.37 to 0.60); each league alone 0.45 to 0.51, every range above 0.
- **No-rule baseline CLV (unweighted):** +2.85% (+2.63% to +3.06%) on 27,164 rows.
- **The rule's CLV, best to best:** +7.61% (+6.85% to +8.40%) on 2,113 bets; same book
  +13.1% (+11.8% to +14.4%; it carries the selection bias of picking the best price).
- **Price-matched excess (the amended judged measure, here descriptive; run 41350946771):**
  +4.60% (+3.88% to +5.29%): the rule's +7.61% against a matched baseline of +3.01%. The
  unweighted excess is +4.76%. The rule's bets spread evenly over the five bands (18–22% each);
  the baseline drifts most on the longest prices (over 23.0: +4.5%; the other bands +2.2% to
  +3.3%). Per league: E0 +2.9%, I1 +3.2%, F1 +3.8%, D1 +5.4%, SP1 +7.0%, every range above 0.
- **Reproducibility (fixed 1 Nov 2026, before any judged number):** the first two
  development runs (41346853368, 41350946771) on the same cached prices and the same 27,167
  rows gave 2,113 and 2,115 rule bets at 20%. The inputs matched; B's chances differed in
  their last digits. scipy's default L-BFGS-B stop let last-bit float differences between
  runner machines move the Dixon-Coles fits behind the goal features by up to ~1e-5
  (reproduced locally by fitting on reordered rows). The goalscorer pipeline now fits
  Dixon-Coles and the goal model with tight options (`models.player_goals.FIT_OPTIONS`:
  ftol 0, gtol 1e-8), which brings that to ~1e-7, and rounds B's chance to 6 decimals
  before the threshold test (`player_goal_priced.P_DECIMALS`). Two runs of the fixed code
  started together (41354439416 and 41354442133) gave identical numbers in every league and
  pooled: 2,112 rule bets at 20%, the matched excess +4.62% (+3.91% to +5.30%), c 0.49
  (0.37 to 0.60), ROI −3.5% (−20.5% to +15.3%). T stays 20% (every candidate still clears
  0.3 a match) and the bands' quintiles are unchanged (6.5, 11.5, 17.0, 23.0). The judged
  read runs this code.
- **Hit rate:** 9.5%. **ROI** at the entry price: −3.8% (−20.3% to +14.0%).
- **Gap** (the best price's chance minus the rate scored): +2.85 points; the best price still
  carries margin.

Development spend: 35,688 credits (runs 41342996136 and 41344832136). Step 1, the judged
window's history since 10 Oct: 3,101 credits (runs 41346901027, which stopped on an HTTP 502,
and 41349243376), every listed match priced at entry and close (E0 30, SP1 30, D1 26, I1 40,
F1 29 events); not read.

### Building the steps (2026-11-01, after the sign-off)

- **Steps 3 and 1, the history:** `player_goal_backfill.py` (`odds-check.yml`
  `task=goalscorer-backfill`).
  - `design=dev` buys 2025/26 for step 3.
  - `design=forward` buys 2026/27 from 10 Oct for step 1, matches kicked off at least 3 h ago.
  - Per league and match day: one events list, then each event at the entry and the close
    with the ten books above.
  - A dry run (`cap=0`) comes first. Every body is cached in the paid `player-odds-history-`
    cache, and a recorded answer is never bought twice.
- **The development analysis** (`goalscorer-test.yml`, `player_goal_priced.py`):
  - B's 2025/26 walk-forward chances come from refits anchored at 2025-07-01 on all earlier
    matches. The refit dates can differ slightly from §11's 2023-anchored run; this is
    development only.
  - Each league's starters are joined to the cached prices.
  - T comes from the pooled bets per match at each candidate (volume only).
  - The development c, CLV (best-to-best, with the no-rule baseline) and ROI at that T are
    printed as development only.
  - B-cal is not in the development read; it joins the judged read.
- **The live log (step 2):** `player_goal_live.py`, run by every default-branch publish
  after ESPN's team news.
  - Entry: the first run 20–75 min before kickoff once both lineups are confirmed, or at
    20–30 min regardless.
  - Close: the first run 0–20 min before kickoff (`closes.yml` plans a publish 15 min
    before each cluster).
  - Each snapshot is one live call with the ten books (1 credit), never taken twice. The cap
    is 3,000 a month, with the 3,000 reserve. Logging stops on 1 Feb 2027 until the lead
    decides.
  - Rows go to data-log `odds_log/<code>_goalscorer_<YYYY-MM>.jsonl`.
  - §16 asks for the live entry "no earlier than kickoff − 75 min". A run between 30 and 75
    min with no confirmed lineups waits, so the entry is taken at confirmation, or at 30 min
    at the latest.
