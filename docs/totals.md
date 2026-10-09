# Totals markets: corners, goal lines and team totals (round 8, second task)

The owner asked on 8 Oct about three two-sided totals markets:
- **A. corners**;
- **B. total goals at every line** (0.5–5.5, not just 2.5);
- **C. each team's own goal total.**

Research only: 0 credits, no key passed, nothing live changes. A ≤40-credit live probe of
which books price these markets is a separate decision for the owner. This work spends
nothing.

## Pre-registration (2026-10-09, before any data was loaded)

**Leagues and seasons.**
- Leagues: E0, SP1, D1, I1, F1 (with xG) and E1 (goals only).
- Every model is fitted only on earlier matches. Scored seasons are 2017/18–2024/25;
  earlier seasons train only.
- 2025/26 is dropped before anything is computed. It is the lab's locked season, and its
  prices are thin.

**Questions, per market and line.**
1. Is the model well calibrated?
2. Does it beat a simple baseline?
3. Where prices exist, does it add anything to them?

### A. Corners (a new count model)

- **Data:** football-data's HC/AC (corners), HS/AS (shots) and FTHG/FTAG.
- **Features,** all from the team's earlier league matches (the last 10, across seasons,
  at least 3 needed):
  - each side's corners for and against, and shots for and against, each as a log ratio
    to the league average over the 365 days before;
  - the match model's expected goals for each side, from the Dixon-Coles walk-forward:
    log supremacy and log total (game state and opponent strength).
- **Model** (primary): independent NB2 regressions for home corners and away corners
  (`models.player_counts.NBRegression`). The distribution of the total is their
  convolution.
  - Refitted every 28 days on the 730 days before (`lab.harness.walk_forward`).
  - Needs at least 1,000 earlier matches.
- **Bivariate check** (descriptive, not a test):
  - the correlation of the home and away Pearson residuals;
  - log loss of the total under independence vs a single NB2 fitted directly on total
    corners with the same features.
- **Baseline:** NB2 on total corners with the league's mean and dispersion over the 730
  days before (no team information).
- **Lines:** 8.5, 9.5, 10.5, 11.5.
- **Prices:** every football-data column name across the six leagues and nine seasons is
  scanned for a corner price, to confirm there are none (expected).

### B. Goal totals at 0.5–5.5

- **Model:** the existing Dixon-Coles walk-forward score matrix, unchanged:
  `backtest.walk_forward`, weekly refits, xG weight 0.7 (goals only for E1).
  P(over) = the matrix's mass above the line.
- **Baseline:** the league's over rate at that line over the 365 days before the match.
- **At 2.5 only:** Pinnacle's early and closing over/under prices (football-data
  P>2.5/P<2.5 and PC>2.5/PC<2.5), made margin-free with Shin's method, in seasons where
  both cover at least 90% of matches. Through `lab.metrics.evaluate`:
  - market = Pinnacle early, CLV vs the fair close;
  - pass = `lab.metrics.passes` (blend-weight range > 0 and CLV range > 0).
- The other lines have no prices in football-data: calibration and baseline only.

### C. Team totals at 0.5, 1.5 and 2.5

- **Model:** the same matrix's row and column marginals.
- **Baseline:** the league's home (or away) over rate at that line over the 365 days
  before.
- No prices.

### Tests and family

1. **Beats the baseline** (one test per league × market-line).
   - The paired log-loss gain (baseline minus model) for over/under.
   - Pass: its range is above 0.
   - Market-lines per league: corners 4, goal totals 6, team totals 6 (3 lines × home and
     away), so 16 per league and 96 tests across six leagues.
2. **Pinnacle at 2.5:** 2 metrics × 6 leagues = 12 tests.
3. **Family:** 108 tests, so Bonferroni ranges are 1 − 0.05/108 = 99.954%.
   - Ranges resample whole matches, with 20,000 draws so the far tails are resolved.
4. **Calibration** is described, not tested.
   - Reported per market-line: predicted vs observed over rate with a 95% range
     (calibration in the large), the logistic recalibration slope with a 95% range, and a
     10-bin table.
   - A Bonferroni-wide range would make "calibrated" too easy to claim, so calibration
     uses 95%.
   - A line counts as well calibrated if both 95% ranges contain the ideal (observed −
     predicted = 0; slope = 1).

**Note added before any real run** (from the synthetic tests): two checks at 95% per
line flag about 1 perfectly calibrated line in 10 by chance. Across 96 market-lines,
about 10 flags are expected from chance alone. A market counts as miscalibrated only
if its flags are many, or if they point the same way (for example, every line
over-predicting).

### Reading the result, per market

- **Well calibrated:** the line's calibration in the large and its slope are both
  consistent with ideal.
- **Beats a simple baseline:** test 1 passes.
- **Where an edge could plausibly be:**
  - Only where the model is calibrated and beats the baseline, and is either priced
    softly or not priced at football-data at all. A model that beats a climatology
    baseline is not evidence that it beats a bookmaker.
  - Where no prices exist, the honest statement is "untested against any price".
  - At 2.5 against Pinnacle, the round-2 E0 result (model 0.6794, close 0.6755; CLV
    −2.9%) is the prior.

