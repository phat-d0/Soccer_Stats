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

### Development results (run 37542756710, 2026-10-06 22:47 UTC, holdout locked)

2,934 matches (2017/18–2024/25) that every candidate a–d predicted and Pinnacle priced
early. Ranges are 99.5% (Bonferroni for 10 tests) and resample whole matches. Bets: the
live rule (12% edge, one per match) at Pinnacle's early price, 1 unit each. The stack's
base was chosen by the pre-registered rule: a, the Dixon-Coles baseline, had the best
development log loss of a–d.

| Candidate | Log loss | vs early price (gain, range) | Brier | Blend weight c (range) | Bets | CLV (range) | ROI (range) | Pass |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Pinnacle early (reference) | 0.9493 | – | – | – | – | – | – | – |
| a Dixon-Coles + xG | 0.9589 | −0.0096 (−0.0180 to −0.0012) | 0.5676 | +0.11 (−0.18 to +0.38) | 1,481 | −4.1% (−5.0 to −3.4) | +2.3% (−13.4 to +20.0) | no |
| b Hierarchical Poisson | 0.9618 | −0.0125 (−0.0210 to −0.0033) | 0.5699 | +0.06 (−0.27 to +0.35) | 1,676 | −5.0% (−5.8 to −4.3) | +1.0% (−15.0 to +18.3) | no |
| c LightGBM | 0.9749 | −0.0256 (−0.0370 to −0.0153) | 0.5784 | −0.03 (−0.23 to +0.19) | 1,918 | −4.0% (−4.7 to −3.3) | −4.7% (−16.5 to +8.8) | no |
| d Multinomial logit | 0.9653 | −0.0160 (−0.0254 to −0.0063) | 0.5722 | +0.04 (−0.21 to +0.31) | 1,629 | −3.6% (−4.2 to −2.8) | −1.0% (−14.1 to +13.1) | no |
| e Stack (a + early price) | 0.9511 | −0.0018 (−0.0047 to +0.0012) | 0.5624 | – | 26 | −1.3% (−5.5 to +2.6) | +0.5% (−81 to +93) | no |

Log loss by season (lower is better; Pinnacle early for scale is 0.949 overall):

| Season | a | b | c | d | e |
| --- | --- | --- | --- | --- | --- |
| 2017/18 | 0.9537 | 0.9569 | 0.9819 | 0.9654 | 0.9485 |
| 2018/19 | 0.8942 | 0.8991 | 0.9157 | 0.9038 | 0.8930 |
| 2019/20 | 0.9651 | 0.9685 | 0.9964 | 0.9869 | 0.9699 |
| 2020/21 | 1.0096 | 1.0093 | 1.0322 | 1.0313 | 1.0075 |
| 2021/22 | 0.9498 | 0.9579 | 0.9690 | 0.9527 | 0.9310 |
| 2022/23 | 0.9914 | 0.9890 | 0.9761 | 0.9679 | 0.9737 |
| 2023/24 | 0.9360 | 0.9514 | 0.9470 | 0.9395 | 0.9150 |
| 2024/25 | 0.9710 | 0.9761 | 0.9877 | 0.9789 | 0.9701 |

What it shows:
- **Nothing passes.** Every model is worse than Pinnacle's early price on its own (every
  log-loss gain range is below 0), and none earns blend weight whose range clears 0. The
  baseline comes closest (c = +0.11, about the 6% weight the live blend found), but the
  range runs from −0.18 to +0.38.
- **CLV is negative for every model, with tight ranges**: −3.6% to −5.0% against a −2.8%
  margin. The live rule's picks are worse than a random side (round 1 found the same).
- **ROI is noise**: the ranges are about ±15 points on 1,500–1,900 bets. The baseline's
  +2.3% is not evidence of anything; CLV says the picks lose to the close.
- **More flexible is worse.** LightGBM is the worst on log loss and calibration (its
  0.1–0.2 bin predicts 15.5% for 17.9% observed). The hierarchical Poisson's empirical-
  Bayes priors chose the widest prior (0.4) in 8 of 9 seasons: shrinkage didn't help.
  The logit has the best CLV of a–d (−3.6%), still well below 0.
- **The stack** (the live idea: baseline blended with the price) almost matches the
  early price (0.9511 vs 0.9493) and makes only 26 bets at a 12% edge. That is the
  honest state of the live blend: it mostly defers to the market.

