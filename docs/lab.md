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

## Bake-off 2: 1X2 on the English lower leagues (pre-registered 2026-10-07, before any run)

**Question:** in the Championship (E1), League One (E2) and League Two (E3), where
betting volume is lower and prices may be less sharp, does any model, or a stack of a
model with the opening price, add information to Pinnacle's early 1X2 price out of
sample?

**What is the same as bake-off 1:** the harness, seasons, candidates a–e and their
tuning grids, the stack rule, the market, the bet rule, the metrics and the pass rule.
- Data: football-data, 2014/15–2025/26. 2014/15–2015/16 warm up, 2016/17 feeds the
  stack, **development is 2017/18–2024/25**, and the **2025/26 holdout is locked**.
- Market: Pinnacle's early price (Shin). Bets: the live rule (12% edge, one per match)
  at that price. CLV is against Pinnacle's Shin-fair close.
- Pass rule: for a–d, the blend-weight range is above 0 **and** the CLV range is above
  0. For e, the log-loss gain over the early price has a range above 0 **and** the CLV
  range is above 0. Profit alone never passes.

**What is different:**
- **Goals only.** Understat has no xG for these leagues, so:
  - (a) is Dixon-Coles on goals (`xg_weight` 0);
  - (b) fits goals;
  - (c) and (d) use `features.FEATURES_GOALS`: Elo, goals for and against over 6 and
    20 matches, and rest (13 features; home advantage is in the orientation).

  This limits comparability with E0: a lower-league failure could be the lost xG, not
  the market. A pass would be on weaker inputs, which makes it more convincing, not
  less.
- **Each league on its own.** Each is scored separately; no pooling.
- **Pinnacle coverage check.** A season is scored only if at least 90% of its matches
  have all six Pinnacle 1X2 prices (early and close). Seasons below that still train
  the models but are not scored. The run prints the coverage per season, and I report
  any dropped season here.
- **Promotion and relegation.** Each league is loaded on its own, so a promoted or
  relegated team's matches in another division are not seen. Its Elo restarts at
  1,420, or carries over from its last spell in that league. The same applies to E0.

**Multiple testing.** 3 leagues × 5 candidates × 2 pass metrics = **30 tests**, so every
development range is **99.83%** (Bonferroni). Tuning variants are chosen inside the
training years, as before (8 + 8 + 3 configs per league, 57 in all, not scored
separately). On the holdout, each league's ranges use 2 × (its number of finalists) ×
3 leagues.

**Holdout:** opened once per league, after that league's development results are
recorded here, for the same finalists as bake-off 1:
1. every candidate that passes;
2. a (the reference);
3. the best of b–d by development log loss;
4. e, as defined: the stack on the best of a–d.