### Code

`src/soccer_stats/edge/totals.py`; `odds-check.yml` `task=totals` with `league`. Tests
include no look-ahead.

## Results (2026-10-09, `odds-check.yml task=totals`, 0 credits, no key)

Runs: E0 37863800450, SP1 37863803667, D1 37863806642, I1 37863809156, F1 37863811872,
E1 37863814409.
- Scored seasons are 2017/18–2024/25: 2,374–4,236 matches per league. Corners score from
  2017/18 (D1 from 2018/19), once 1,000 earlier matches exist.
- Gain ranges are 99.954% (108 tests). Calibration ranges are 95%.

**Corner prices.** None. Of 156–157 football-data columns per league, none is a corner
price. Corners are untested against any price.

### Does the model beat the simple baseline? (test 1, lines passing)

| League | Goal totals (of 6) | Team totals (of 6) | Corners (of 4) |
| --- | --- | --- | --- |
| E0 | 0 | 6 | 0 |
| SP1 | 3 (2.5, 4.5, 5.5) | 6 | 0 |
| D1 | 1 (3.5) | 6 | 0 |
| I1 | 0 | 6 | 0 |
| F1 | 2 (2.5, 3.5) | 6 | 0 |
| E1 | 0 | 0 | 0 |
| **All** | **6 of 36** | **30 of 36** | **0 of 24** |

- **Team totals** beat the league-rate baseline clearly. Log-loss gains are +0.02 to
  +0.06 per match, with every range above 0, in all five top leagues.
- **Goal totals:** the point gains are positive almost everywhere (+0.001 to +0.017),
  but only 6 of 36 ranges clear the Bonferroni bar.
- **Corners:** gains are −0.005 to +0.002 and no range clears 0.
- **E1** (goals only): no line beats the baseline in any market. Its goal-total log loss
  is worse than the baseline at 5 of 6 lines (all but 5.5).

### Is it well calibrated? (95%; about 1 line in 10 is flagged by chance)

| Market | Lines calibrated | Recalibration slope (1 = ideal) | Reading |
| --- | --- | --- | --- |
| Team totals, top 5 | 24 of 30 | 0.83–1.06, mostly 0.94–1.05 | **Well calibrated.** Six flags, mostly the 2.5 line over-predicted by 1–2.5 points (SP1, I1). |
| Goal totals, top 5 | 8 of 30 | 0.46–0.96, mostly 0.7–0.85 | **Over-confident.** Predictions spread too far: high totals too high, low totals too low. The average rate is close (within ±2 points). |
| Corners, top 5 | 0 of 20 | 0.14–0.75 | **Badly over-confident.** The features carry almost no signal for the total. |
| E1, every market | 0 of 16 | 0.25–0.69 | **Over-confident.** The goals-only model is too sharp for the Championship. |

### Corners: independent vs bivariate check (descriptive)

- The home and away residuals are negatively correlated in every league (−0.16 to
  −0.23). When one side wins more corners, the other wins fewer, so independent counts
  give the total too wide a spread.
- The direct NB on the total has a better count log loss than the independent
  convolution everywhere. It beats the league-average baseline only in E0, SP1 and I1, by
  0.002–0.003 a match. The independent model never beats the baseline.

### Goal totals at 2.5 against Pinnacle (test 2; 2019/20–2024/25, the seasons with prices)

| League | Matches | Log loss: model / early / close | Blend c (range) | 12% bets | CLV (range) | Pass |
| --- | --- | --- | --- | --- | --- | --- |
| E0 | 2,192 | 0.6783 / 0.6732 / 0.6730 | +0.02 (−0.44 to +0.48) | 294 | −3.0% (−4.7 to −1.5) | no |
| SP1 | 2,200 | 0.6780 / 0.6704 / 0.6688 | −0.03 (−0.43 to +0.33) | 416 | −4.1% (−5.3 to −3.0) | no |
| D1 | 1,727 | 0.6574 / 0.6511 / 0.6470 | −0.00 (−0.43 to +0.50) | 215 | −3.7% (−5.4 to −1.8) | no |
| I1 | 2,177 | 0.6828 / 0.6770 / 0.6749 | +0.02 (−0.41 to +0.42) | 321 | −3.0% (−4.5 to −1.5) | no |
| F1 | 1,954 | 0.6846 / 0.6769 / 0.6718 | −0.06 (−0.54 to +0.39) | 327 | −3.4% (−4.8 to −1.8) | no |
| E1 | 3,164 | 0.6957 / 0.6832 / 0.6824 | −0.05 (−0.36 to +0.28) | 857 | −3.7% (−4.4 to −3.1) | no |

The model trails Pinnacle's early price by 0.005–0.013 log loss and earns no blend weight.
The 12% rule's CLV range is below 0 everywhere. This repeats the round-2 E0 result (CLV
−2.9%) in six leagues.

## Verdict (totals), per market

- **A. Corners: no.**
  - The pre-registered model doesn't beat a league-average baseline at any line in any
    league, and it is badly over-confident.
  - Football-data has no corner prices, so there is nothing to test against.
  - No plausible edge from this model. Corners would need different information
    (playing styles, live game state), not a reworked version of these features.
