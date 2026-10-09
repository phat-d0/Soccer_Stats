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
