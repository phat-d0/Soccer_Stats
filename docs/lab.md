# Research lab: rules and the match-model bake-off

Owner: the research-lab agent. This file holds the lab's rules, the pre-registration of each
experiment (written and committed before it runs) and the results. The research log for
signals and markets stays in `docs/edge.md`.

## The harness (`src/soccer_stats/lab/`)

- `harness.py`, generic: one row per event (a match or a player line) with a time.
  - `walk_forward`: refit on all earlier rows every 28 days and predict the next block.
  - `nested`: for each period (a season), pick the settings by running the same
    walk-forward over the previous period, trained only on rows before it (refits every
    84 days), and keep the lowest log loss. The scored period never chooses anything.
  - `Holdout`: the locked holdout. Predicting any row at or after its start raises
    `HoldoutLocked` unless `unlock(reason)` was called; opening prints a banner with the
    UTC time and the reason, and can append it to a log file.
- `metrics.py`, generic (any number of outcomes; player props use 2):
  - log loss, Brier score, a calibration table;
  - blend weight: `c` in `score_k = a_k + b·log(market_k) + c·log(model_k)` (the
    `match_calibration` blend), fitted on the out-of-sample rows, with a range from
    refits on bootstrap samples of whole matches;
  - CLV of the live rule (12% edge, one bet per match) at the market's price, against
    the margin-free close; ROI last;
  - every range resamples whole groups (matches).
- `features.py`, `models.py`, `run.py`: the match bake-off below.
- Tests: `tests/test_lab.py` (synthetic data): a leak test (no fit ever sees a row it
  predicts, tuning included), the locked holdout, a planted-edge test (a feature that
  carries information the market lacks passes; the market plus noise fails), and the
  features' no-look-ahead check.

For player markets: build rows with `time`, a group id (the match), `y`, features, the
market's chances at bet time, the bettable odds (NaN where a side isn't offered) and
the fair closing chances, then call `nested` and `metrics.evaluate` as `run.py` does.

## Bake-off 1: 1X2 models (pre-registered 2026-10-06, before any run)

**Question:** does any model, or a stack of a model with the opening price, add
information to Pinnacle's early 1X2 price out of sample?

**Data.** EPL, football-data (results, Pinnacle early and closing prices) and Understat xG,
2014/15–2025/26. 2014/15–2015/16 only warm up features and training. 2016/17 is
predicted but not scored: it gives the stack its first fits. **Development seasons:
2017/18–2024/25** (about 3,000 matches). **Holdout: 2025/26**, locked (its matches are
dropped before anything is computed until it is opened).

**Market.** Pinnacle's early price (football-data's pre-closing odds, one to three days
out), margin removed by Shin. Bets are taken at Pinnacle's early price; CLV is against
Pinnacle's Shin-fair close.

**Candidates** (1X2 only):

| # | Candidate | Inputs | Settings tuned (nested, per season) | Configs |
| --- | --- | --- | --- | --- |
| a | Dixon-Coles + xG (the live model), weekly refits, 730-day window | goals, xG | none | 1 |
| b | Hierarchical Poisson, MAP with Gaussian priors on attack/defence and team home edges (empirical Bayes: the prior scales are chosen on the previous season), time-decayed, target 0.7·xG + 0.3·goals, 730-day window | goals, xG | half-life {120, 365} days × prior sd {0.15, 0.4} × team home-edge sd {0, 0.1} | 8 |
| c | LightGBM multiclass | features below | leaves {4, 8} × min child {40, 160} × trees {150, 400} (learning rate 0.03) | 8 |
| d | Multinomial logistic, standardized, mean-imputed | features below | C {0.01, 0.1, 1} | 3 |
| e | Stack: the best of a–d by development log loss, blended with Pinnacle's early price (`score_k = a_k + b·log(early_k) + c·log(model_k)`), refitted every 28 days on earlier out-of-sample matches (first fit after 300) | a–d output, early price | none | 1 |

b–d refit every 28 days on all matches from 2015/16 up to the block (b uses its last 730
days). No neural nets.

**Features (c, d)**, from earlier matches only, fixed in advance:
- Elo going into the match (home, away, difference): K 20, home edge 60, goal-difference
  multiplier, new teams start at 1,420, 80% carried between seasons;
- xG for and against, goals for and against, per game over the last 6 and last 20
  league matches, home and away side (16 values);
- rest days since the last league match (capped at 14), each side.
- Home advantage is in every row's orientation (the intercepts).
- Team news: excluded. There is no history (FPL logging began 5 Oct 2026).
- No market prices: the market enters only in the stack (e) and in the scoring.

**Metrics** for each candidate, on the same development matches: log loss (with range)
and its gain over the early price, Brier score, a calibration table, the blend weight
`c` with its range, the live rule's CLV with its range, ROI with its range (last).

**Pass rule.** For a–d: the blend weight's range is above 0 **and** the CLV range is
above 0. For e (which contains the price): its log loss beats the early price's (gain
range above 0) **and** its CLV range is above 0. Profit alone never passes.

**Multiple testing.** 5 candidates × 2 pass metrics = 10 tests, so every range is
99.5% (Bonferroni). Tuning variants (8 + 8 + 3 configs) are chosen inside the training
years and not scored separately; I count them here: 21 configs in all, plus a and e.

**Holdout.** Opened once, after the development results are recorded in this file, for
these finalists:
1. every candidate that passes on development;
2. a (the live model, as the reference);
3. the best of b–d by development log loss, and e built on it.

On the holdout the pass rule is the same, at the Bonferroni level for the number of
finalists × 2. The run that opens it prints the time; I record it here.

**What would change things.** A pass on development and on the holdout means a handover
proposal to the moneyline agent (in this file). No pass means no change to the live
model, and a ranked list of next ideas.

**Run.** `odds-check.yml` with `task=lab` (development) or `task=lab-holdout` (with
`reason`, `finalists`, `stack_base`), print-only: it never pushes and never deploys. No
Odds API credits.