- **B. Goal totals at 0.5–5.5: no.**
  - The model is directionally better than a league average but rarely significantly so,
    and its spread is over-confident at every line.
  - At 2.5, where prices exist, it loses to Pinnacle in all six leagues (CLV −3% to −4%).
  - Other lines have no prices in football-data, so they are untested against any book.
    The over-confidence and the 2.5 result make an edge at the other lines unlikely.
- **C. Team totals: well calibrated and beats a simple baseline in the top five leagues,
  but untested against any price.**
  - This is the only market where the model is both calibrated and clearly better than
    climatology.
  - That isn't evidence it beats a bookmaker. Team totals come from the same expected
    goals that lose to Pinnacle on 1X2 and on the 2.5 total, and books price them off
    those same sharp lines.
  - Any edge would have to come from a book pricing team totals more softly than its own
    match lines. Only the ≤40-credit live probe the lead proposed (awaiting the owner's approval) can tell: which books
    quote team totals on both sides, and at what margin. Worth that probe; not worth
    building anything before it.
- **E1:** the goals-only model is over-confident in every market. It shouldn't be shown
  for E1 totals without recalibration.
- Nothing passes; no handover; no live change.

## Live market probe (round 8, third task; owner-approved 9 Oct, hard cap 40 credits; written before any call)

**Question.** For each of the six leagues (E0, SP1, D1, I1, F1, E1): which bookmakers on
The Odds API quote these markets on both sides, at which lines, and at what margin?
- (a) team totals;
- (b) alternate goal totals;
- (c) corners totals.

BTTS and player shots on target are noted in passing.

**Budget.**
- Hard cap 40 credits, counted from `x-requests-last`.
- A call is skipped if its maximum cost would pass the cap, or would leave under 3,000
  credits on the shared key (`player_goal_odds.CappedBudget`).
- Cap 0 is a dry run: only the free events lists are fetched.
- The key goes only to this task. It is never printed.

**Method,** league by league in the order E0, SP1, D1, I1, F1, E1:
1. **Events list** (free). Take the soonest upcoming match; markets open nearer
   kickoff.
2. **Discovery.** `/events/{id}/markets` in all five regions lists the market keys each
   bookmaker offers. About 1 credit; the first call's real cost sets the estimate for the
   rest.
3. **One odds call.** `/events/{id}/odds` for the target keys discovery found, with a
   `bookmakers=` list of up to 10 books (10 books cost one region), so the cost is
   about one credit per market.
   - Target keys, in priority order: team totals (`team_totals`,
     `alternate_team_totals`), alternate goal totals (`alternate_totals`), any key with
     "corner", `btts`. Player shots on target are added in E0 only.
   - Each league gets an even share of the credits left. Lower-priority keys are
     dropped first.

**Outputs, per league and market** (no model, no bets):
- the books listing it;
- the books quoting both sides at the same line (and for team totals the same team);
- the lines;
- the margin per two-sided pair (1/over + 1/under − 1): median and range per book.

Also reported:
- every market key discovered;
- the credits spent and the balance before and after.

**Reading.**
- A two-sided market with a median margin of 5% or less is "close to fair": a model
  could be checked against it.
- 5–8% is soft but testable.
- Above 8%, or one-sided, isn't bettable at our level of accuracy. Player shots at
  FanDuel were over-only, and the goalscorer books listed no "No" price at all.
- Team totals are judged at the book with the lowest median margin.

**Code.** `edge/market_probe.py`; `odds-check.yml` `task=markets` with `cap`. The key is
passed only to this task, and cap 0 is a dry run.

### Results (2026-10-09 05:39 UTC; `odds-check.yml task=markets cap=40`, run 37889595473)

**Credits.** 39 of the 40-credit cap: 22,535 before, 22,496 after.
- Events lists: free.
- Discovery: 1 credit per league.
- Odds: 4–6 per league, one per market.
- The dry run before it (run 37889446400) spent 0.

**Matches probed** (the soonest in each league):
- E0: Arsenal v Leeds, 10 Oct.
- SP1: Málaga v Espanyol, 9 Oct.
- D1: Dortmund v Bremen, 9 Oct.
- I1: Genoa v Fiorentina, 10 Oct.
- F1: Lens v Lyon, 9 Oct.
- E1: West Ham v QPR, 9 Oct.

Discovery found 62–70 books per league. The top five leagues have about 75 market keys,
including team totals, alternate goal totals, corners (match, team, first half), cards,
BTTS and many player props. The Championship has 32 keys: no team totals and no player
props.

**Two deviations from the plan,** both found in the output:
- The corner keys counted as targets ahead of BTTS (the rule took every key containing
  "corner"). With a 6-credit share per league, BTTS and shots on target were priced only
  in the Championship. Discovery still says which books list them.
- In E0 the ten books chosen didn't include Pinnacle. Pinnacle lists alternate goal
  totals, corners and BTTS in every league, E0 included (the discovery bodies), so E0 has
  no Pinnacle price for goal totals or corners: the Pinnacle ranges below cover five
  leagues. It lists no goal team totals anywhere, so no team-total price is missing.
  (Corrected by the lead at merge; the PR said Pinnacle listed only corners.)

**Median two-sided margin per book** (1/over + 1/under − 1 at the same line; ranges
across books):

| Market | E0 | SP1 | D1 | I1 | F1 | E1 |
| --- | --- | --- | --- | --- | --- | --- |
| Team totals, alternate lines 0.5–5.5 | 10 books, 5.0–8.7% (Bovada 5.0, FanDuel 6.0, BetMGM 7.0) | 7 books, 5.9–8.7% (FanDuel 5.9) | 7, 6.5–9.3% (FanDuel 6.5) | 7, 6.4–9.9% (FanDuel 6.4, BetMGM 6.5) | 7, 6.3–9.7% (FanDuel 6.3, DraftKings 6.4) | none offered |
| Team totals, main line | FanDuel 6.3%, BetMGM 7.6% | 6.0%, 8.0% | 6.4%, 7.8% | 6.8%, 8.4% | 6.4%, 7.8% | none |
| Alternate goal totals 0.5–8.5 | 10 books, 4.0–6.7% (Pinnacle not asked) | Pinnacle 3.7%, others 5.0–6.5% | Pinnacle 4.8%, others 5.0–6.4% | Pinnacle 3.6%, others 4.7–8.8% | Pinnacle 3.7%, others 5.0–6.1% | Pinnacle 4.0%, UK books 6.1–10.0% |
| Corner totals (match) | 10 books, 6.6–9.6% (Pinnacle not asked) | Pinnacle 5.6%, others 7.0–9.8% | Pinnacle 6.0%, 7.0–9.4% | Pinnacle 5.6%, 7.6–9.7% | Pinnacle 6.2%, 8.0–10.1% | Pinnacle 5.4%, LeoVegas 8.8% |
| Corner team totals | 9 books, 6.6–11.1% | Pinnacle 6.9%, 7.0–11.1% | Pinnacle 6.9%, 7.5–11.2% | Pinnacle 7.0%, 7.5–11.1% | Pinnacle 6.6%, 8.2–11.1% | Pinnacle 5.8% |
| BTTS | listed by 32 books | 24 | 24 | 23 | 22 | 10 books priced: Pinnacle 4.5%, others 6.9–8.2% |
| Player shots on target (listed) | 7 books (FanDuel, Kambi books, William Hill) | 6 | 6 | 5 | 5 | none |

- **Every market here is quoted on both sides** at most books and lines. This is
  unlike player shots (FanDuel over-only) and the goalscorer market (no "No" price
  anywhere).
- **Betfair's exchanges** (AU, UK) show 0.7–1.2% on goal totals, but their prices are
  before commission (about 5% of winnings), so the real cost is higher.
- **Pinnacle quotes no goal team totals on this API** in any league, only corners. The
  team totals have no sharp reference price. The cheapest quotes are FanDuel's (5.9–6.8%
  median) and Bovada's in E0 (5.0%).
