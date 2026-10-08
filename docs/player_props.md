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