**If something passes on development and on the holdout:**
- a handover proposal for the moneyline agent;
- a check (from The Odds API's documentation, no calls) of whether DraftKings or
  another available book prices that league live.

**Run:** `odds-check.yml` with `task=lab` and `league=E1|E2|E3`, then `task=lab-holdout`
with the same `league` and the finalists. Print-only, no credits.

### Development results (2026-10-07 04:43–04:46 UTC, holdouts locked)

Runs 37572801050 (E1), 37572803258 (E2) and 37572805017 (E3). Ranges are 99.83%
(Bonferroni for 30 tests) and resample whole matches. Bets: the live rule (12% edge,
one per match) at Pinnacle's early price, 1 unit each.

**Pinnacle coverage.** Every scored season (2017/18–2024/25) has all six Pinnacle
prices for at least 90% of its matches in all three leagues: E1 99–100%, E3 97–100%,
E2 98–100%. No season was dropped. 2014/15–2015/16 show 0%
only because prices are loaded from 2016/17; those seasons are warm-up, never scored.

| League | Candidate | Matches | Log loss (Pinnacle early) | Gain vs early (range) | Blend weight c (range) | Bets | CLV (range) | ROI (range) | Pass |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| E1 | a Dixon-Coles (goals) | 4,232 | 1.0554 (1.0355) | −0.020 (−0.030 to −0.012) | −0.11 (−0.30 to +0.08) | 2,213 | −4.3% (−4.9 to −3.7) | −7.3% (−17.8 to +3.4) | no |
| E1 | b Hierarchical Poisson | 4,232 | 1.0542 | −0.019 (−0.027 to −0.011) | −0.21 (−0.44 to +0.04) | 2,400 | −4.7% (−5.3 to −4.2) | −7.2% (−18.4 to +5.1) | no |
| E1 | c LightGBM | 4,232 | 1.0539 | −0.018 (−0.028 to −0.008) | −0.01 (−0.24 to +0.23) | 2,435 | −4.4% (−4.9 to −3.8) | −3.7% (−13.2 to +6.7) | no |
| E1 | d Multinomial logit | 4,232 | 1.0520 | −0.017 (−0.025 to −0.009) | −0.14 (−0.46 to +0.09) | 2,142 | −4.8% (−5.4 to −4.2) | −5.3% (−17.6 to +6.5) | no |
| E1 | e Stack (d + early price) | 4,232 | 1.0363 | −0.001 (−0.003 to +0.002) | – | 20 | −3.0% (−7.2 to +1.2) | −12.6% (−90 to +77) | no |
| E2 | a Dixon-Coles (goals) | 4,011 | 1.0429 (1.0248) | −0.018 (−0.028 to −0.009) | −0.01 (−0.27 to +0.24) | 1,936 | −3.8% (−4.4 to −3.2) | −5.6% (−14.8 to +5.6) | no |
| E2 | b Hierarchical Poisson | 4,011 | 1.0446 | −0.020 (−0.028 to −0.011) | −0.07 (−0.32 to +0.16) | 2,029 | −4.4% (−5.1 to −3.7) | −6.5% (−16.4 to +3.9) | no |
| E2 | c LightGBM | 4,011 | 1.0456 | −0.021 (−0.032 to −0.010) | +0.11 (−0.08 to +0.34) | 2,432 | −4.6% (−5.1 to −4.1) | −1.1% (−11.0 to +8.9) | no |
| E2 | d Multinomial logit | 4,011 | 1.0412 | −0.016 (−0.025 to −0.008) | +0.12 (−0.10 to +0.41) | 2,207 | −4.6% (−5.2 to −4.1) | −4.1% (−15.8 to +8.2) | no |
| E2 | e Stack (d + early price) | 4,011 | 1.0255 | −0.001 (−0.003 to +0.002) | – | 15 | −2.1% (−7.0 to +2.0) | −12.5% (−100 to +92) | no |
| E3 | a Dixon-Coles (goals) | 4,039 | 1.0731 (1.0526) | −0.020 (−0.029 to −0.011) | −0.10 (−0.31 to +0.16) | 1,899 | −4.1% (−4.7 to −3.5) | −3.8% (−15.3 to +6.4) | no |
| E3 | b Hierarchical Poisson | 4,039 | 1.0698 | −0.017 (−0.025 to −0.010) | −0.25 (−0.49 to +0.05) | 1,956 | −4.9% (−5.5 to −4.3) | −6.2% (−17.8 to +4.4) | no |
| E3 | c LightGBM | 4,039 | 1.0722 | −0.020 (−0.029 to −0.009) | −0.06 (−0.28 to +0.23) | 2,310 | −4.5% (−5.1 to −4.0) | −5.6% (−14.8 to +5.2) | no |
| E3 | d Multinomial logit | 4,039 | 1.0687 | −0.016 (−0.024 to −0.008) | −0.16 (−0.37 to +0.16) | 1,961 | −4.8% (−5.4 to −4.1) | −7.6% (−19.9 to +4.3) | no |
| E3 | e Stack (d + early price) | 4,039 | 1.0533 | −0.001 (−0.002 to +0.001) | – | 8 | −4.6% (−11.9 to +2.8) | +76% (−100 to +277) | no |

What it shows:
- **Nothing passes in any league.** Every model is worse than Pinnacle's early price by
  0.016–0.021 in log loss, and every gain range is below 0. No blend-weight range
  clears 0; most point estimates are below 0, so the price already holds what the
  goals models know.
- **CLV is −3.8% to −4.9% for every model in every league**, with tight ranges. That is
  the same picture as the Premier League (−3.6% to −5.0%). The lower leagues' early
  prices are not measurably softer against these models.
- The multinomial logit had the best development log loss of b–d in all three leagues,
  so the stack is built on it (the pre-registered rule). The stack nearly matches the
  early price and makes 8–20 bets in eight seasons.
- Caveat: these runs are goals-only, so part of the gap to the market may be the
  missing xG. But E0 with xG was no better against Pinnacle, so xG alone would not
  close the gap.

**Holdout finalists** (by the rule above), in every league: a (reference), d (best of
b–d), and e on d. Holdout ranges: 99.72% (Bonferroni for 3 finalists × 2 × 3
leagues).

### Holdout (opened 2026-10-07, 04:50–04:51 UTC): not scorable

Each league's holdout was opened once, as pre-registered, for a, d and e:
- E1, run 37573365902, at 04:50:29Z;
- E2, run 37573368859, at 04:50:31Z;
- E3, run 37573371184, at 04:51:09Z.

Each run printed its banner and reason. **None could be scored.** Pinnacle's early and
closing 1X2 prices cover only **47% (E1), 30% (E2) and 30% (E3)** of 2025/26's matches
in football-data's files. The pre-registered 90% coverage rule excludes the season, so
the runs printed "Not scored" and no table. I did not re-open them or score the
priced subset: that would break the open-once rule after the fact.

What this means:
- Nothing passed on development, so no holdout result could have changed the verdict.
- The 2025/26 holdout is now spent for these leagues.
- **Data finding for the whole project:** football-data's 2025/26 files have Pinnacle
  prices for only part of the season. That is 30–47% here, and the E0 holdout found
  198 priced matches of 380 (about 52%). Pinnacle closed its public odds API in 2025,
  which would explain it, but I haven't checked. Anything that fits or scores against
  football-data's Pinnacle prices for 2025/26 has a thinner sample than it looks: the
  live match blend (`match_calibration`, fitted on Pinnacle closes) and the Record
  replay. That is for the moneyline agent and the lead to check.
- **Process fix:** check holdout-season price coverage before pre-registering. Counting
  which matches have prices doesn't need results or models. I should have done it
  here.

## Verdict (bake-off 2)

**Nothing passes in the Championship, League One or League Two.** Every goals-based
model is clearly worse than Pinnacle's early price:
- log loss is 0.016–0.021 worse, with every range below 0;
- no blend weight is above 0;
- CLV is −3.8% to −4.9% across about 1,900–2,400 bets per model and league.

The lower leagues' early prices are not measurably softer against these models than the
Premier League's. With bake-off 1, that makes four leagues and five model types with
the same result. Building more 1X2 models against Pinnacle's early price is not worth
more effort. There is no handover to the moneyline agent. Nothing passed, so I didn't
look up which books price these leagues live on The Odds API.

## Next ideas after bake-off 2, ranked by expected value and cost

| # | Idea | Why | Cost | Owner |
| --- | --- | --- | --- | --- |
| 1 | **Late team news vs DraftKings price moves**, from `odds_log/` and `fpl_news/` (pre-register first) | The one source of information not tested yet. The market may price late changes slowly at soft books. | 0 credits; needs data to about mid-November | research-lab |
| 2 | **Check the 2025/26 Pinnacle gap in the live blend**: how many 2025/26 training rows `match_calibration` has, and whether it should use Betfair exchange or the market average where Pinnacle is missing | Keeps the live blend honest; a data issue, not an edge | 0 credits, about an hour | moneyline (with research-lab) |
| 3 | **A new holdout for future bake-offs**: 2026/27 against the logged DraftKings closes, or football-data's Betfair exchange prices for 2025/26 | 2025/26 at Pinnacle is spent and thin | 0 credits | research-lab |
| 4 | DraftKings lagging the sharp price (needs a live Pinnacle or exchange reference) | A known soft-book inefficiency | About 1 credit per refresh; owner and lead decision | moneyline |
| 5 | Player lines through the harness | Same locked-holdout test for player models; expected value is low because of FanDuel's margin | 0 credits | player-props |

Recommendation: stop 1X2 model work. Do 2 now (cheap, protects the live code), and do
1 when the logs are long enough.

## A minimum edge learned from history (pre-registered 2026-10-07, before any run)

**Owner's request:** replace the fixed edge buttons (1/2/5/8/12%…) with a minimum edge
that comes from history. Only flag a bet when its claimed edge is big enough to stand
out from how far realized results normally land from the claims.

**Code:** `lab/thresholds.py`, `edge_threshold(bets)`. It is generic: one row per
settled bet with the claimed edge, the chance it was measured with, the odds, won or
lost (voids left out), a match id, the season and the time.

**Rule (fixed now):**
- *Return per bet:* won × odds − 1 per unit staked (the realized edge).
- *Levels:* claimed edge t = 0%, 1%, …, 30%. Each level is judged on the bets claiming t
  to t + 5 points. This rolling band is the only smoothing. I chose a band over "every
  bet at t and up" on synthetic data, before any real run: the cumulative version
  returns 0% whenever the large edges pay, even when small claimed edges are pure noise.
  That answers a different question from "which bets stand out".
- *Range:* 95% two-sided, by bootstrap over whole matches (2,000 draws).
- *Minimum edge:* the smallest t whose band has a lower bound above 0, where every
  higher band with at least 30 bets also does. Bands with fewer than 30 development
  bets are not judged.
- *Out of sample:* the level is found on the development part and checked on the
  latest season. With a single season, the development part is its first half by
  kickoff and the check is the second half. It is published only if the check's bets at
  t and up returned more than 0 (point estimate; the check sample is small). Otherwise
  `min_edge` is null.
- *No qualifying level:* `min_edge` is null, with a plain-English `note` the app shows
  as it is.
- *Multiple testing:* 31 levels are scanned. The every-higher-band condition and the
  out-of-sample check guard against picking a lucky band. There is no further
  correction.

**Bet pools (the trade rules are unchanged):**
- **Moneyline, DraftKings** (`backtest-dk`): every match's bet under the live rule at a
  threshold of 0, so the first look (48 h, then 3 h) with any positive edge. Computed
  for each strategy (`strategies.raw` and `strategies.blend`, each with
  `.edge_threshold`).
  - The top-level `edge_threshold` is for the chance the app trades on: the blend when
    fitted, else the model (`edge_threshold_strategy` says which).
  - Caveat: at a higher threshold the rule can bet at the later look instead. The pool
    keeps the first qualifying look, so it approximates the rule at t rather than
    replaying it.
- **Moneyline, Pinnacle replay** (`edge_threshold_pinnacle` in `E0_dk.json`): the model's
  bets at Pinnacle's early price (football-data), over the seasons `backtest-dk`
  already predicts for the blend's training. This is the larger sample.
- **Player shots** (`backtest-players`): the main strategy's picks (confirmed starters,
  FanDuel's last price, blended chance) at a threshold of 0. Filtering them by edge
  gives exactly the rule's picks at any higher threshold. Each strategy also gets
  `priced.strategies.<name>.edge_threshold`; the top-level `edge_threshold` in
  `E0_players.json` is the main strategy's.

**Output contract** (for the app):

```
edge_threshold: {min_edge, confidence, method, n_bets, seasons, note,
  by_bucket: [{edge_lo, edge_hi, n, implied, model, realized, realized_lo, realized_hi,
               roi, roi_lo, roi_hi}],
  development_scan: [...], check: {...}}
```

- `implied`, `model` and `realized` are win rates. `realized_lo` and `realized_hi`
  bound the realized win rate. `roi*` is the realized return per unit.
- `by_bucket` uses every settled bet, for display only. The buckets are claimed edge
  0–2, 2–5, 5–8, 8–12, 12–20, 20–30 and 30%+.

**Expectation (stated before running):** every backtest so far loses at every
threshold, so I expect `min_edge: null` for all three pools.

### Results (2026-10-07; print-only `backfill.yml` run 37645372430, and the player lines on data-log)

| Pool | Settled bets with a positive edge | Development / check | `min_edge` | Why |
| --- | --- | --- | --- | --- |
| Moneyline, DraftKings, blend (the live chance; top-level in `E0_dk.json`) | 20 | – | **null** | Too few: the blend almost never claims an edge |
| Moneyline, DraftKings, model alone | 338 | 2025/26 first half (169) / second half | **null** | No band's lower bound clears 0. Best was 2–7%: 41 bets, +34.5% a bet (range −12.7% to +84.2%) |
| Moneyline, model at Pinnacle early (football-data) | 1,280 | 2022/23–2024/25 (1,084) / 2025/26 (196) | **null** | No band's lower bound clears 0. Best was 25–30%: 69 bets, +19.8% (range −33.8% to +95.7%) |
| Player shots, starters after lineups (blend; top-level in `E0_players.json`) | 333 | 2024/25 (262) / 2025/26 | **null** | No band's lower bound clears 0. Best was 13–18%: 33 bets, +25.8% (range −100% to +207%) |

The player-shots row was computed from `backtest/E0_player_lines.csv.gz` on data-log
with the same rule as `backtest-players`: the main strategy's picks at a threshold of 0.

Claimed edge vs result, all settled bets (win rates; return per unit with its range):

| Pool | Claimed edge | Bets | Implied | Model | Won | Return (range) |
| --- | --- | --- | --- | --- | --- | --- |
| Pinnacle early | 0–2% | 71 | 0.476 | 0.482 | 0.479 | +2.4% (−26% to +32%) |
| Pinnacle early | 2–5% | 137 | 0.437 | 0.452 | 0.380 | −15.0% (−35% to +7%) |
| Pinnacle early | 5–8% | 158 | 0.414 | 0.441 | 0.405 | −4.9% (−26% to +18%) |
| Pinnacle early | 8–12% | 186 | 0.420 | 0.460 | 0.398 | −4.4% (−24% to +16%) |
| Pinnacle early | 12–20% | 302 | 0.385 | 0.444 | 0.371 | −2.9% (−19% to +14%) |
| Pinnacle early | 20–30% | 183 | 0.306 | 0.380 | 0.257 | −14.1% (−40% to +18%) |
| Pinnacle early | 30%+ | 243 | 0.203 | 0.295 | 0.198 | −10.4% (−36% to +18%) |
| DraftKings, model | 0–2% | 49 | 0.418 | 0.422 | 0.388 | −12.9% (−45% to +21%) |
| DraftKings, model | 12–20% | 59 | 0.304 | 0.352 | 0.322 | −2.4% (−39% to +41%) |
| DraftKings, model | 20–30% | 46 | 0.241 | 0.301 | 0.174 | −37.2% (−78% to +10%) |
| Player shots | 12–20% | 58 | 0.066 | 0.077 | 0.069 | −14.7% (−86% to +91%) |
| Player shots | 20–30% | 48 | 0.070 | 0.087 | 0.021 | −56.2% (−100% to +43%) |

What it shows:
- In every pool, the realized win rate follows the bookmaker's implied rate, within
  noise (a few buckets land slightly above it), not the model's. The bigger the claimed
  edge, the further the model's chance sits above what happened.
- This is the same finding as the bake-offs, from another angle: the claimed edge is
  the model's error, not information.
- The expected `min_edge: null` holds everywhere. The app should say so in plain words
  instead of offering edge buttons that imply a bet is worth taking.

## Bake-off 3: 1X2 on La Liga, Bundesliga, Serie A and Ligue 1 (pre-registered 2026-10-08, before any model run)

**Question:** does any model, or a stack of a model with the opening price, add
information to Pinnacle's early 1X2 price out of sample in Spain (SP1), Germany (D1),
Italy (I1) or France (F1)?

**Coverage checked first** (`odds-check.yml` `task=lab-coverage`, runs 37840790870,
37840794329, 37840798213, 37840802259 and 37841597798). It counts matches with all six
Pinnacle 1X2 prices (early and close) and matches with Understat xG; it scores no
results and fits no models.

| League | Pinnacle, 2016/17–2024/25 | Pinnacle, 2025/26 | xG, 2016/17–2024/25 |
| --- | --- | --- | --- |
| SP1 | 99–100% | 49% | 100% |
| D1 | 100% | 49% | 100% |
| I1 | 99–100% | 52% | 100% |
| F1 | 99–100% | 50% | 90% (2016/17), else 100% |

- The first D1 check found xG on only 78–89% of 2016/17–2018/19. Three Understat names
  had no football-data spelling: Hamburger SV, Hannover 96 and Nuernberg. I added them
  to `xg.TEAM_NAMES` (c30fed5, additive; the moneyline session was told). The re-check
  shows 100%.
- F1's 2016/17 gap is in a warm-up season only.
- Ligue 1 had 279 matches in 2019/20 (stopped early) and 306 a season from 2023/24.

**Seasons (chosen from the coverage above):**
- 2014/15–2015/16 warm up features and training.
- 2016/17 is predicted, not scored; it feeds the stack.
- **Development: 2017/18–2023/24** (7 seasons, about 2,300–2,650 matches per league).
- **Holdout: 2024/25, locked**, the latest season with at least 90% Pinnacle coverage
  in every league (`--holdout-season 2024`).
- 2025/26 is not used at all: under 90% coverage, and after the holdout, so it stays
  locked.

**Same as bake-off 1 (E0):**
- candidates a–e, with the same tuning grids and the same nested tuning;
- features with xG (`features.FEATURES`: Elo, 6/20-match xG and goals for and against,
  rest); Dixon-Coles and the hierarchical Poisson fit 0.7·xG + 0.3·goals;
- the stack rule (e = the best of a–d by development log loss, blended with Pinnacle
  early);
- the market (Pinnacle early, Shin) and the bet rule (12% edge, one per match, at
  Pinnacle early);
- the metrics and the pass rule: for a–d, blend-weight range above 0 **and** CLV range
  above 0; for e, log-loss gain range above 0 **and** CLV range above 0. Profit alone
  never passes.
- The 90% coverage rule applies per season. All development seasons pass it, per the
  table above.

**Multiple testing:** the four leagues are one family. 4 leagues × 5 candidates × 2 pass
metrics = **40 tests**, so every development range is **99.875%** (Bonferroni). Tuning
variants are chosen inside the training years: 19 configs per league, 76 in all, not
scored separately.

**Holdout:** opened once per league, after that league's development results are
recorded here, for:
1. every candidate that passes;
2. a (the reference);
3. the best of b–d by development log loss;
4. e, as defined (the stack on the best of a–d).

Each league's holdout ranges are Bonferroni for 2 × its finalists × 4 leagues.

**Learned minimum edge, per league:** `lab.thresholds.edge_threshold` on a's (the live
model's) bets at Pinnacle early. The bets are the live rule at a threshold of 0 over the
development seasons; the check is 2023/24, the latest development season. The rules are
as pre-registered for the learned minimum edge above (5-point bands, 95%, at least 30
bets, every higher band, the check must return more than 0). It is reported per league,
and I expect null.

**If something passes on development and on its holdout:** a handover proposal for the
moneyline agent. Otherwise, no change and a plain verdict.

**Run:** `odds-check.yml` with `task=lab`, `league=SP1|D1|I1|F1` and `holdout_season=2024`;
then `task=lab-holdout` with the same inputs and the finalists. Print-only, no credits.

### Development results (2026-10-08 20:45–20:48 UTC, holdouts locked)

Runs 37841844538 (SP1), 37841848290 (D1), 37841852120 (I1) and 37841856079 (F1).
Seasons 2017/18–2023/24. Ranges are 99.875% (Bonferroni for 40 tests) and resample
whole matches. Bets: the live rule (12% edge, one per match) at Pinnacle's early price,
1 unit each.

| League | Candidate | Matches | Log loss (Pinnacle early) | Gain vs early (range) | Blend weight c (range) | Bets | CLV (range) | ROI (range) | Pass |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| SP1 | a Dixon-Coles + xG | 2,569 | 0.9868 (0.9772) | −0.010 (−0.019 to −0.000) | +0.20 (−0.09 to +0.55) | 1,205 | −5.0% (−6.1 to −3.9) | +0.8% (−19 to +23) | no |
| SP1 | b Hierarchical Poisson | 2,569 | 0.9915 | −0.014 (−0.023 to −0.004) | +0.10 (−0.26 to +0.47) | 1,402 | −5.4% (−6.4 to −4.4) | −5.8% (−24 to +13) | no |
| SP1 | c LightGBM | 2,569 | 1.0029 | −0.026 (−0.040 to −0.011) | +0.06 (−0.12 to +0.26) | 1,712 | −3.5% (−4.2 to −2.7) | +0.5% (−12 to +16) | no |
| SP1 | d Multinomial logit | 2,569 | 0.9948 | −0.018 (−0.029 to −0.006) | +0.08 (−0.18 to +0.31) | 1,418 | −4.0% (−4.9 to −3.1) | −2.3% (−17 to +13) | no |
| SP1 | e Stack (a + early) | 2,569 | 0.9777 | −0.001 (−0.004 to +0.003) | – | 14 | −8.1% (−19 to +3) | −100% | no |
| D1 | a Dixon-Coles + xG | 2,079 | 0.9972 (0.9833) | −0.014 (−0.024 to −0.004) | −0.30 (−0.67 to +0.12) | 822 | −4.1% (−5.2 to −2.9) | −0.6% (−27 to +24) | no |
| D1 | b Hierarchical Poisson | 2,079 | 1.0020 | −0.019 (−0.032 to −0.008) | −0.28 (−0.66 to +0.14) | 1,227 | −4.2% (−5.5 to −3.2) | −8.6% (−28 to +15) | no |
| D1 | c LightGBM | 2,079 | 1.0139 | −0.031 (−0.048 to −0.013) | −0.06 (−0.29 to +0.20) | 1,456 | −4.1% (−4.9 to −3.2) | −1.2% (−16 to +17) | no |
| D1 | d Multinomial logit | 2,079 | 1.0010 | −0.018 (−0.033 to −0.004) | −0.03 (−0.42 to +0.34) | 1,272 | −4.2% (−5.1 to −3.3) | −0.2% (−18 to +17) | no |
| D1 | e Stack (d + early) | 2,079 | 0.9853 | −0.002 (−0.007 to +0.003) | – | 204 | −4.9% (−8.2 to −1.6) | −1.5% (−49 to +49) | no |
| I1 | a Dixon-Coles + xG | 2,561 | 0.9733 (0.9545) | −0.019 (−0.028 to −0.010) | −0.30 (−0.63 to +0.08) | 1,081 | −4.4% (−5.5 to −3.2) | −15.5% (−34 to +6) | no |
| I1 | b Hierarchical Poisson | 2,561 | 0.9769 | −0.022 (−0.032 to −0.013) | −0.26 (−0.60 to +0.13) | 1,384 | −5.2% (−6.3 to −4.1) | −14.9% (−32 to +4) | no |
| I1 | c LightGBM | 2,561 | 0.9824 | −0.028 (−0.044 to −0.015) | −0.05 (−0.24 to +0.20) | 1,583 | −3.5% (−4.3 to −2.6) | −5.4% (−19 to +12) | no |
| I1 | d Multinomial logit | 2,561 | 0.9746 | −0.020 (−0.031 to −0.010) | −0.09 (−0.31 to +0.19) | 1,240 | −3.2% (−4.0 to −2.3) | −14.8% (−29 to +0) | no |
| I1 | e Stack (d + early) | 2,561 | 0.9543 | +0.000 (−0.005 to +0.006) | – | 147 | −3.9% (−6.4 to −1.6) | +0.9% (−28 to +34) | no |
| F1 | a Dixon-Coles + xG | 2,390 | 1.0051 (0.9890) | −0.016 (−0.026 to −0.007) | −0.22 (−0.58 to +0.19) | 1,068 | −3.8% (−4.9 to −2.6) | −8.4% (−28 to +14) | no |
| F1 | b Hierarchical Poisson | 2,390 | 1.0067 | −0.018 (−0.026 to −0.007) | −0.22 (−0.59 to +0.22) | 1,181 | −4.7% (−5.8 to −3.7) | −6.9% (−26 to +13) | no |
| F1 | c LightGBM | 2,390 | 1.0227 | −0.034 (−0.047 to −0.019) | −0.17 (−0.38 to +0.09) | 1,498 | −3.4% (−4.3 to −2.6) | −6.4% (−22 to +8) | no |
| F1 | d Multinomial logit | 2,390 | 1.0126 | −0.024 (−0.035 to −0.012) | −0.19 (−0.45 to +0.09) | 1,252 | −4.1% (−5.1 to −3.0) | −7.3% (−25 to +12) | no |
| F1 | e Stack (a + early) | 2,390 | 0.9904 | −0.001 (−0.006 to +0.002) | – | 54 | −4.2% (−9.1 to +0.0) | −0.7% (−50 to +48) | no |

**Learned minimum edge (a at Pinnacle early; development 2017/18–2022/23, check 2023/24):**

| League | Bets | `min_edge` | Best band (development) |
| --- | --- | --- | --- |
| SP1 | 2,432 | null | 24–29%: 115 bets, +30.7% (range −16.7% to +84.6%) |
| D1 | 1,954 | null | 18–23%: 128 bets, +11.6% (range −24.0% to +49.8%) |
| I1 | 2,395 | null | 12–17%: 259 bets, +19.7% (range −5.5% to +47.9%) |
| F1 | 2,236 | null | 13–18%: 267 bets, +8.9% (range −13.4% to +34.1%) |

What it shows:
- **Nothing passes in any of the four leagues.** Every model is worse than Pinnacle's
  early price on log loss: −0.010 to −0.034, with every range at or below 0. No
  blend-weight range clears 0; in D1, I1 and F1 every point estimate is negative.
- **CLV is −3.2% to −5.4%** for every model in every league, with tight ranges.
- **The stack only matches the price** (gains −0.002 to +0.000). Its few bets also have
  negative CLV, so the stack adds nothing.
- This is the same as the Premier League (bake-off 1, with xG) and the English lower
  leagues (bake-off 2, goals only). With xG and a fully priced holdout season, the
  big-four leagues behave like E0.
- **No league has a minimum edge**, so `min_edge` is null for each. Serie A's best band
  comes closest (lower bound −5.5%), but it is one band out of 31 and fails the
  every-higher-band rule.

**Holdout finalists** (by the pre-registered rule):

| League | Finalists |
| --- | --- |
| SP1 | a, b (best of b–d), e (on a) |
| D1 | a, d (best of b–d), e (on d) |
| I1 | a, d (best of b–d), e (on d) |
| F1 | a, b (best of b–d), e (on a) |

- **Stack base, a subtlety.** The stack's base is chosen by each candidate's development
  log loss over the rows *it* predicted (`run.py`, as in bake-offs 1 and 2), not the
  common rows in the table. a skips a team's first matches after promotion, so in D1
  and I1 d won on its own rows even though a is lower on the common rows. I keep e as
  it was defined and scored in development, as in bake-off 1.
- Holdout ranges: 99.79% (Bonferroni for 3 finalists × 2 × 4 leagues = 24).

### Holdout results (2024/25, each opened once, 2026-10-08)

| League | Run | Opened (UTC) | Matches |
| --- | --- | --- | --- |
| SP1 | 37843378719 | 20:58:23 | 364 |
| D1 | 37843382576 | 20:58:26 | 285 |
| I1 | 37843387711 | 20:58:30 | 352 |
| F1 | 37843391250 | 20:58:30 | 291 |

Each run printed its banner and reason, and 2024/25 had 100% Pinnacle coverage in every
league. Ranges are 99.79% (Bonferroni for 24 tests).

| League | Finalist | Log loss (Pinnacle early) | Gain vs early (range) | Blend weight c (range) | Bets | CLV (range) | ROI (range) | Pass |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| SP1 | a | 0.9700 (0.9563) | −0.014 (−0.037 to +0.008) | −0.16 (−1.17 to +0.79) | 141 | −3.9% (−7.0 to −0.5) | −9.2% (−53 to +40) | no |
| SP1 | b | 0.9720 | −0.016 (−0.039 to +0.008) | −0.15 (−1.24 to +0.94) | 166 | −5.9% (−8.9 to −2.9) | −15.5% (−57 to +34) | no |
| SP1 | e (on a) | 0.9575 | −0.001 (−0.010 to +0.007) | – | 0 | – | – | no |
| D1 | a | 1.0034 (0.9834) | −0.020 (−0.044 to +0.002) | −0.86 (−1.86 to +0.39) | 78 | −4.6% (−9.1 to −0.8) | −21.5% (−74 to +43) | no |
| D1 | d | 1.0118 | −0.028 (−0.060 to +0.003) | −0.34 (−1.23 to +0.55) | 165 | −5.7% (−8.4 to −3.6) | −9.8% (−52 to +30) | no |
| D1 | e (on d) | 0.9854 | −0.002 (−0.010 to +0.005) | – | 0 | – | – | no |
| I1 | a | 0.9643 (0.9450) | −0.019 (−0.040 to −0.001) | −0.21 (−1.28 to +0.93) | 118 | −6.0% (−9.3 to −2.5) | −35.8% (−75 to +5) | no |
| I1 | d | 0.9508 | −0.006 (−0.036 to +0.026) | +0.28 (−0.52 to +1.10) | 209 | −3.2% (−4.8 to −1.5) | −13.5% (−43 to +20) | no |
| I1 | e (on d) | 0.9377 | +0.007 (−0.002 to +0.017) | – | 0 | – | – | no |
| F1 | a | 0.9839 (0.9638) | −0.020 (−0.048 to +0.007) | −0.26 (−1.13 to +0.69) | 128 | −6.6% (−9.8 to −3.2) | −3.5% (−50 to +51) | no |
| F1 | b | 0.9775 | −0.014 (−0.040 to +0.014) | −0.12 (−1.06 to +0.89) | 172 | −7.4% (−10.3 to −4.7) | −2.6% (−43 to +51) | no |
| F1 | e (on a) | 0.9635 | +0.000 (−0.009 to +0.008) | – | 0 | – | – | no |

- The holdout agrees with development: no finalist passes in any league.
- Every model's CLV range is below 0, at −3.2% to −7.4%.
- The Serie A stack beat the early price on 2024/25 (log loss 0.9377 vs 0.9450). Its
  range includes 0 and it made no bets, so it fails the pre-registered rule. One season
  out of four leagues and three finalists is the kind of result the correction exists
  for.

## Verdict (bake-off 3)

**Nothing passes in La Liga, the Bundesliga, Serie A or Ligue 1, on development or on
the holdout.** With xG and fully priced seasons, the big-four leagues match the Premier
League:
- every model trails Pinnacle's early price;
- no model earns blend weight above 0;
- the live rule's picks give up 3–7% against the close.

Each league's learned minimum edge is null. Across bake-offs 1–3 that is eight leagues,
five model types and one stack, with the same result. **No handover to the moneyline
agent**, and no reason to add these leagues to the live bet rule. Nothing passed, so
the question of which live book prices them didn't arise and wasn't checked.

**Next ideas** (unchanged in rank from bake-off 2, with one addition):
1. Late team news against the DraftKings odds log, once the log is long enough (free).
2. Stop 1X2 model work against Pinnacle's early price in any league. If the owner wants
   a non-English league shown in the app, show it for display only, with the blend
   (which defers to the price).
3. New: if more leagues are ever tested, test the stack's one near-miss (Serie A)
   first, as its own pre-registered experiment on 2025/26 once a sharp closing price
   covers it. Pinnacle in football-data covers only half of that season, so the price
   source has to be decided first.

## The Championship's learned minimum edge (pre-registered 2026-10-08, before any run)

The owner is adding the Championship (E1) to the app beside the big-four leagues, so it
needs the same learned minimum edge. This is not a new model selection: bake-off 2
already ran E1's goals-only bake-off and nothing passed.
- **Bets**: candidate a only (the live Dixon-Coles, goals only, the same spec as
  bake-off 2), at Pinnacle's early 1X2 price, under the live rule at a threshold of 0
  (the outcome with the largest positive edge, one per match). No other candidate, no
  stack and no tuning are run (`lab.run --edge-only`).
- **Seasons**: 2017/18–2024/25, scoring only seasons where Pinnacle's early and closing
  prices cover at least 90% of the matches. 2025/26 is left out (47% covered) and stays
  locked.
- **Rule**: `lab.thresholds.edge_threshold` unchanged (5-point bands, 95% match-resampled
  ranges, at least 30 bets per judged band). Development is every scored season but the
  latest; the latest scored season is the check.
- **Where it goes**: `src/soccer_stats/lab/min_edge.json`, one entry per league in the
  `edge_threshold` contract plus the run it came from, read with
  `lab.thresholds.league_levels()`. The same file records the big-four levels from
  bake-off 3 (all null). E0 keeps its level in `E0_dk.json`.

### Results (2026-10-08 21:15 UTC; `odds-check.yml task=lab-edge`, 0 credits)

| League | Run | Bets | Development / check | min_edge | Best development band |
| --- | --- | --- | --- | --- | --- |
| E1 (goals only) | 37845449673 | 4,052 | 2017/18–2023/24 / 2024/25 | none | 28–33%: 162 bets, +16.4% (−9.6% to +44.6%) |
| SP1 | 37845453133 | 2,432 | 2017/18–2022/23 / 2023/24 | none | 24–29%: 115 bets, +30.7% (−16.7% to +84.6%) |
| D1 | 37845456702 | 1,954 | same | none | 18–23%: 128 bets, +11.6% (−24.0% to +49.8%) |
| I1 | 37845460440 | 2,395 | same | none | 12–17%: 259 bets, +19.7% (−5.5% to +47.9%) |
| F1 | 37845463369 | 2,236 | same | none | 13–18%: 267 bets, +8.9% (−13.4% to +34.1%) |

- **The Championship has no learned minimum edge.** E1 seasons 2016/17–2024/25 are all
  at least 99% priced, so 2017/18–2024/25 are scored and 2025/26 (47%) is left out.
  No 5-point band from 0% to 30% has a lower bound above 0. At the live 12% rule's
  edges (12–20%) the model claimed 42.0% and won 32.8% against 36.3% implied: −10.7% a
  bet (−19.7% to −1.1%). Realized win rates track Pinnacle's implied rate, not the
  model's, as in every other league.
- The big-four reruns reproduce bake-off 3's bet counts exactly (`--edge-only` runs the
  same candidate a on the same seasons), and all four stay null.