- Margins are medians over all lines a book quotes. Lines far from the middle carry more
  margin.

**Reading, as pre-registered.**
- **Team totals: soft but testable, not close to fair.**
  - The best book is 5.0% (Bovada, E0 only) and FanDuel is about 6% in every top league.
    That is roughly twice Pinnacle's margin on the match total.
  - The model was well calibrated on team totals and beat a league average. To be
    bettable it would also have to beat a ~6% margin, with no sharp price to tell how
    good the book's line is.
  - Testing that needs prices over time: the closing team-total line at FanDuel, logged
    like the DraftKings h2h log. A one-off probe can't show it.
- **Alternate goal totals:** close to fair at Pinnacle (3.6–4.8%) and soft elsewhere
  (5–7%). The model already loses to Pinnacle on the 2.5 line.
- **Corners:** soft everywhere (Pinnacle 5.4–6.2%, others 6.6–11%), and the corner
  model beats no baseline.
- **Championship:** no team totals and no player props on this API. Only goal totals,
  corners and BTTS.
- No live change. No further spend is proposed here; logging FanDuel's team totals would
  be a new owner decision.

## Cost of logging team totals live (round 9; estimate only, 0 credits, nothing switched on)

The owner asked on 9 Oct for an exact cost before deciding whether to log team-total
prices, so the model's CLV can be tested. **This is an estimate. No call was made and
nothing is live.**

**What would be logged.**
- **Books:** FanDuel in E0, SP1, D1, I1 and F1, plus Bovada in E0. The Championship has no
  team totals on The Odds API.
- **Markets:** `team_totals` (the main line per team), and optionally
  `alternate_team_totals` (0.5–5.5).
- **Snapshot plans per match:**
  - lean: the close only;
  - base: 24 hours before kickoff and the close;
  - rich: 24 hours, 6 hours and the close.

