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