**Holdout finalists** (by the rule above): no candidate passed, so the finalists are
a (reference), b (best of b–d by development log loss) and e. One wording slip in the
pre-registration: the candidates table defines e on the best of a–d (that is a, and e
was scored that way above), while the finalist list says "e built on" the best of b–d.
I keep e as it was defined and scored in development (base a), so the holdout tests the
same model. Holdout ranges: 99.17% (Bonferroni for 3 finalists × 2).

### Holdout result (run 37543374450, opened 2026-10-06T22:53:30Z)

The run printed: `HOLDOUT OPENED at 2026-10-06T22:53:30Z (rows from 2025-07-01):
Pre-registered final scoring (docs/lab.md): no candidate passed development; finalists
a (reference), b (best of b-d), e (stack on a).` This was the only time it was opened.

Only **198** of 2025/26's 380 matches have both candidates' predictions and a Pinnacle
early price in football-data's file, so the holdout is small. Ranges are 99.17%
(Bonferroni for 3 finalists × 2).

| Finalist | Log loss (Pinnacle early 0.9831) | Gain vs early (range) | Blend weight c (range) | Bets | CLV (range) | ROI (range) | Pass |
| --- | --- | --- | --- | --- | --- | --- | --- |
| a Dixon-Coles + xG | 0.9844 | −0.0013 (−0.031 to +0.029) | +0.76 (−0.40 to +1.90) | 99 | −3.6% (−5.9 to −1.4) | −11.5% (−50 to +31) | no |
| b Hierarchical Poisson | 0.9892 | −0.0061 (−0.040 to +0.029) | +0.62 (−0.51 to +1.76) | 119 | −5.1% (−7.3 to −2.9) | −9.7% (−48 to +30) | no |
| e Stack (a + early price) | 0.9839 | −0.0008 (−0.007 to +0.005) | – | 0 | – | – | no |

On all 368 matches a predicted (with or without a price), its 2025/26 log loss is
1.0155 and b's is 1.0221: a hard season for both.

## Verdict (bake-off 1)

**Nothing passes, on development or on the holdout.** No model tried adds information
to Pinnacle's early 1X2 price:
- not a better-tuned version of the live model (b);
- not machine learning on Elo, xG form, goal form and rest (c and d);
- not the live model blended with the price (e).

Every model's picks have clearly negative CLV, −3.6% to −5.1% across about 1,500–1,900
development bets and 100–120 holdout bets. The live model stays as it is. Its blend
correctly defers to the market, and there is nothing to hand to the moneyline agent.

The harness is the lasting output. Any future idea, match or player, can be put
through `nested` and `metrics.evaluate` with the same locked-holdout discipline.

## Next ideas, ranked by expected value and cost

| # | Idea | Why it might work | Cost | Owner |
| --- | --- | --- | --- | --- |
| 1 | **The same bake-off on softer leagues**: Championship, League One and Two (E1–E3) | football-data carries Pinnacle early and close for these leagues; Understat doesn't. Lower leagues get less betting volume, so early prices may be less efficient. Uses goals-only features and the harness as is. | 0 credits; a few hours | research-lab |
| 2 | **Late team news vs DraftKings price moves**, from `odds_log/` and `fpl_news/` on data-log | It's the one information source the market may price late. It needs a few weeks of 2026/27 logs (about mid-November). | 0 credits | research-lab |
| 3 | **DraftKings lagging the sharp price**: bet DraftKings when it hasn't followed Pinnacle's move | A known soft-book inefficiency. It needs a live Pinnacle reference (Odds API `eu` region, about 1 credit per refresh on top of the DraftKings call). | About 1 credit per refresh; owner and lead decision | moneyline, after a research-lab feasibility check |
| 4 | **Player lines through the harness** (two outcomes; NaN odds for the missing under) | It gives the player model the same locked-holdout test. Expected value is low because of FanDuel's 10-point margin. | 0 credits | player-props |
| 5 | **O/U 2.5 and Asian handicap through the harness** | Round 2 already found little model weight there (0.11–0.12). | 0 credits | research-lab |

Recommendation: run 1 next (free, and the best chance of a market the models can beat),
then 2 once the logs hold enough matches.
