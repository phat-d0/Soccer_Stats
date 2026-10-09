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