- Written to `src/soccer_stats/lab/min_edge.json` (read with
  `lab.thresholds.league_levels()`), one entry per league in the `edge_threshold`
  contract plus `source`. The development scans are in each run's `EDGE_JSON` log line,
  not the file. Nothing in the live rule changes.

## Lineup surprises against Pinnacle's move (round 8; pre-registered 2026-10-08, before any data was loaded)

**Question.** Does the starting XI explain how Pinnacle's price moves from early to
close, and does the close already absorb it? This decides whether a live team-news
check (16 Nov) and live odds logging for new leagues are worth pursuing. Research only:
0 credits, nothing live changes.

**Timing.** football-data's early price is taken one to three days before kickoff. The
close is taken at kickoff. Starting XIs are public about an hour before kickoff. So a
lineup surprise is news that arrives after the early price and before the close.
- Tests 1 and 4 are the real questions.
- Tests 2 and 3 (betting at the early price) are an upper bound: nobody knows the XI
  when the early price is up.

**Data.**
- Understat match rosters: who started (position not "Sub") and each player's xG.
- football-data's Pinnacle early and closing 1X2 prices, made margin-free with Shin's
  method.
- Matches are paired on home, away and a date within 2 days, as in `xg.attach_xg`.
- Leagues: E0, SP1, D1, I1, F1.
- Seasons: 2016/17–2024/25. 2016/17 trains only; 2017/18–2024/25 are scored, keeping
  only seasons where Pinnacle's early and closing prices cover at least 90% of matches.