**Cost per call** (the market probe's `x-requests-last`, run 37889595473):
- One `/events/{id}/odds` call per match per snapshot. It costs 1 credit per market per
  region. FanDuel and Bovada are both in `us`, and up to ten books cost one region, so a
  call costs 1 credit (`team_totals`) or 2 (plus `alternate_team_totals`).
- The events list that gives the ids is free (cost 0 in the probe log).
- So the cost is matches × snapshots × markets. Matches kicking off together don't share
  a call.

**Method.**
- `odds_feed.estimate_snapshot_credits` replays each league's real fixture calendar
  (Understat) against the publish schedule (`publish_runs`).
- A snapshot is the first scheduled run at or after its time. The close is the last
  scheduled run before kickoff.
- `soccer-stats estimate-team-totals --months 2026-10,2026-11,2026-12`; `backfill.yml`
  `estimate_month` with a month list (no key).
- Run: `backfill.yml` run 37940224476, 0 credits.

### Credits per month (`team_totals` / with `alternate_team_totals`)

| Month | Matches (5 leagues) | Lean (close) | Base (24 h + close) | Rich (24 h + 6 h + close) |
| --- | --- | --- | --- | --- |
| Oct 2026 | 189 | 189 / 378 | 378 / 756 | 567 / 1,134 |
| Nov 2026 | 158 | 158 / 316 | 316 / 632 | 474 / 948 |
| Dec 2026 | 165 | 165 / 330 | 330 / 660 | 495 / 990 |
| **Average a month** | 171 | **171 / 341** | **341 / 683** | **512 / 1,024** |

**Per league, base plan, `team_totals` only:**

| League | Oct | Nov | Dec |
| --- | --- | --- | --- |
| Premier League | 76 | 64 | 120 |
| La Liga | 72 | 70 | 60 |
| Bundesliga | 72 | 54 | 54 |
| Serie A | 86 | 74 | 60 |
| Ligue 1 | 72 | 54 | 36 |

- December's Premier League (60 matches) includes the festive midweek rounds.
- November has an international break.
- Every match's close lands within 30 minutes of kickoff on the schedule. European
  kickoffs fall in the 10:00–22:00 UTC window, where publish runs every 15 minutes.
- November and December still use placeholder kickoff times on Understat. That changes
  when a snapshot is taken, not how many there are, so the cost holds.

### Against the balance

The balance is about 22,496 on the shared key. Live match odds already take about 4,050 a
month (`docs/leagues.md`), which on its own lasts about 5½ months.

| Plan | Extra a month | All live spend a month | Months on 22,496 |
| --- | --- | --- | --- |
| None (today) | 0 | ≈ 4,050 | ≈ 5.6 |
| Lean, `team_totals` | 171 | ≈ 4,220 | ≈ 5.3 |
| Base, `team_totals` | 341 | ≈ 4,390 | ≈ 5.1 |
| Base, with alternates | 683 | ≈ 4,730 | ≈ 4.8 |
| Rich, with alternates | 1,024 | ≈ 5,070 | ≈ 4.4 |

These are upper bounds: GitHub throttles scheduled runs, so some snapshots would be
missed. The baseball app's use of the shared key comes on top.

### How fast priced matches build up (from 9 Oct 2026)

| | All five leagues | Premier League alone |
| --- | --- | --- |
| After 4 weeks | 203 matches | 40 |
| After 8 weeks | 357 matches | 80 |
| Time to ~150 matches | about 3 weeks | about 13 weeks |

Each match gives two team totals (home and away), so the number of priced lines is about
twice the match count at the main line, and more with alternates.

### How it would plug in (not built)

1. **Fetch.** In `publish`, alongside the DraftKings match odds: for each top-five match
   due a snapshot, one `/events/{id}/odds` call for FanDuel (and Bovada in E0), markets
   `team_totals` (+ alternates).
   - Use the same budget guard as the matchday leagues: stop below 3,000 credits on the
     freshest balance (`MATCHDAY_RESERVE_CREDITS`), so the Premier League's match odds
     and the baseball app keep priority.
   - Cache each body per event and snapshot.
2. **Log.** Write `odds_log/<code>_team_totals_<YYYY-MM>.jsonl` on `data-log`, append-only
   and deduplicated like the DraftKings log. One row per match × team × line × snapshot:
   - over and under prices, the margin-free chance and the book;
   - `snapshot` (24h, 6h or close), `fetched_at` and minutes before kickoff;
   - the model's chance at that moment: the team's goal marginal from the same score
     matrix that sets `p`. The totals research found this marginal well calibrated in the
     top five leagues.
3. **Score.** CLV per row against FanDuel's own de-margined close for that team and line:
   - the snapshot's chance vs the close's fair chance, and the model's side;
   - plus log loss, model vs the close.
   - Through `lab.metrics` with ranges that resample whole matches. With no sharp
     reference (Pinnacle lists no goal team totals), the close is the only benchmark.
4. **Rules.** No bets and no paper trades until CLV clears its range. Any rule would be
   pre-registered in `docs/lab.md` before the data are read.

**Recommendation, if the owner wants it:** the base plan with `team_totals` only, about 340
credits a month.
- CLV needs an entry price and a close. The lean plan (about 170) has only the close, so
  it can compare the model with the closing price but can't measure CLV.
- The rich plan's 6-hour snapshot adds a second entry point near team news. It is worth
  adding only if the base plan shows something.
- Alternates double the cost for lines far from the middle, which carry more margin.

## Live team-total logging (built; owner approved the base plan on 9 Oct)

FanDuel's `team_totals` (Bovada too in the Premier League, in the same `us` call; no
alternates) in E0, SP1, D1, I1 and F1. Two snapshots per match, about 340 credits a
month. Data only: no paper trades, nothing new in the app. Code: `team_totals.py`.

**When a snapshot is taken (each publish run):**
- **Look:** the first run that finds the kickoff 18 to 30 hours away.
  - Runs are at least hourly, so a throttled or skipped run still leaves several chances
    inside the 12-hour window.
- **Close:** the first run within 30 minutes of kickoff.
  - Up to 75 minutes out, a run also takes the close if no later scheduled run is left
    before kickoff (kickoffs outside 10:00–22:00 UTC, where runs are hourly).
  - A run that misses the window simply misses that snapshot. `minutes_before` records
    how close each one was.
- **Never twice:** every call is recorded (event, snapshot, cost, balance), and a recorded
  pair is never fetched again, even when FanDuel returned nothing.
  - The record lives on data-log (`odds_log/team_totals_calls_<YYYY-MM>.jsonl`, read
    before each build) and in the local cache.
- **Event ids:** taken from the league's cached DraftKings body, since every book shares
  the same ids. A match with no event there is skipped, not looked up.

**Budget:**
- No call when the freshest balance seen is under 3,000 credits
  (`MATCHDAY_RESERVE_CREDITS`). The freshest balance is the newest of any league's
  DraftKings meta and the last team-total call; it is re-checked after every call from
  the response header.
- No call once this calendar month's team-total calls reach 450 credits (`MONTHLY_CAP`),
  a hard stop above the ~340 estimate.
- The Premier League's match odds and the baseball app keep priority.
- Only the default branch fetches (`TEAM_TOTALS_DIR` is set there alone), so a dispatch
  on another branch never spends credits it couldn't log.