- 2025/26 (about 50% covered) is dropped before anything is computed.

**The surprise** (fixed; no variants are run):
- For team T in match m, W is T's earlier league matches in the same season, the last
  up to 6. With fewer than 3, the value is missing, so the first three rounds are out.
- Regulars are players who started at least ⌈2/3·|W|⌉ of W.
- Each regular's weight is their share of the team's xG over W: their xG in W divided
  by the xG of all of T's players in W.
- S(T, m) is the summed weight of the regulars who do not start m.
- The signal is x = S(away) − S(home). A positive value means the away side is more
  depleted, which should favour home.
- Only matches before m are used for regulars and weights. The only information from m
  itself is its starting XI, which is the news being tested.

**Tests per league** (4):
1. **Move.** OLS of the move in log(home/away) (close minus early, margin-free) on x.
   - Passes if the slope range excludes 0; a positive slope is expected.
   - Out-of-sample R², season by season on earlier seasons, is reported alongside.
2–3. **Betting at the early price** (an upper bound).
   - The candidate is a walk-forward conditional logit, score_k = a_k + b·log(early_k)
     + d·z_k with z = (x/2, 0, −x/2). It is refitted every 28 days on earlier matches,
     from 300 earlier rows.
   - It goes through `lab.metrics.evaluate`: market = Pinnacle early, odds = Pinnacle
     early, CLV vs Pinnacle's fair close.
   - Pass rule: `lab.metrics.passes`, meaning the blend-weight range and the 12% rule's
     CLV range are both above 0. ROI is reported but never passes on its own.
4. **Does the close absorb it?** The same candidate, built on the fair close instead of
   early and scored beside the close.
   - A blend-weight range above 0 means the close does not fully absorb lineup news.
   - The 12% rule's ROI at Pinnacle's closing price is reported.

**Family.** 5 leagues × 4 tests = 20, so all ranges are Bonferroni 99.75%. Ranges
resample whole matches. With ranges this wide and nothing tuned, there is no separate
holdout: each test is run once.

**Reading the result.**
- The close absorbs lineup news in a league if test 4's range includes 0.
- Lineups move the price there if test 1 passes.
- If the close absorbs it everywhere, a team-news edge can only exist against books
  slower than Pinnacle (DraftKings). The live check on 16 Nov answers that question;
  this test can't.

**Limits.**
- An XI can't tell a known injury (already in the early price) from a late surprise.
  This is a property of the data and isn't fixed here.
- The weights are by xG, as specified, so absent defenders and goalkeepers count as
  zero.

**Code.** `edge/lineups.py`; `odds-check.yml` `task=lineups` with `league` (no key
passed). Tests include no look-ahead.