**Log:** `odds_log/<code>_team_totals_<YYYY-MM>.jsonl` on data-log, append-only and
deduplicated. One row per book × team × line with both sides priced:
- league, home, away, kickoff, event_id, snapshot (look/close), book, team, side, line;
- over and under prices, the Shin margin-free `fair_over`/`fair_under`, and `margin`;
- `fetched_at` (FanDuel's `last_update`, else the download time, with `time_source`),
  `downloaded_at` and `minutes_before`;
- `p_model_over`: the model's chance that the team scores more than the line. It comes
  from the card's own score matrix, the same one that sets `p`, with team news applied
  in the Premier League (`goals_cdf` on each card, cumulative to 6 goals).

The DraftKings log reader only reads `<code>_<YYYY-MM>.jsonl`, so the two logs never mix.

**Publish log line:** "Team totals: N calls, C credits, R rows; month M of 450 credits,
credits left X", plus the reason when it stopped.

**Analysis:** `odds-check.yml` `task=team-totals`, no key. It pairs each look with the
close for the same match, book, team and line, adds the team's goals from football-data,
and scores FanDuel rows through `lab.metrics.evaluate`:
- the model against the look's fair price (gain, blend weight);
- the 12% rule's CLV against FanDuel's de-margined close;
- model vs close log loss;
- calibration tables for the model and the close.

It prints "not enough data yet" below 50 settled matches with both snapshots. That is
about two weeks across the five leagues at the estimated pace.

## Corners bake-off (round 11; owner's request 9 Oct; pre-registered 2026-10-09, before any code or data)

**Why.** In round 8 the corner model (independent home/away NB2 on form features) lost to
a league average at all 24 lines and was badly over-confident. The owner asked for a
proper corners model. 0 credits; research only.

**Data and seasons.**
- Data: football-data's corners (HC/AC), shots (HS/AS) and results, and the match model's
  expected goals (the Dixon-Coles walk-forward, as in round 8). Leagues: E0, SP1, D1, I1,
  F1 and E1.
- Data years 2014/15–2024/25. 2025/26 is never loaded.
- Development is scored on 2017/18–2023/24. Matches from 1 July 2024 are dropped before
  anything is computed, including the match model's fit.
- The holdout, 2024/25, is opened once per league in a separate run, with a logged reason
  (`lab.harness.Holdout`), for the development finalists only.
- Every candidate is refitted every 28 days on the 730 days before the block
  (`lab.harness.walk_forward`) and needs 1,000 earlier matches.
- Features use earlier matches only. The match model's expected goals come from its own
  weekly walk-forward.

**Candidates** (each gives home and away corner distributions, plus the total):
- **(a) Baseline.** The league average over the window: an NB2 for home corners, one for
  away corners and one for the total, at the league mean and moment-matched dispersion.
- **(b) Team averages, shrunk.**
  - Each team's corners for and against over the window, as ratios to the league's
    home/away means, shrunk toward 1 with 10 pseudo-matches.
  - λ_home = μ_home · for(home) · against(away); λ_away = μ_away · for(away) ·
    against(home).
  - NB2 per side with a moment-matched dispersion. The total is the convolution.
- **(c) Corner ratings,** Dixon-Coles style.
  - log λ_home = μ + h + att(home) + def(away); log λ_away = μ + att(away) + def(home).
  - Time decay with a half-life of 180 days, and Gaussian priors on att/def (sd 0.2),
    fitted by MAP on the corner counts (the lab's `HierPoisson`, one shared home edge).
  - NB2 per side with a moment-matched dispersion. The total is the convolution.
- **(d) Ratings plus match context.**
  - An NB2 regression per side on log λ from (c) plus context: the match model's log
    supremacy and log total, and each side's 10-match shots for/against form (log ratios
    to the league).
  - L2-regularised (`NBRegression`). The total is the convolution.
- **(e) Direct total.**
  - The total's mean is (d)'s λ_home + λ_away. Its NB2 dispersion is estimated on the
    totals themselves (moment-matched on the window), which absorbs the negative
    home/away correlation (about −0.2 in round 8).
  - Total lines only; its team lines are (d)'s.
- No separate bivariate model: (e) handles the correlation for the total, and team lines
  are marginals.

**Lines.**
- Match total: over 8.5, 9.5, 10.5, 11.5 (four lines).
- Team corners: home and away, each over 3.5, 4.5, 5.5 (six lines).

**Metrics and pass rule.**
- For each candidate and line group (total, team), the per-match mean log-loss gain over
  baseline (a) across that group's lines. Ranges resample whole matches.
- Each line's recalibration slope (logistic of the outcome on logit(p); 1 is ideal).
- All candidates are scored on the same matches: those every candidate predicts.
- A candidate **passes** a group in a league if:
  - its gain range is above 0, at the Bonferroni level; and
  - every line in the group has a slope point estimate within **0.80–1.25**.
- **Development family:** (b), (c) and (d) on 2 groups, (e) on 1, so 7 tests per league
  and 42 in all: 99.881%.
- **Finalists:** per league and group, the candidate with the best development gain
  (passing or not).
- **Holdout family:** 2 groups × 6 leagues = 12 tests, 99.583%. A finalist passes if it
  meets the same rule on 2024/25.
- Log loss, Brier and calibration tables are also reported.

**Reading.**
- A league's corners model is recommended only where its finalist passes on the holdout.
  It would go in the app as display-only corner over/unders on the match sheet, labelled
  as model estimates that have not been tested against any bookmaker price.
- A price test would mean logging Pinnacle's live corner prices, about a 5.5–6% margin in
  round 8's probe. That is a separate owner decision with its own cost estimate. Nothing
  is spent here.

**Code.** `edge/corners.py`; `odds-check.yml` `task=corners` with `league` (no key).
- Holdout runs take `reason` and `finalists` ("total=<x>,team=<y>").
- Tests include no look-ahead.

### Corners bake-off: development results (2017/18–2023/24; recorded before any holdout)

Runs: `odds-check.yml` `task=corners`, commit 3962d0d, 0 credits, no key: E0 38003805694,
SP1 38003808230, D1 38003810294, I1 38003812234, F1 38003814395, E1 38003816937. (A first
batch on ec0cb6a failed in five leagues on a read-only array in the ratings fit; fixed in
3962d0d, with a test. The Spain run in that batch gave the same numbers.) Gain = mean
per-match log-loss gain over the league average (a) across the group's lines, range at
99.881%; slopes = the lowest and highest line slope (band 0.80–1.25).

Each team's corners (over 3.5 / 4.5 / 5.5, home and away):

| League | Matches | (b) team averages | (c) corner ratings | (d) ratings + context |
| --- | --- | --- | --- | --- |
| E0 | 2,558 | +0.046 (+0.032..+0.059), 0.84–0.98, **pass** | +0.046 (+0.031..+0.060), 0.73–0.94 | +0.047 (+0.032..+0.062), 0.73–0.88 |
| SP1 | 2,570 | +0.016 (+0.005..+0.026), 0.67–0.75 | +0.016 (+0.004..+0.027), 0.62–0.70 | +0.015 (+0.003..+0.026), 0.56–0.72 |
| D1 | 1,980 | +0.032 (+0.018..+0.045), 0.79–0.94 | +0.033 (+0.018..+0.047), 0.73–0.85 | +0.035 (+0.020..+0.051), 0.72–0.87 |
| I1 | 2,562 | +0.036 (+0.024..+0.047), 0.85–0.93, **pass** | +0.038 (+0.026..+0.051), 0.78–0.89 | +0.038 (+0.025..+0.051), 0.75–0.84 |
| F1 | 2,399 | +0.013 (+0.004..+0.022), 0.67–0.87 | +0.008 (−0.003..+0.019), 0.56–0.66 | +0.014 (+0.003..+0.024), 0.62–0.75 |
| E1 | 3,701 | +0.009 (+0.002..+0.016), 0.59–0.72 | +0.009 (+0.000..+0.017), 0.53–0.66 | +0.011 (+0.002..+0.019), 0.57–0.72 |

Match total (over 8.5 / 9.5 / 10.5 / 11.5):

| League | (b) | (c) | (d) | (e) direct total |
| --- | --- | --- | --- | --- |
| E0 | +0.003 (−0.005..+0.011), 0.53–0.78 | +0.001, 0.42–0.62 | +0.001, 0.45–0.64 | +0.000, 0.41–0.57 |
| SP1 | +0.006 (−0.002..+0.015), 0.57–0.87 | +0.004, 0.51–0.70 | +0.003, 0.49–0.71 | +0.001, 0.44–0.63 |
| D1 | +0.001 (−0.008..+0.011), 0.38–0.53 | +0.001, 0.41–0.51 | −0.001, 0.33–0.45 | −0.003, 0.31–0.41 |
| I1 | +0.009 (−0.001..+0.018), 0.72–0.87 | +0.007, 0.57–0.77 | +0.005, 0.58–0.74 | +0.003, 0.53–0.66 |
| F1 | +0.000 (−0.008..+0.008), 0.36–0.64 | −0.007, 0.20–0.38 | −0.003, 0.32–0.48 | −0.005, 0.29–0.42 |
| E1 | +0.001 (−0.006..+0.007), 0.43–0.58 | −0.004, 0.34–0.44 | −0.003, 0.36–0.44 | −0.004, 0.32–0.40 |

Reading:
- **Each team's corners carry real information.** Every candidate beats the league average
  in every league, and the range clears 0 for 17 of 18 league × candidate pairs. The gain is
  largest in E0 (+0.046 a line) and smallest in the Championship (+0.01).
- **But almost all are over-confident** (slopes below 0.80). Two pass the full rule: (b), the
  shrunk team averages, in E0 and Serie A. The ratings models, (c) and (d), spread their
  chances further and are less calibrated.
- **The match total is not predictable beyond the league average.** No candidate's range
  clears 0 in any league, and every one is over-confident (slopes 0.20–0.87). The direct
  total (e) is the worst of them, so the home/away correlation is not what goes wrong.
- **Finalists, by the pre-registered rule (best development gain, passing or not):** total
  (b) in all six leagues; team lines (d) in E0, D1, F1, E1 and (c) in SP1, I1. In E0 and I1
  the rule picks a finalist that does not pass, over (b), which does. That is the rule as
  written. The holdout runs exactly these finalists: E0, D1, F1, E1 `total=b,team=d`; SP1,
  I1 `total=b,team=c`.

### Corners bake-off: holdout results (2024/25, opened once per league) and verdict

Runs: `odds-check.yml` `task=corners` with `reason` and `finalists`, commit c491838, 0
credits, no key: E0 38004172501, SP1 38004174573, D1 38004177405, I1 38004179990, F1
38004182745, E1 38004185144. Each log prints "HOLDOUT OPENED at 2026-10-09T23:24Z" with the
reason. Ranges at 99.583% (12 tests).

| League | Matches | Total: (b) gain, slopes | Team: finalist, gain, slopes | Passes |
| --- | --- | --- | --- | --- |
| E0 | 364 | −0.003 (−0.026..+0.019), 0.17–0.55 | (d) +0.045 (+0.007..+0.082), 0.66–0.98 | no |
| SP1 | 363 | +0.003 (−0.016..+0.022), 0.32–0.88 | (c) +0.013 (−0.014..+0.040), 0.44–0.83 | no |
| D1 | 284 | −0.001 (−0.023..+0.021), 0.27–0.74 | (d) +0.014 (−0.028..+0.051), 0.48–0.70 | no |
| I1 | 352 | +0.015 (−0.011..+0.041), 0.67–1.05 | (c) +0.017 (−0.016..+0.049), 0.55–0.76 | no |
| F1 | 291 | −0.005 (−0.025..+0.014), −0.18–0.52 | (d) +0.032 (+0.001..+0.061), 0.62–1.17 | no |
| E1 | 522 | −0.001 (−0.019..+0.016), 0.18–0.62 | (d) +0.032 (+0.010..+0.053), 0.72–1.18 | no |

**Verdict: no candidate passes in any league, so nothing is recommended for the app.**
- **Match total corners:** the league average is as good as any model, in development and
  on the holdout. The app should not show a model total-corners line.
- **Each team's corners:** the models know something. On the holdout the gain range clears
  0 in E0, Ligue 1 and the Championship, and in development it clears 0 almost everywhere.
  But every finalist has at least one line outside the 0.80–1.25 slope band. Development
  and holdout both say the same thing: the chances are too spread out. On ~300 holdout
  matches the slope ranges are wide (roughly ±0.4), so the band test there is noisy, but
  the development slopes (2,000–3,700 matches) point the same way.
- **What could follow (not done, needs its own pre-registration):** a per-league
  recalibration of the team-corner model, logit(p) = a + b·logit(p_model) fitted on earlier
  seasons only, as B-cal did for the goalscorer. Testing it fairly needs fresh data: the
  2024/25 holdout is now spent, and 2025/26 has not been touched. Display only.
- **Prices:** football-data has no corner prices, so nothing here says anything about
  beating a bookmaker. A price test would mean logging Pinnacle's live corner prices (about
  5.5–6% margin in round 8's probe). That is a separate owner decision with its own cost
  estimate. Nothing was spent.
