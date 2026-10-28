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
- The close absorbs lineup news in a league if test 4's range is not above 0 (it
  includes 0, or lies below it: adding x then only adds noise). Clarified before any
  real run, after the synthetic tests showed a range wholly below 0 for a close that
  already knows the XI.
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

### Results (2026-10-08, `odds-check.yml task=lineups`, 0 credits; ranges 99.75%)

Runs: E0 37853078197, SP1 37857126948, D1 37857129940, I1 37857132611, F1 37857135194.
- Every league's Understat rosters downloaded in full, with 0 missing.
- 90–92% of football-data matches paired with a surprise value; the rest are each
  season's first three rounds.
- Every season from 2016/17 to 2024/25 is at least 99% priced, so all of 2017/18–2024/25
  is scored.

| League | Matches | 1. Move: slope (range) | R² out of sample | 2–3. Early: blend c (range) | 12% bets, CLV | 4. Close: blend c (range) |
| --- | --- | --- | --- | --- | --- | --- |
| E0 | 2,797 | +0.218 (+0.168 to +0.265) **pass** | 6.1% | +0.01 (−0.33 to +0.41) | 50, +1.2% (−2.1 to +5.0) | −0.09 (−0.41 to +0.31) |
| SP1 | 2,794 | +0.203 (+0.154 to +0.256) **pass** | 5.1% | −0.21 (−0.50 to +0.07) | 0 | −0.22 (−0.51 to +0.05) |
| D1 | 2,231 | +0.150 (+0.096 to +0.206) **pass** | 2.7% | −0.30 (−0.69 to +0.13) | 93, −5.8% (−10.2 to −0.2) | −0.25 (−0.67 to +0.19) |
| I1 | 2,794 | +0.172 (+0.122 to +0.215) **pass** | 3.5% | −0.12 (−0.41 to +0.10) | 9, −0.2% (−8.7 to +6.6) | −0.12 (−0.39 to +0.14) |
| F1 | 2,554 | +0.173 (+0.121 to +0.222) **pass** | 3.5% | −0.20 (−0.51 to +0.09) | 2, −3.6% | −0.23 (−0.55 to +0.05) |

- **Lineups move the price, in every league (test 1 passes 5 of 5).**
  - When the away side is missing a large share of its recent xG, Pinnacle's price moves
    toward home between the early price and the close, and the reverse.
  - The surprise explains 2.7–6.1% of the move's variance out of sample (Bundesliga lowest, E0 highest).
  - The move by band is monotone. In E0 it runs from −0.079 (away side much stronger
    than usual) to +0.068 (away side depleted), against a spread of 0.17.
- **The close absorbs it, in every league (test 4 fails 5 of 5).**
  - Beside the fair close the surprise earns no blend weight: every range includes 0 and
    every point estimate is at or below 0.
  - Log loss beside the close is no better (every gain range includes 0).
- **Betting at the early price doesn't pass anywhere (tests 2–3 fail 5 of 5),** even
  though this test knows the XI before it could be known.
  - Beside the early price the surprise earns no blend weight. The move is real, but too
    small next to match noise to sharpen the result's odds.
  - The 12% rule rarely fires (0–93 bets).
  - Germany's 93 bets are long shots: average odds 7.7, CLV −5.8%, and a ROI of +51% whose
    range runs from −39% to +168%. They pass nothing.
- **Descriptive.**
  - 91–94% of team-matches are missing at least one regular; rotation is the norm.
  - The mean surprise is 0.15–0.17 of a team's recent xG, and a third of team-matches
    exceed 0.2.

## Verdict (lineup surprises)

**The close already absorbs lineup news.**
- In all five leagues the starting XI explains part of how Pinnacle's price moves from
  early to close, and by kickoff Pinnacle has priced it.
- The surprise adds nothing beside the close, and nothing measurable to the result
  beside the early price.

What this means for the work plan:
- **The live team-news check (16 Nov)** can only find value where a book is slower than
  Pinnacle: DraftKings near kickoff. It should compare DraftKings' last pre-lineup price
  with its close (or with Pinnacle's close) after a surprise. It shouldn't look for
  information Pinnacle lacks. Worth running, as that cheap check: the odds log already
  holds the data.
- **Live odds logging for new leagues** is worth it only for that same purpose: measuring
  how fast DraftKings reacts. It isn't for any model edge, since nothing beats Pinnacle's
  close here or in bake-offs 1–3.
- No handover and no live change.

## Model 12% filters (owner, 19 Oct; pre-registered 2026-10-19, before any code or run)

**Owner's request.** The live Model 12% test loses because its edges sit on long shots
the model over-rates: on 86 settled live trades the model said 37%, the price implied
31%, and 29% won; at prices of 5.00 and up (15 bets) the model said 19%, the price 15%,
and 7% won, −$85 of the −$109. Test on history which of two filters, if either, is worth
keeping. Those live numbers are why the filters were chosen, so they are not evidence
for them; the test below uses history only.

**The three rules (fixed now).** Each is applied once per match to the model's own raw
chances (p_home, p_draw, p_away) and the quoted decimal prices; edge = p × decimal − 1.
- **Baseline:** the live rule as it is: the side with the highest edge among those with
  edge ≥ 12% (and > 0), one pick per match, `trades.best_pick`'s order and ties (home,
  draw, away; a later side replaces an earlier one only with a strictly higher edge;
  12% minus `EPS` 1e-9), $1 a bet.
- **A, model favourites only:** the same, but only sides whose model chance is above
  50% (strictly) can be picked.
- **B, no long shots:** the same, but only sides whose decimal price is 3.00 or shorter
  (≤ 3.00) can be picked.
- **How a filter applies (the choice, kept):** the filter is applied *before* the pick,
  not to the pick the baseline would make. Each rule chooses the best edge ≥ 12% among
  the sides that pass its filter. So a match whose baseline pick is a long shot can
  still be bet under B on another side with edge ≥ 12% and price ≤ 3.00. Under A at most
  one side can pass (two sides can't both be above 50%).

**Data.**
- Model chances: `backtest.walk_forward` exactly as `lean_backtest` builds them
  (`soccer-stats backtest-lean`): Dixon-Coles refitted every 7 days on the 730 days
  before each window, predictions from 2019-07-01, training data from 2017/18 (burn-in
  2 seasons), xG weight 0.7 in the Understat leagues (E0, SP1, D1, I1, F1) and goals
  only (weight 0) in E1, E2 and E3, as live. Matches with a team that has fewer than 6
  matches in the training window get no chance (the walk-forward's skip, the same 6 as
  the live `low_data` rule), so they are never bet. Every chance comes from matches
  played before its window: no look-ahead.
- Prices: Pinnacle's own early 1X2 from football-data (PSH/PSD/PSA, never the
  average/Bet365 fallback; `data.pinnacle_prices`), bet at the quoted price (margin
  kept). CLV = entry odds × Pinnacle's Shin-fair close (PSCH/PSCD/PSCA) − 1
  (`trades.clv`); a bet without a full close counts for ROI but not CLV.
- Seasons: 2019/20–2024/25 only, and in each league only the seasons where Pinnacle's
  early and close prices both cover at least 90% of played matches (`lean_backtest`'s
  coverage rule). 2025/26 (about half priced) and 2026/27 are dropped before any bet is
  made, whatever their coverage.
- Leagues: E0, SP1, D1, I1, F1, E1, E2, E3.

**Metrics, per rule, pooled across leagues and per league** (per season pooled as a
descriptive extra): bets; win rate beside the model's average chance and the average
implied chance (1/decimal, margin kept); ROI per $1 with a 95% range; CLV with a 95%
range; beat-the-close share (CLV > 0); average odds. Ranges are percentile bootstraps
(2,000 draws, fixed seed) resampling matches; each rule has at most one bet per match,
so resampling bets is resampling matches. Pooled CLV also gets the 97.5% range used by
the decision.

**Decision rule (written before any result).**
- A filter is **worth keeping** if, pooled across the eight leagues, its CLV range at
  97.5% (Bonferroni for two filters: 2 tests at 95% family-wise) lies above 0 **and** it
  has at least 300 bets.
- If both pass, keep the one with the higher pooled CLV. If neither passes, say so
  plainly and recommend no live change.
- ROI is reported, not judged (too noisy). The baseline is the reference and is not
  judged. Per-league and per-season numbers are descriptive. Variants counted: 2 (A and
  B); nothing else will be tried under this registration.

**Secondary, descriptive check (not judged):** the same three rules on E0's DraftKings
2025/26 backtest (`backtest.dk_candidates` on the cached DraftKings snapshots, as
`backtest-dk` builds them for `E0_dk.json`: looks 48 h and 3 h before kickoff, a match
trades at its first qualifying look under each rule, xG weight 0.7, burn-in 2, teams
with fewer than 6 matches in the training window skipped). Home/draw/away only, as the
live DraftKings call asks for h2h only. Reported: bets, win rate vs model vs implied,
ROI with a 95% range, CLV against DraftKings' close and against Pinnacle's close.

**What I already know (disclosed):** the baseline at Pinnacle's early price has been
seen before in E0 (round 2: CLV −4.2% on 1,419 1X2 bets; `edge_threshold_pinnacle`)
and in the bake-offs (CLV −3% to −5% in every league). No filtered result has been
computed. Expectation: the baseline's CLV is below 0 in every league; A and B make
fewer, shorter-priced bets, and I expect their CLV to be closer to 0 but not above it.

**How it runs:** `soccer-stats backtest-edge12-filters` (`edge12_filters.py`),
`backfill.yml` input `edge12_filters` (no key, 0 credits, prints and uploads a 7-day
artifact, never pushes to data-log), dispatched once on the research branch.

### Results (2026-10-19; print-only `backfill.yml` run 39291797948, 0 credits, no key)

Order: pre-registration committed 268baf7 at 16:55:55 UTC; code c15748b at 17:01:57;
first run 39290653007 dispatched 17:02:08. That run lost Ligue 1, the Championship and
League Two to football-data read timeouts (the other five leagues' numbers were printed
and seen). The fix (e9d4f46, 17:09) only adds download retries, and refuses to run an
Understat league goals-only. Nothing in the rules, data or metrics changed. The complete
run 39291797948 (dispatched 17:10:03) is the result. Its five shared leagues match the
first run exactly. Every league scored all six seasons 2019/20–2024/25: Pinnacle's
early and close prices cover at least 90% everywhere. xG was attached to 100% of matches
in the five Understat leagues and 0% in E1–E3, as registered.

Won = win rate; model = the model's average chance on the picked side; implied =
1/decimal (margin kept). ROI per $1 and CLV with 95% ranges (2,000 match resamples);
pooled CLV also at 97.5%.

**Pooled, eight leagues**

| Rule | Bets | Won / model / implied | ROI (95%) | CLV (95%) | CLV 97.5% | Beat close | Avg odds |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Baseline | 8,911 | 26.6% / 35.7% / 28.4% | −5.6% (−9.7% to −1.8%) | −4.6% (−4.8% to −4.4%) | −4.9% to −4.4% | 30% | 4.52 |
| A, model > 50% | 1,560 | 44.8% / 58.7% / 47.2% | −5.7% (−11.1% to −0.5%) | −2.7% (−3.1% to −2.3%) | −3.1% to −2.3% | 32% | 2.17 |
| B, price ≤ 3.00 | 2,925 | 40.8% / 52.1% / 42.7% | −4.8% (−9.1% to −0.6%) | −3.1% (−3.4% to −2.9%) | −3.5% to −2.8% | 31% | 2.40 |

**Per league (descriptive)**: bets; won / model / implied; ROI (95%); CLV (95%)

| League | Baseline | A, model > 50% | B, price ≤ 3.00 |
| --- | --- | --- | --- |
| E0 | 1,140; 24.6 / 31.7 / 24.9%; +4.4% (−8.2 to +18.5); −4.2% (−4.9 to −3.5) | 148; 47.3 / 59.0 / 48.2%; −2.3% (−19.1 to +14.5); −2.5% (−3.7 to −1.3) | 286; 42.3 / 52.1 / 43.1%; −2.0% (−15.3 to +11.7); −3.1% (−4.0 to −2.2) |
| SP1 | 930; 24.2 / 31.4 / 25.2%; −5.7% (−19.0 to +6.9); −4.7% (−5.5 to −3.9) | 107; 48.6 / 57.0 / 46.9%; +2.3% (−17.5 to +22.3); −1.4% (−2.8 to +0.2) | 225; 43.1 / 50.2 / 41.8%; +2.0% (−13.3 to +17.9); −2.2% (−3.2 to −1.2) |
| D1 | 628; 23.7 / 29.1 / 23.7%; +3.7% (−13.9 to +22.6); −4.4% (−5.3 to −3.4) | 66; 45.5 / 58.4 / 48.1%; −6.0% (−30.7 to +19.4); −1.7% (−3.5 to +0.1) | 132; 42.4 / 51.7 / 43.1%; −0.8% (−20.6 to +19.6); −2.2% (−3.4 to −1.0) |
| I1 | 856; 19.3 / 30.2 / 24.4%; −23.4% (−35.9 to −10.4); −4.8% (−5.6 to −3.9) | 88; 43.2 / 57.6 / 47.4%; −11.5% (−33.3 to +9.2); −0.9% (−2.6 to +0.8) | 190; 37.9 / 50.6 / 42.3%; −11.2% (−27.5 to +4.6); −1.8% (−3.0 to −0.6) |
| F1 | 851; 22.2 / 30.9 / 24.6%; −4.2% (−19.1 to +10.9); −4.4% (−5.3 to −3.6) | 90; 41.1 / 58.0 / 46.7%; −12.2% (−35.2 to +10.9); −1.8% (−3.4 to −0.1) | 185; 36.2 / 51.0 / 42.0%; −13.5% (−30.5 to +3.2); −2.1% (−3.3 to −1.0) |
| E1 | 1,666; 31.1 / 40.7 / 32.2%; −4.3% (−11.5 to +3.5); −4.5% (−4.9 to −4.1) | 407; 48.2 / 59.5 / 47.9%; +0.8% (−9.0 to +10.8); −2.9% (−3.6 to −2.2) | 723; 43.2 / 53.0 / 43.5%; −0.7% (−9.2 to +7.6); −3.4% (−3.9 to −2.9) |
| E2 | 1,441; 30.1 / 40.9 / 32.4%; −9.2% (−17.4 to −1.5); −4.4% (−4.9 to −4.0) | 401; 43.4 / 59.6 / 47.3%; −9.0% (−19.0 to +1.4); −3.0% (−3.7 to −2.3) | 641; 42.3 / 53.9 / 43.7%; −2.6% (−11.6 to +5.9); −3.3% (−3.9 to −2.8) |
| E3 | 1,399; 29.2 / 39.5 / 31.3%; −5.9% (−14.1 to +2.2); −5.3% (−5.8 to −4.9) | 253; 40.3 / 57.5 / 45.6%; −12.1% (−25.0 to +1.8); −3.8% (−4.7 to −3.0) | 543; 36.1 / 50.5 / 41.2%; −12.7% (−22.6 to −2.6); −4.0% (−4.6 to −3.4) |

By season (pooled, descriptive): every season's CLV range is below 0 for all three
rules. Baseline −3.6% to −5.8%, A −1.9% to −3.6%, B −2.8% to −3.5%.

**Decision (pre-registered rule):** A has 1,560 bets with a pooled CLV range at 97.5% of
−3.1% to −2.3%. B has 2,925 bets with −3.5% to −2.8%. Neither range lies above 0, so
**neither filter passes. No live change.** (Per league, A's 95% CLV range touches 0
only in La Liga, the Bundesliga and Serie A, with 66–107 bets each. Per-league numbers
are descriptive, not judged.)

**DraftKings E0 2025/26 check (descriptive): not run as registered.** The job found no
`odds-history-*` cache (the cached DraftKings snapshots `backtest-dk` reads), neither on
this branch nor on the default branch it can restore from. GitHub likely evicted it
after 7 days without use; the last run that read it was 10 Oct. Without the
snapshots, A and B can't be replayed on DraftKings, since both need every side's price
at each look. What can be shown, from data-log's `backtest/E0_dk.json` (generated 10
Oct, the baseline's 173 settled trades), is the baseline and the baseline's own picks
that already pass each filter. These subsets are *not* rules A and B: A or B could
also bet a different side, or at the other look, in matches the baseline bet on a long
shot.

| DraftKings E0 2025/26 | Bets | Won / model / implied | ROI (95%) | CLV vs DraftKings (95%) | CLV vs Pinnacle (95%) |
| --- | --- | --- | --- | --- | --- |
| Baseline (as `E0_dk.json`) | 173 | 23.7% / 31.4% / 25.1% | −13.4% (−38.3% to +14.1%) | −6.2% (−7.3% to −4.9%) | −6.3% (−7.6% to −4.9%) |
| Baseline picks with model > 50% | 13 | 53.8% / 57.1% / 48.7% | +7.8% (−53.1% to +59.0%) | −3.8% (−6.7% to −0.6%) | −3.6% (−6.6% to −0.1%) |
| Baseline picks priced ≤ 3.00 | 34 | 41.2% / 48.8% / 41.5% | −3.6% (−40.9% to +39.7%) | −4.5% (−6.4% to −2.6%) | −4.6% (−6.5% to −2.7%) |

## Verdict (Model 12% filters)

**Neither filter is worth keeping.** Both filters do what the owner hoped in one way:
they drop the long shots where the model overrates its chances most. Bets fall from
about 8,900 to 1,560 (A) or 2,925 (B), average odds from 4.5 to about 2.2–2.4, and the
average loss against Pinnacle's close shrinks from 4.6% to 2.7% (A) or 3.1% (B).
But the model still overrates its picks by a wide margin. Model favourites it gives
58.7% win 44.8% of the time, against 47.2% implied by the price. The filtered bets
still lose to the closing price in every league and every season, and lose money
overall. A smaller loss is still a loss, and the pre-registered bar (CLV range above 0)
isn't close: both upper ends are about −2.3% or lower. The live Model 12% test can stay
as the owner's paper test or be stopped. Adding either filter would only make it lose
more slowly. No handover.

## Price-band edge rule (owner, 20 Oct; pre-registered 2026-10-20, before any code or run)

**Owner's request.** The match rule bets the side with an edge, but in a three-way market
an edge often sits on a side that isn't the favourite, and the model's underdog edges look
like its mistakes. Dig into how often the underdog wins when the model disagrees with the
price, and test a stricter edge on underdogs and a looser one on favourites, so fewer,
better bets might win. The live numbers that prompted this (Model 12% trades and E0's
DraftKings backtest: in the 3.50–6.00 band the model said about 28%, the price 22%, and
15–18% won) are why the bands were chosen, so they are not evidence for the rule.

**What is already known (disclosed).** The Model 12% filters test above used the same
data: baseline CLV −4.6% (8,911 bets), A (model above 50%) −2.7%, B (price 3.00 or
shorter) −3.1%, every season below 0. So I expect every band's CLV to be below 0 at every
threshold, the favourite bands least so. This test differs in two ways: each band gets its
own minimum edge, chosen on early seasons only, and favourites may qualify below 12%.

**Data (identical to the Model 12% filters test).** The model's walk-forward chances
(`backtest.walk_forward`, Dixon-Coles refitted every 7 days on the 730 days before each
window, from 2019-07-01, burn-in 2 seasons, xG weight 0.7 in E0, SP1, D1, I1, F1 and
goals only in E1, E2, E3; no look-ahead); Pinnacle's own early 1X2 from football-data
(PSH/PSD/PSA, quoted, margin kept); CLV = entry odds × Pinnacle's Shin-fair close − 1.
Leagues E0, SP1, D1, I1, F1, E1, E2, E3; only seasons where Pinnacle's early and close
prices both cover 90% of played matches. Edge = model chance × quoted decimal − 1.

**Split (fixed now).** Development 2019/20–2022/23. Check 2023/24–2024/25, used only once,
after the rule below is frozen by the code from development alone. 2025/26 and later are
dropped before anything is computed.

**Bands (by the side's Pinnacle early quoted decimal price o).**
- `fav`: home or away, o < 2.00
- `mid`: home or away, 2.00 ≤ o ≤ 3.50
- `dog`: home or away, 3.50 < o ≤ 6.00
- `long`: home or away, o > 6.00
- `draw`: the draw, at any price

**Step 1, descriptive (development only).** Every side with edge > 0, per band × claimed
edge bucket (0–5%, 5–10%, 10–15%, 15–20%, 20–30%, 30%+): sides, the model's average
chance, the implied chance (1/o), the win rate, ROI per $1 and CLV, each with a 95% range.
This answers "how often does the underdog win when the model disagrees".

**Step 2, the rule, chosen on development only.** For each band and each threshold t in
{2%, 5%, 8%, 12%, 15%, 20%, 25%, 30%}: the band's sides with edge ≥ t, each scored on its
own (a match can give a home and a draw side). The band's threshold is the t with the
highest mean CLV among the t with at least 200 development sides (ties: the higher t). A
band is switched **off** when that best mean CLV is 0 or below.

**Step 3, the frozen rule R.** Per match, among the sides that clear their band's
threshold (bands that are off can't), bet the one with the highest edge (ties: home,
draw, away), $1, one bet per match. If every band is off, R makes no bets and the result
is "no rule survives development".

**Step 4, the check (2023/24–2024/25, once).** R's pooled bets across the eight leagues:
bets, win rate vs model vs implied, ROI (95% range), CLV (95% range), beat-the-close.
**Decision:** R is worth a live paper test if its pooled CLV 95% range lies above 0 with at
least 300 bets (one test, so 95%). ROI is reported, not judged.

**Descriptive alongside (not judged).** On the same check seasons: the baseline (12% on
every side, the live rule), filters A and B, and R* = the same selection without
switching any band off (each band at its best development t whatever its sign), so the
owner can see whether band thresholds beat one flat 12% out of sample. R per band and
per league. Ranges are percentile bootstraps (2,000 draws, fixed seeds) resampling
matches.

**How it runs.** `soccer-stats backtest-band-edges` (`lab/band_edges.py`), `odds-check.yml`
`task=band-edges`: no key, 0 credits, never pushes; log and a 7-day artifact. One run.

### Results (2026-10-20; `odds-check.yml` run 39388494286, 0 credits, no key)

**Order.** Pre-registration c6b8aff (07:34:45 UTC), code a43f8cf, then the one run
39388494286 (dispatched 07:44:15 UTC on a43f8cf). Nothing was run before the
pre-registration and nothing has been run since.

**Data.** Eight leagues, 2019/20–2024/25 all scored (Pinnacle early and close coverage
≥ 90% in every season; 2025/26 dropped first): E0 2,215 matches, SP1 2,212, D1 1,782,
I1 2,185, F1 1,971, E1 3,184, E2 2,984, E3 3,020. Development 13,007 matches, check 6,546.

**Step 1: how often the side the model likes wins (development, every side with edge > 0).**
"Model" is the model's average chance, "implied" 1/price at Pinnacle early (margin
included), ROI per $1 and CLV vs Pinnacle's fair close with 95% ranges (matches resampled).

| Band | Sides | Model | Won | Implied | ROI | CLV |
| --- | --- | --- | --- | --- | --- | --- |
| Favourite (< 2.00) | 1,420 | 63.1% | 57.1% | 58.7% | −2.8% (−7.2% to +1.8%) | −2.4% (−2.7% to −2.1%) |
| Middle (2.00–3.50) | 5,620 | 43.2% | 36.0% | 38.1% | −5.6% (−8.7% to −2.2%) | −3.4% (−3.6% to −3.2%) |
| Underdog (3.50–6.00) | 3,124 | 27.7% | 21.3% | 23.0% | −6.6% (−13.1% to −0.2%) | −5.0% (−5.4% to −4.7%) |
| Long shot (> 6.00) | 1,570 | 15.5% | 11.6% | 11.9% | +7.2% (−7.9% to +23.5%) | −6.1% (−6.9% to −5.4%) |
| Draw | 4,018 | 26.3% | 23.6% | 24.3% | −3.4% (−9.3% to +2.3%) | −3.1% (−3.4% to −2.9%) |

In every band the side the model likes wins a little *less* often than the price says,
not more, and a bigger claimed edge doesn't help: favourites at a 20%+ edge won 50.5%
(model 70.3%, implied 55.7%, 91 sides); underdogs at 15–20% won 17.0% (model 27.0%,
implied 23.0%). The long-shot ROI is one season's luck: its CLV is the worst of all.

**Step 2: thresholds (development).** Best mean CLV per band among thresholds with ≥ 200
sides: favourite 2% (−2.3%, 1,139 sides), middle 30% (−2.8%, 537), underdog 30% (−4.3%,
766), long shot 30% (−5.6%, 686), draw 2% (−3.4%, 3,215). Every best CLV is below 0, so
**every band is switched off**.

**Step 3–4: the frozen rule and the check.** R makes no bets, so there is nothing to
check: **no rule survives development; no live change.**

**Descriptive on the check seasons (2023/24–2024/25, 6,546 matches, not judged).**

| Rule | Bets | Won | Model | Implied | ROI | CLV |
| --- | --- | --- | --- | --- | --- | --- |
| R* (each band at its best t, none off) | 2,336 | 30.1% | 38.6% | 32.6% | −10.7% (−17.3% to −4.3%) | −4.4% (−4.7% to −4.0%) |
| Baseline 12% (the live rule) | 2,761 | 26.1% | 35.9% | 28.6% | −13.1% (−18.6% to −6.6%) | −5.4% (−5.8% to −5.0%) |
| A, model > 50% | 489 | 46.2% | 58.9% | 47.3% | −2.3% (−11.4% to +6.9%) | −3.1% (−3.7% to −2.4%) |
| B, price ≤ 3.00 | 900 | 40.4% | 52.3% | 42.9% | −6.0% (−13.6% to +1.7%) | −3.3% (−3.8% to −2.8%) |

R* by band on the check: favourite 608 bets, won 54.9% vs 58.5% implied, CLV −2.6%;
middle 249, won 38.6% vs 36.1%, ROI +7.8% (−8.9% to +25.3%) but CLV −4.0%; underdog
375, won 17.1% vs 22.2% (model 32.5%), ROI −23.5%, CLV −5.6%; long shot 223, won 6.7%
vs 12.4% (model 18.9%), ROI −45.0%, CLV −11.0%; draw 881, won 21.9% vs 23.2%, CLV −3.5%.
The baseline's CLV range is below 0 in every one of the eight leagues (−4.6% to −6.3%).

**Verdict.** The owner's idea is right about direction: the further the side is from
favourite, the worse the model's picks do (CLV −2.4% on favourites to −6.1% on long
shots in development, −2.6% to −11.0% on the check), so being stricter on underdogs and
looser on favourites loses *less* than the flat 12% (R* −10.7% vs −13.1% ROI, CLV −4.4%
vs −5.4%). But no band at any threshold beats Pinnacle's close: even the model's
favourites win less often than the price implies. Taking fewer bets trims the losses;
it doesn't find a winning subset. No live change; the paper tests run as they are.

## Favourites coming back from behind (owner, 24 Oct; pre-registered 2026-10-24, before any code or data is loaded)

**Owner's idea.** Back the pre-match favourite in play after it falls behind, on the bet that
favourites come back more often than the in-play price allows. Phase 1 (this test) uses free
data only and asks the necessary first question: do favourites come back more often than a
fair, time-scaled in-play model expects? If they don't, a market priced like that model
already prices the comeback and phase 2 (in-play prices, credits) isn't worth buying.
0 credits, no key, no live change. Descriptive research: nothing here opens a trade.

**Matches and seasons.** Development 2017/18–2024/25 (start years 2017–2024), the window of
the other lab work. Every match from 1 July 2025 is dropped before anything is computed;
2025/26 stays a possible holdout (`lab.harness.Holdout`, locked). Leagues: E0, SP1, D1, I1,
F1 (both events) and E1, E2, E3 (event B only, see below). Played matches only.

**The favourite.** Pinnacle's own closing 1X2 from football-data (PSCH/PSCD/PSCA; never the
average or Bet365 fallback), margin removed with Shin. The favourite is the side (home or
away) with the larger fair win chance. Strength bands by that chance: **45–55%, 55–65%,
65–75%, 75%+**. Matches whose favourite is under 45%, or without Pinnacle's close, are left
out (counted).

**Events** (each match can enter both; within an event, a match counts once):
- **Event A, conceded first:** the first goal of the match is scored by the underdog (an own
  goal counts for the side credited with it). Goal minutes from Understat's per-match shot
  data (`getMatchData`, result "Goal" or "OwnGoal", with its minute), E0, SP1, D1, I1, F1
  only. Understat's own-goal side convention is taken as the one that rebuilds
  football-data's final score in more matches; any match whose rebuilt final score differs
  from football-data's FTHG/FTAG is dropped and counted (a data check, not an outcome
  read). The Championship, League One and League Two have no Understat data; ESPN's
  historical summaries for 2017–2024 would be about 13,000 requests with unknown coverage,
  so event A leaves them out in phase 1 (stated in the report). First-goal minute bands:
  **0–30, 30–60, 60+** (Understat's minute; stoppage time counts in its band).
- **Event B, behind at half-time:** football-data's HTHG/HTAG, all eight leagues. Any
  deficit; the 1-goal and 2+-goal splits are reported beside it (descriptive).

**Outcomes after each event:** the favourite's final win, draw and loss rates, and "avoided
defeat" (win or draw), with 95% ranges from resampling matches.

**Benchmark: a fair time-scaled in-play model.**
- Pre-match goal means (favourite λf, underdog λu): independent Poisson means fitted by least
  squares to Pinnacle's margin-free closing 1X2 **and** closing over/under 2.5 (PC>2.5/PC<2.5,
  Shin), the same fit as `team_totals.implied_means`. The total needs the over/under price
  (the h2h alone can't pin it: round 13). Matches without Pinnacle's closing over/under get no
  benchmark and are counted; the benchmark is reported only for league-seasons where it
  covers at least 90% of the event's matches (the coverage table says which).
- Remaining goals: each side's goals after the event are Poisson with mean λ × (minutes left /
  90), minutes left = 90 − event minute (event A: the first goal's minute, capped to 0–90;
  event B: 45). A uniform scoring rate is the stated simplification; real goal rates rise
  late in matches, so this model, if anything, leans toward more late goals being needed.
  The favourite wins if its remaining goals exceed the underdog's by more than the deficit;
  draws if by exactly the deficit.
- Per match: predicted P(win), P(draw), P(avoid defeat) given the score at the event.

**Tests (the family).** Primary measure: realized − predicted favourite **win** rate
(per-match residual y − p, averaged), with a match-resampled range. Cells: event A × 4
strength bands × 3 minute bands (12) and event B × 4 strength bands (4), plus each event
pooled over bands (2): **18 tests, 99.722% ranges** (Bonferroni). "Avoided defeat" and draw
residuals are reported at the same level, descriptive. Calibration per event: a logistic
slope and intercept of the outcome on logit(predicted P(win)), pooled (descriptive; 1 =
perfectly scaled). Per-league tables at 95%, descriptive.

**Verdict rule.**
- **Gap found** if any of the 18 win residual ranges is entirely above 0: favourites win more
  often than the fair model says in that cell. That is the condition for proposing phase 2.
- **Below the model** if ranges sit entirely below 0 (favourites come back less often than the
  model; backing them in play at fair-model prices would lose).
- Otherwise **no gap shown.** Phase 1 can't say whether real in-play prices already include
  any gap; only phase 2 can.

**Phase 2 note (not run; owner's decision, credits).** The Odds API's historical snapshots
cost about 10 credits per region and market, so a ≤5-credit probe can't fetch one; a live
`/odds` call during a match (1 credit) can show whether in-play prices are returned at all.
Costed in the report only if phase 1 finds a gap.

**Code.** `lab/comebacks.py`; `odds-check.yml` `task=comebacks-fetch` with `league`
(downloads and caches one league's Understat match files, prints counts only, never
outcomes) and `task=comebacks` (the analysis, all leagues pooled).

### Results (2026-10-24; `odds-check.yml` run 40082512216, 0 credits, no key; ranges 99.722% unless marked)

**Data.** Understat goal minutes joined to football-data for 9,489 of 9,519 favourite
matches in the five Understat leagues (E0 2,106, SP1 1,931, D1 1,639, I1 2,050, F1 1,763;
9–10 Understat pages missing per league; one D1 match dropped because Understat's goals
didn't rebuild football-data's score). Understat lists own goals under the side credited
with them (the convention that rebuilt every other score). The benchmark needs Pinnacle's
closing over/under 2.5, which football-data lacks for E0 in 2017/18 and 2018/19, so those
two seasons have rates but no benchmark (2,174 of 2,849 event-A matches and 2,166 of 2,879
event-B matches benchmarked). Event A's minute bands: the underdog's opener came in 0–30 in
1,717 matches, 30–60 in 821, 60+ in 311.

A first analysis run (40081691108) restored none of the five Understat caches (its restore
steps listed a different path from the save step, so the cache version differed) and saw
event A for only 819 Premier League matches an older cache held. Its event-B numbers are
identical to the ones below (football-data only). The workflow fix is commit 975ff3a; the
run above is the one reported. Nothing else changed between the two runs.

**Event A, the underdog scores first** (win = the favourite's final win rate; model = the fair
time-scaled prediction on the benchmarked matches; residual = realized − predicted):

| Favourite | Opener | Matches | Win | Draw | Avoided defeat | Model win | Win residual (99.7%) | Test |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 45–55% | 0–30 | 775 | 27.0% | 26.3% | 53.3% | 23.1% | +3.9% (−1.4% to +9.1%) | no gap |
| 45–55% | 30–60 | 390 | 17.4% | 29.7% | 47.2% | 13.8% | +3.6% (−3.5% to +10.0%) | no gap |
| 45–55% | 60+ | 158 | 0.6% | 27.2% | 27.8% | 3.4% | −3.4% (−3.9% to −2.8%) | below model* |
| 55–65% | 0–30 | 488 | 37.7% | 27.3% | 65.0% | 31.1% | **+8.3% (+1.3% to +15.0%)** | **above model** |
| 55–65% | 30–60 | 244 | 22.5% | 29.1% | 51.6% | 18.8% | +4.7% (−4.5% to +13.9%) | no gap |
| 55–65% | 60+ | 91 | 5.5% | 24.2% | 29.7% | 4.5% | +2.5% (−4.6% to +11.9%) | no gap |
| 65–75% | 0–30 | 277 | 46.6% | 23.5% | 70.0% | 41.4% | +2.2% (−7.2% to +12.9%) | no gap |
| 65–75% | 30–60 | 118 | 29.7% | 27.1% | 56.8% | 24.6% | +8.3% (−5.1% to +23.8%) | no gap |
| 65–75% | 60+ | 38 | 5.3% | 26.3% | 31.6% | 4.9% | +2.5% (−6.7% to +21.6%) | no gap |
| 75%+ | 0–30 | 177 | 58.8% | 20.9% | 79.7% | 54.8% | +0.2% (−13.9% to +13.2%) | no gap |
| 75%+ | 30–60 | 69 | 44.9% | 27.5% | 72.5% | 35.2% | +12.6% (−9.9% to +33.8%) | no gap |
| 75%+ | 60+ | 24 | 25.0% | 12.5% | 37.5% | 7.1% | +16.4% (−7.6% to +45.0%) | no gap |
| **Pooled** | | **2,849** | 29.1% | 26.5% | 55.6% | 24.5% | **+4.3% (+1.6% to +6.9%)** | **above model** |

\* No benchmarked favourite won (0 of 129), so resampling can't make a win and the range
collapses around −3.4%; read it as "won no more than the model's 3.4%". Its draw residual is
+11.2% (+0.9% to +23.7%): those favourites rescued draws, not wins.

Avoided-defeat residual, pooled: +6.3% (+3.1% to +9.1%). Draw residual pooled +2.0% (−0.7% to
+4.8%). Calibration of the win (logistic on logit of the model's chance): slope 0.97,
intercept +0.21, so the model ranks the comebacks right but sits uniformly too low.

By league (95%, descriptive): E0 +6.9% (+3.4% to +10.9%), I1 +6.8% (+2.9% to +11.1%), SP1
+5.5% (+1.6% to +9.5%), F1 +3.7% (−0.4% to +7.7%), D1 −2.7% (−6.7% to +1.5%).

**Event B, behind at half-time** (eight leagues):

| Favourite | Matches | Win | Draw | Avoided defeat | Model win | Win residual (99.7%) | Test |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 45–55% | 1,637 | 15.8% | 25.0% | 40.8% | 10.8% | **+5.5% (+2.4% to +8.7%)** | **above model** |
| 55–65% | 795 | 20.6% | 27.3% | 47.9% | 15.1% | **+5.8% (+0.8% to +10.7%)** | **above model** |
| 65–75% | 306 | 27.5% | 26.1% | 53.6% | 21.3% | +5.0% (−3.3% to +13.5%) | no gap |
| 75%+ | 141 | 39.0% | 29.8% | 68.8% | 29.8% | +9.4% (−5.7% to +22.2%) | no gap |
| **Pooled** | **2,879** | 19.5% | 26.0% | 45.5% | 14.0% | **+5.7% (+3.5% to +8.2%)** | **above model** |

By deficit (95%): one goal behind, 2,356 matches, win 22.2% vs model 16.2%, +5.7% (+3.9% to
+7.7%); two or more behind, 523 matches, win 7.3% vs 3.9%, +5.7% (+2.8% to +8.6%), draw
residual +4.6%. Avoided-defeat residual pooled +6.6% (+3.5% to +9.8%); draw residual +0.9%
(−1.9% to +4.0%). Calibration slope 0.72, intercept −0.05: the model's chances are too spread
out, too low most of all where it says a comeback is unlikely.

By league (95%, descriptive): I1 +12.2%, E0 +8.2%, SP1 +8.2%, F1 +5.7%, E2 +4.8%, E1 +4.2%
(all ranges above 0), D1 +2.9% (−1.7% to +7.8%), E3 +1.0% (−2.5% to +4.9%).

**Verdict (pre-registered rule): gap found.** Five of the 18 tests sit entirely above 0: both
pooled tests, event B's 45–55% and 55–65% bands, and event A's 55–65% favourites who concede
in the first 30 minutes. One cell sits below (45–55% favourites who concede after 60 minutes;
see the footnote). Favourites who fall behind win about 4–6 points more often than the fair
time-scaled model says (event A 28.8% vs 24.5%; event B 19.7% vs 14.0% on the benchmarked
matches), most clearly for modest favourites (45–65%) and in the Premier League, Serie A and
La Liga; the Bundesliga shows nothing.

**What the gap is against, and two checks after the run (exploratory, not pre-registered).**
The benchmark is deliberately simple: a constant scoring rate and no game state. Two checks,
run locally on the same football-data matches after the results above were read:
- *Second halves have more goals.* Over the eight leagues' 27,341 matches, 55.5% of goals come
  after half-time. Giving event B's model that share of the remaining goals (instead of 50%)
  lowers the pooled residual from +5.7% to **+3.8% (95%: +2.2% to +5.5%)**; 45–55% +4.0%,
  55–65% +3.7%, 65–75% +2.2%, 75%+ +5.8%. About a third of the half-time gap is the scoring
  rate, not the favourite.
- *Trailing underdogs, as a mirror.* When the favourite leads at half-time (5,202 benchmarked
  matches), the underdog wins 3.6% vs the model's 3.0% (+0.6%, 95% +0.2% to +1.1%, with the
  55.5% share), draw residual +1.7%. Trailing teams in general recover a little more than a
  game-state-free model says (the leader sits back, the trailer pushes); favourites recover
  more in absolute terms, about the same in relative terms (+24% vs +20% of the model's
  chance).

So the gap is real against a fair model without game state, and partly generic. An in-play
market prices the score, the clock and usually game state; whether it prices a trailing
favourite's comeback fully is the open question, and only in-play prices can answer it.

**Phase 2 cost (not spent; the owner's and lead's decision).** The pre-registered ≤5-credit
probe isn't possible for history: one historical snapshot (`/historical/sports/{sport}/odds`,
h2h, one region) costs about 10 credits, so the cheapest test of "do historical snapshots
carry in-play prices" is **10 credits**: one Premier League snapshot at a time when matches
were in play (e.g. a Saturday about 15:50 UK), checking whether started events come back with
prices and how fresh they are. The live alternative is a **1-credit** `/odds` call during a
live match, which shows whether in-play h2h prices are returned at all; collecting a test set
that way would take months of in-play calls (each refresh 1 credit per league). If history
does carry in-play prices, a phase-2 test needs a snapshot at half-time of each kickoff slot
(one snapshot covers every match in the league that kicked off together), about 10 credits a
slot. All eight leagues and seasons would be thousands of slots (tens of thousands of
credits); a realistic design is one league over recent seasons, about 300 slots and 3,000
credits (rough estimate; `estimate`-style replay before any spend).

### Phase 2, step 1: the archive keeps in-play prices (24 Oct, owner-approved 10 credits)

`lab/inplay_probe.py` (`odds-check.yml` `task=inplay-probe`, `league`, `cap`; `hist_dates` set to
one ISO time = a historical snapshot, else a live call). One historical h2h snapshot of the
Premier League (run 40136522857, **10 credits**, 19,124 → 19,114), asked for 10 Oct 2026
14:55 UTC and served at 14:50:36, 51 minutes after four 14:00 kickoffs (half-time, minute ~45
by ESPN's goal minutes):

| Match | Score then (final) | Pinnacle home / draw / away | Books priced |
| --- | --- | --- | --- |
| Aston Villa v Brentford | 1-1 (2-2) | 3.10 / 2.79 / 2.70 | 7 |
| Chelsea v Bournemouth | 2-0 (5-1) | 1.08 / 11.27 / 38.4 | 7 |
| Sunderland v Brighton | 0-0 (0-2) | 3.31 / 2.60 / 2.74 | 6 |
| Ipswich v Fulham | 0-0 (2-1) | 3.60 / 2.66 / 2.51 | 7 |

- **All four started matches came back with in-play h2h prices**, at Pinnacle, DraftKings,
  Betfair's exchange (margin 0.0–1.3%), BetMGM, Betway, Unibet and Paddy Power; FanDuel and
  William Hill returned pre-match prices only. Book updates were 0.1–0.9 minutes old (one
  Betway quote 12.8). The prices follow the score (Chelsea 2-0 up at 1.06–1.09; the 0-0 and 1-1
  matches priced as level games), so they are true in-play prices, not stale pre-match ones.
- No favourite was trailing at half-time in this snapshot, so it says nothing yet about the
  comeback gap itself. It answers the gating question only: a historical phase-2 test can be
  bought, with Pinnacle's in-play price as the sharp benchmark.
- **Cost lever for phase 2:** the free data (football-data half-time scores, ESPN/Understat goal
  minutes) already say which kickoff slots had a favourite behind at half-time, so a test need
  only buy those slots' half-time snapshots (10 credits each), not every slot. The live 1-credit
  check (25 Oct) shows whether live in-play prices come back the same way.

**A second historical snapshot** (run 40136975819, **10 credits**, 19,114 → 19,104; a
duplicate probe from the research session, closed PR #101). Premier League, asked for 25 May
2025 15:55 UTC and served at 15:50:37, about 50 minutes after all ten final-day 15:00 kickoffs:
- All ten started matches had in-play h2h prices, from Betfair's exchange, BetMGM, Bovada,
  DraftKings, FanDuel, Pinnacle, 888sport and Unibet. Most were 4.6 minutes old, FanDuel's 8.7
  (FanDuel quoted in play then, unlike on 10 Oct 2026). William Hill and Marathonbet quoted none.
- **Pinnacle's quote was inconsistent with the score in some matches.** Southampton 0-1 Arsenal
  had Arsenal at 1.34 at Pinnacle (1.25 before kickoff) against 1.09–1.15 at the other books.
  Ipswich 0-1 West Ham had West Ham at 2.47 against 1.36–1.47. In the other matches Pinnacle
  was in line, as on 10 Oct 2026. So Pinnacle's archived in-play quote is sometimes stale, not
  always wrong: a test checks each one against the other books.
- Two favourites trailed at half-time (anecdotes, not a test). Liverpool, 72% before kickoff,
  were 0-1 down to Crystal Palace, with Betfair at 2.64 (38% to win). Brighton, about 50%,
  were 0-1 down at Tottenham, with Betfair at 4.4 (23%). Phase 1's base rates for a favourite
  one goal down at half-time are 30.5% (65–75% band) and 18.2% (45–55%), so the market was
  more generous than history in both.

The two probes spent **20 credits** together.

### Phase 2, step 1b: live in-play prices come through (25 Oct, owner-approved 1 credit)

One live `/odds` call (`task=inplay-probe`, `league=E0`, `cap=1`; run 40204253352, **1 credit**,
10,979 → 10,978) at 14:52 UTC, half-time of four Premier League matches that kicked off at
14:00. All four came back with live h2h prices from 8–9 books (Pinnacle, DraftKings,
Betfair's exchange, FanDuel, BetMGM, Betway, Unibet, Paddy Power), updated 0.2–1.4 minutes
before the call, and the prices matched ESPN's half-time scores: Manchester United 0-2
Bournemouth had United at 16–23.8 and Bournemouth at 1.14–1.17 (a trailing favourite, priced
live); Liverpool 2-0 Brighton had Liverpool at 1.08–1.13; Hull 1-0 Brentford and Crystal
Palace 0-0 Newcastle priced as such. Sky Bet's Manchester United line (2.9 / 2.7 / 2.7) was
stale, a reminder that some books lag in play. So live in-play betting at DraftKings' price is
possible from this API; with design C closed (the half-time price over-prices the comeback),
there is no live strategy to run on it.

### Phase 2 costing (lead brief, 24 Oct; a costed proposal, nothing pre-registered or spent)

**Prices to use.** Both probes (above) found in-play prices at Betfair's exchange, the US books
and Pinnacle, but Pinnacle's archived quote was sometimes stale (May 2025). The test therefore
checks every Pinnacle quote against the median of the other books and falls back to Betfair's
exchange when it disagrees (the quote check below).

**Planner** (`lab/comeback_plan.py`, `odds-check.yml` `task=comeback-plan`; football-data
only, no key). A kickoff slot is one league's matches that kicked off together. One historical
snapshot of that league (h2h, ten books = one region, 10 credits) catches every match in the
slot at half-time. The planner:
- uses football-data's `Time` (UK local) for kickoffs and requests kickoff + 55 min. The archive
  returns the latest 5-minute snapshot at or before that, so +50 to +55, inside the half-time
  break;
- finds the favourites the same way as phase 1 (Pinnacle's close, ≥ 45%);
- reads only which favourites trailed at half-time, never the final score.

| League | 2022/23 | 2023/24 | 2024/25 | 2025/26* |
| --- | --- | --- | --- | --- |
| E0 | 51 matches, 46 slots | 44, 37 | 50, 41 | 26, 22 |
| SP1 | 35, 34 | 44, 44 | 36, 36 | 16, 16 |
| D1 | 28, 23 | 33, 29 | 41, 35 | 10, 9 |
| I1 | 36, 35 | 34, 34 | 42, 41 | 18, 18 |
| F1 | 45, 42 | 32, 31 | 41, 39 | 21, 21 |
| E1 | 46, 37 | 57, 37 | 57, 43 | 31, 26 |
| E2 | 50, 36 | 59, 40 | 69, 41 | 19, 13 |
| E3 | 52, 36 | 70, 42 | 60, 42 | 20, 12 |

Each cell is the number of trailing favourites and the snapshots that cover them; credits = 10 ×
slots. \* football-data's 2025/26 files have Pinnacle's close for only 30–55% of matches, so the
favourites there are undercounted.

**Designs.** Each "detects" figure is the gap between the realized win rate and the market's
implied chance that a two-sided 5% test finds with 80% power, at a win rate near 20%.

| Design | Snapshots | Credits | Trailing favourites | Detects a gap of |
| --- | --- | --- | --- | --- |
| A: Premier League, 2022/23–2025/26 | 146 | 1,460 | 171 | 8.6 points |
| B: top five leagues, 2024/25 | 192 | 1,920 | 210 | 7.7 points |
| Top five leagues, 2022/23–2024/25 | 547 | 5,470 | 592 | 4.6 points |
| C: all eight leagues, 2022/23–2024/25 | 901 | 9,010 | 1,112 | 3.4 points |
| D: all eight leagues, four seasons | 1,038 | 10,380 | 1,273 | 3.1 points |

Phase 1's half-time gap after the second-half correction is +3.8 points (against a simple model,
not the market). Only C or D could detect a market gap that size, which needs about 870
matches. The key had about 19,100 credits on 24 Oct, is shared with baseball, and live spend is
about 3,600 a month, so C would take roughly half the balance. A and B can only find a large
mispricing (8 points or more). They are pilots, useful mainly to see whether the in-play market
already prices the comeback. Whether the archive has in-play prices for the Championship,
League One and League Two is unchecked; one 10-credit snapshot per league would settle it
before C or D.

**"Conceded first" (event A)** can't share snapshots: each match needs its own, a few minutes
after its goal. Phase 1 had about 360 such matches a season in the top five, so roughly 3,600
credits a season. Not proposed.

**What the pre-registered test would be** (to be written and committed before any purchase):
- *Quote check:* a book's quote counts only if it was updated after kickoff and at most 5
  minutes before the snapshot, and its implied chance for the trailing favourite is below the
  same book's pre-match price. A Pinnacle quote also has to sit within a set tolerance of the
  other passing books' median implied chance (the tolerance is fixed at pre-registration),
  else Betfair's exchange is the fair price for that match. Matches with no passing quote are
  counted and dropped.
- *Primary: bet the trailing favourite at the in-play price.* $1 flat per match at DraftKings'
  passing in-play price (the owner's book), with Betfair's exchange back price (less 2%
  commission on wins) as a second line. ROI with a match-resampled 95% range, overall and by
  phase 1's bands. CLV doesn't exist in play, so ROI is the measure. A pass needs the range
  entirely above 0.
- *Secondary: model against the in-play market.* The fair price is the exchange's prices
  normalised, or Pinnacle's Shin-fair price where it passes the quote check. Report realized
  minus market-implied win rate by band, and the phase-1 model (with the 55.5% second-half
  share) against the same market. Does it add anything beyond the price? Use a logistic blend
  weight on the model with a match-resampled range, as in the lab's other tests.
- Development and judging sets fixed in advance (for example, a pilot on B, with C minus B as
  the confirmatory set), with the confirmatory set opened once.

**Recommendation.** If the owner wants to go on: B as a pilot (1,920 credits), plus 30 credits
to check the lower leagues. Expand to C only if B's point estimate is positive and the owner
accepts the spend. Nothing is bought until the test is pre-registered and the owner approves the
cap.

## Draw no bet (owner, 24 Oct; pre-registered 2026-10-24, before any code or run)

**Owner's question.** Instead of the 3-way moneyline, bet draw no bet (DNB): back home or away,
stake refunded on a draw. The hope: a higher hit rate, and a binary result that is easier to
predict and to monitor. Test both live match rules as DNB on history, beside the same rules on
1X2, at 0 credits.

**What is already known (disclosed).** DNB is the Asian handicap at line 0. Round 2 (`docs/edge.md`,
Premier League, Pinnacle's AH from 2019/20, the main line only) found model CLV at 12% of −2.0% on
AH against −4.2% on 1X2, and a blend CLV of −0.2% to +0.8% at 2–8% edges with no range above 0.
The Model 12% filters and price-band tests (above) used the same model chances and seasons as this
test. The model overrates the side it likes in every price band, and that is a home-vs-away error,
which DNB keeps. So I expect both DNB rules' CLV below 0, DNB 12% less negative than 1X2 12%.

**Data** (as the Model 12% filters and the Lean backtest, `soccer-stats backtest-lean`):
- Model chances: `backtest.walk_forward` (weekly Dixon-Coles refits on the 730 days before each
  window, from 2019-07-01, burn-in 2 seasons, xG weight 0.7 in E0, SP1, D1, I1, F1, goals only in
  E1, E2, E3). No look-ahead.
- Leagues: the eight live domestic leagues. The Champions League is left out (no early prices).
- Seasons: 2019/20–2024/25, in each league only the seasons where Pinnacle's own early and close
  1X2 both cover at least 90% of played matches (`lean_backtest.coverage`). 2025/26 and later are
  dropped before any bet is made.
- The bet is decided at Pinnacle's **early** price (football-data PSH/PSD/PSA), as in
  `lean_backtest`; training for any fit uses matches played before the block's decision day.

**The DNB price (fixed now).**
- **Primary, synthetic from 1X2:** DNB decimal price on a side = its 1X2 price × (1 − 1/draw price),
  from Pinnacle's early quoted prices (margin kept). That is the price you get by backing the side
  and covering the draw at the same book, so it can be built live from DraftKings' h2h with no new
  market. Break-even = 1/price.
- **Fair DNB chance** (early and close) = Shin-fair p_side / (p_home + p_away) from the 1X2 prices.
- **Check, real AH 0:** where football-data's Pinnacle Asian handicap line is 0 (AHh = 0 early,
  AHCh = 0 close; PAHH/PAHA, PCAHH/PCAHA), the real DNB price. Descriptive only: the synthetic's
  margin against AH 0's, the gap between their fair chances, and both rules rescored at the AH-0
  price on those matches (a biased subset: only close matches have line 0).

**The chances (no new fit, no new parameter).**
- Model DNB: q = p_side / (p_home + p_away) from the model's walk-forward 1X2.
- Blend DNB: the same renormalisation of the Lean backtest's own walk-forward 1X2 blend
  (`lean_backtest.walk_forward`: refitted every 28 days on earlier matches, Pinnacle's close,
  at least 300 rows).
- σ_DNB for each fit: the sd (ddof 0) of (blend DNB − fair DNB at the close), home side, over that
  fit's own training rows (the away side is its mirror).

**The rules (fixed now).** One bet per match, $1.
- **DNB 12%:** edge = q_model × DNB price − 1 (the edge per stake that isn't refunded); the side
  with the larger edge if it is at least 12% (minus 1e-9); home first on a tie.
- **DNB Lean:** z = (q_blend − 1/DNB price) / σ_DNB per side; the higher z if it is at least 1;
  "strong" at 2.
- **Baselines on the same scored matches:** 1X2 Model 12% (`lab/edge12_filters` baseline) and 1X2
  Lean (`lean_backtest.bets`, all odds).
- Settlement: a win pays price − 1, a draw returns the stake (0), a loss costs 1.

**Measured** (per league, per season, pooled over the eight leagues; ranges resample matches,
2,000 draws):
- bets, won / drawn (refunded) / lost, hit rate = won / (won + lost) against break-even;
- ROI per $1 staked with its 95% range;
- CLV = entry DNB price × Pinnacle's fair DNB close − 1 (per stake not refunded), with its 95%
  range, and the same per $1 staked (× the close's no-draw chance);
- monitoring: the sd of profit per bet, and bets needed for a 5% edge to show at 95% =
  (1.96 × sd / 0.05)², for each DNB rule beside its 1X2 baseline;
- odds bands (≤ 1.50, 1.50–2.00, 2.00–3.00, 3.00–5.00, > 5.00): bets, ROI, CLV;
- calibration (descriptive): on every scored match without a draw, the home side's model, blend and
  fair price DNB chance against what happened (`calibration.reliability`), and each rule's bets.

**Pass rule (fixed now).** A DNB rule passes if its pooled CLV range at 97.5% (two rules) is
entirely above 0. A pass goes to the owner for a live DNB paper track at DraftKings (synthetic from
its h2h). If neither passes, nothing changes live. The owner may still choose a DNB paper track for
easier monitoring, but not as an edge. Lower variance on its own is not a pass: a strategy that
loses with less noise still loses. Everything else is descriptive.

**Run:** `backfill.yml` input `dnb_backtest` (`soccer-stats backtest-dnb`), 0 credits, no key;
from a branch it prints and keeps a 7-day artifact, from the default branch it writes data-log
`backtest/dnb_backtest.json`.

### Results (2026-10-24; `backfill.yml` run 40140494754 from the branch, 0 credits, no key)

Eight leagues, 2019/20–2024/25, 19,553 scored matches (draws 22.9–27.7% by league). Every
rule is on the same matches; ranges resample matches. CLV for DNB is per stake not refunded;
"per $" multiplies by the close's no-draw chance, so it compares directly with 1X2.

| Rule | Bets | Won / refunded / lost | Hit rate vs break-even | Avg odds | ROI (95%) | CLV (97.5%) | CLV per $ | Bets for a 5% edge |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| DNB 12% | 7,017 | 1,652 / 1,738 / 3,627 | 31.3% vs 34.1% | 3.90 | −5.0% (−8.5% to −1.3%) | −6.2% (−6.5% to −5.8%) | −4.8% | 3,737 |
| 1X2 Model 12% | 8,909 | 2,370 / 0 / 6,539 | 26.6% vs 28.4% | 4.52 | −5.6% (−9.5% to −1.7%) | −4.6% (−4.9% to −4.4%) | −4.6% | 5,520 |
| DNB Lean | 1,995 | 620 / 525 / 850 | 42.2% vs 43.2% | 2.91 | −4.2% (−9.4% to +1.0%) | −4.2% (−4.8% to −3.7%) | −3.2% | 2,062 |
| 1X2 Lean (all odds) | 2,627 | 1,068 / 0 / 1,559 | 40.7% vs 41.4% | 2.95 | −2.3% (−7.4% to +3.0%) | −3.1% (−3.5% to −2.7%) | −3.1% | 2,971 |

- **Neither DNB rule passes.** Both CLV ranges sit well below 0, in every league and every season
  (DNB 12% −4.8% to −8.0% by season; DNB Lean −3.3% to −5.5%).
- **Per dollar, DNB loses to the close about as much as 1X2** (−4.8% vs −4.6%; −3.2% vs −3.1%).
  Taking the draw out removes the model's draw errors but none of its home-vs-away error, which
  is where its edge claims come from.
- **The hit rate rises less than hoped:** 31% vs 27% for the 12% rule. The rule still finds its
  biggest claimed edges on underdogs, and an underdog's DNB price stays long (average 3.90; 49%
  of DNB 12% bets are at 3.00 or more). DNB Lean's hit rate is 42% vs 41%.
- **Noise:** about a third fewer bets are needed before a 5% edge shows (3,737 vs 5,520; 2,062 vs
  2,971), not the fourfold gain a 2.20 price would give. That is a real but modest monitoring gain.
- **The model overrates its DNB picks as it does its 1X2 picks:** DNB 12%'s bets said 43.6%,
  the price implied 34.2%, 31.3% won (slope 0.81, 0.74 to 0.89). On every match, the model's DNB
  chance is roughly calibrated in the top five leagues (slopes 0.92–1.19) but over-confident in
  the Championship and League Two (0.69, 0.59).
- **Odds bands:** DNB 12% CLV runs from −4.1% at 1.50 or shorter to −9.0% above 5.00; DNB Lean
  −3.0% to −5.0%. No band is above 0.
- **AH 0 check:** where Pinnacle's line is 0 (11–19% of matches, close games), the synthetic DNB's
  margin is 3.9–5.0% against 2.1–3.2% for the real AH 0, and their fair chances agree (mean gap
  0.001). Decided and paid at the real AH-0 price, both rules still lose to the close: DNB 12%
  1,001 bets, CLV −2.8% (−3.4% to −2.3%), ROI −3.3%; DNB Lean 436 bets, CLV −2.8% (−3.6% to −2.0%),
  ROI −2.0%. The cheaper real market narrows the loss (and these are short-priced, close
  matches), but doesn't turn it positive.

## Verdict (draw no bet)

**No DNB track: it doesn't pass, and it isn't a better way to run the same picks.** DNB changes
the bet, not the model's mistake. The model's edge claims come from rating one side above the
other more than the price does, and DNB keeps exactly that, so it loses to Pinnacle's close as
much per dollar as the 1X2 rules. The synthetic DNB we could build from DraftKings' h2h also pays
about 1.7 points more margin than a real DNB market. The noise falls by about a third, not enough
to justify a new track by itself. Nothing changes live. No handover.

### Phase 2, step 2: design B pre-registration (owner-approved 24 Oct, hard cap 1,920 credits; registered 2026-10-24 before any fetch code is written or run)

The owner approved design B at a hard cap of 1,920 credits. The +30-credit lower-league check
was not approved and is not part of this test. Everything below is frozen before any paid
call. Nothing here changes anything live: **a pass only justifies proposing design C to the
owner.**

**Sample (frozen).**
- The file `src/soccer_stats/lab/comeback_b_slots.json` (SHA-256 starting `893afb5fce500de6`),
  written by `lab.comeback_plan.slot_list` from football-data's 2024/25 files.
- Contents: 192 kickoff slots holding 210 trailing favourites in the top five leagues, 2024/25:
  - E0: 41 slots, 50 matches;
  - SP1: 36 slots, 36 matches;
  - D1: 35 slots, 41 matches;
  - I1: 41 slots, 42 matches;
  - F1: 39 slots, 41 matches.
- A trailing favourite is phase 1's: the side with the larger Shin-fair chance at Pinnacle's
  close, at least 45%, behind at half-time by football-data's HTHG/HTAG, by any margin.
- The file holds half-time scores only, nothing about how a match ended. 2025/26 is not
  touched.

**Purchase.**
- One historical bulk h2h snapshot per slot, requested at the file's `request_at`, which is
  kickoff + 55 minutes.
- Ten named books, which count as one region and cost about 10 credits a snapshot: pinnacle,
  draftkings, betfair_ex_eu, betfair_ex_uk, fanduel, betmgm, unibet_eu, sport888, bovada and
  paddypower.
- Spending rules:
  - every call goes through `CappedBudget` with cap 1,920 and keeps the 3,000-credit reserve
    on the freshest balance;
  - the first call is budgeted at 20 credits, and the run stops if any call costs more than 10;
  - an HTTP or network error stops every call;
  - a slot whose body is cached, or whose call record shows an answered call, is never bought
    again.
- A dry run (cap 0) comes first. The fetch never reads match outcomes.

**Matching a snapshot to a match.**
- The snapshot's timestamp must fall 48–62 minutes after the slot's kickoff (the half-time
  break); otherwise the whole slot is unusable.
- An event is a match when both Odds API team names map to the football-data names, via
  `odds_feed._team` against that league's 2024/25 teams, and its `commence_time` is within 30
  minutes of football-data's kickoff.
- Unmatched trailing favourites are counted and dropped.

**Quote check (per book, per match).** A book's h2h quote passes when all of these hold:
1. It has all three prices.
2. Its `last_update` (the market's, else the book's) is after the event's `commence_time`.
3. It was updated at most 5 minutes before the snapshot's timestamp.
4. Its margin-normalised chance for the trailing favourite is below that team's fair pre-match
   chance at Pinnacle's close. The price must have moved against the side that is behind.

Pinnacle has one more check. Its normalised chance for the trailing favourite must be within
**0.05** (absolute) of the median of the other passing books' normalised chances, and at least
two other books must pass. A Pinnacle quote failing this isn't used.

**Primary test: bet the trailing favourite in play at DraftKings.**
- A usable match is one whose DraftKings quote passes the quote check.
- $1 flat on the favourite to win at DraftKings' in-play decimal price. A win pays odds − 1;
  a draw or a loss costs 1. The final result is football-data's FTHG/FTAG.
- ROI is the mean profit per match. Its 95% range comes from resampling matches (percentile,
  2,000 draws, seed 0).
- **Pass:** at least 100 usable matches and the ROI range entirely above 0.
- **Fail:** fewer than 100 usable matches ("inconclusive"), or a range that reaches 0. A range
  entirely below 0 is reported as "the in-play price over-prices the comeback".

**Second line (descriptive, not part of the verdict).** The same bet at Betfair's exchange back
price, `betfair_ex_eu` then `betfair_ex_uk`, whichever passes the quote check first. A win pays
(odds − 1) × 0.98, i.e. after a 2% commission on winnings. ROI with the same range.

**Secondary (descriptive).**
1. **Realized vs market-implied win rate.**
   - The market's fair chance is Pinnacle's Shin-fair chance when Pinnacle passes both
     checks. Otherwise it is Betfair's normalised chance when Betfair passes. Otherwise there
     is none, and the match is counted out of this measure.
   - Report realized − mean fair, with the same resampled range, overall and by phase 1's four
     bands.
2. **Does the phase-1 model add anything beyond the in-play price?**
   - The model is phase 1's time-scaled Poisson: pre-match means from Pinnacle's closing 1X2
     and over/under 2.5 (`team_totals.implied_means`), conditioned on the half-time score.
     45 minutes are left, with the second half's measured 55.5% share of goals (remaining
     means λ × 0.555).
   - Fit a logistic regression: logit P(win) = a + b·logit(market fair) + c·logit(model).
   - Report c with a resampled 95% range. "Adds information" means c's range is entirely
     above 0.

**Running it.**
- After the fetch, the analysis (`task=comebacks2`, no key) reads only the cached bodies, the
  frozen slot list and football-data. It is run once.
- Any bug found afterwards is fixed, rerun and disclosed, with both runs named, as in phase 1.
- Every count is reported: slots bought, unusable slots, unmatched matches, and quotes failing
  each check by book.

### Phase 2, step 2: result (2026-10-25; run once)

**Runs.**
- Fetch: run 40144988040, from the default branch at cap 1,920. It bought 192 of 192
  snapshots at exactly 10 credits each, **1,920 credits** (19,083 → 17,163), with no stop.
  The bodies are in the cache `comeback-odds-40144988040` and that run's 7-day artifact.
- Analysis: run 40145295663, `task=comebacks2`, no key, run once.

**Counts.**
- Every slot's snapshot fell inside the half-time window: 0 missing, 0 off half-time.
- 12 of the 210 trailing favourites had no matching event, which leaves **198 usable
  matches**.
- DraftKings and Betfair passed the quote check on all 198.
- Pinnacle failed it 91 times: 66 off the other books' median by more than 0.05, 20 stale,
  4 not quoted, 1 not moved. Its fair price was used on 107 matches and Betfair's on the other
  91.
- Other books' failures: FanDuel stale 13; Bovada stale 3 and not moved 7; Paddy Power stale 1.

**Primary: $1 on the trailing favourite at DraftKings' in-play price.**
- 198 bets, 55 won (27.8%), average odds 5.48 (break-even 25.0%).
- **ROI +7.3% (95%: −19.2% to +34.3%)**.

**Verdict (pre-registered rule): fail, because the ROI range includes 0.** Design B does not
justify design C. Nothing changes live.

**Second line (descriptive): Betfair's exchange back price after 2% commission.** 198 bets,
average odds 6.17, ROI +14.9% (−13.7% to +45.5%). This range also includes 0.

**Secondary (descriptive).**
- **Realized vs market-implied:** won 27.8% against the market's fair 24.5%, a gap of
  **+3.3 points (−2.7 to +9.2)**. It points the same way as phase 1's gap (+3.8 points
  against the corrected model), but the range includes 0.
- **Blend weight of the phase-1 model beside the in-play price:** c = 0.30 (−0.58 to +1.19)
  on 196 matches, with b = 0.67 on the market. The model adds nothing detectable beyond the
  price.

**By band (descriptive only, not a test).** Four bands of a fail, none a pass criterion; a
band can't be picked after the fact.

| Favourite | Bets | Won | Avg odds | DraftKings ROI (95%) | Realized − implied |
| --- | --- | --- | --- | --- | --- |
| 45–55% | 99 | 25 | 6.72 | +29.9% (−15.4% to +78.2%) | +7.2 pts (−0.9 to +15.7) |
| 55–65% | 45 | 10 | 4.87 | −25.6% (−67.6% to +18.5%) | −3.3 pts (−14.8 to +9.2) |
| 65–75% | 37 | 12 | 4.22 | −12.5% (−53.2% to +28.4%) | −0.3 pts (−14.5 to +13.8) |
| 75%+ | 17 | 8 | 2.63 | +5.7% (−49.8% to +62.8%) | +6.2 pts (−16.8 to +28.8) |

**Reading it.**
- In 2024/25 these favourites won 27.8% of the time from behind at half-time, above phase 1's
  long-run 19.5% (eight leagues, 2017/18–2024/25). One season of 198 matches carries a range
  of about ±6 points, and the in-play market priced them at 24.5%.
- The market didn't leave a gap this test could find: the point estimates are positive, and
  every range includes 0.
- Detecting a gap of about 3 points needs the roughly 870 matches of design C (9,010
  credits).
- 45–55% favourites look best, but that is the kind of after-the-fact cut the pre-registration
  rules out. Testing it would need a new pre-registration on fresh data (another season, or
  design C's other seasons), judged on that band alone.
- Whether to spend more is the owner's call. This test spent its full 1,920 credits and stops
  here.

### Phase 2, step 3: design C pre-registration (owner-approved 25 Oct, budget 9,010 credits; registered 2026-10-25 before any design-C code is written or run)

The owner approved design C (eight leagues, 2022/23–2024/25), with a total budget of 9,010
credits, including the 1,920 already spent on B. This is a **confirmatory test on prices not
yet seen**. Design B's rules carry over unchanged. A pass only justifies proposing a live paper
test to the owner; nothing here changes anything live.

**Sample (frozen).**
- The file `src/soccer_stats/lab/comeback_c_slots.json` (SHA-256 starting `b6210148a963dc84`)
  holds design C's slots minus every slot bought in design B (`comeback_b_slots.json`): **709
  slots holding 902 trailing favourites**, written by `comeback_plan.slot_list` and sorted by
  request time.
- B's 198 usable matches have been looked at, so they are out of the judged sample. They
  appear only as a descriptive line and in a descriptive pooled C+B line.

| League | 2022/23 slots, matches | 2023/24 | 2024/25 |
| --- | --- | --- | --- |
| E0 | 46, 51 | 37, 44 | in B |
| SP1 | 34, 35 | 44, 44 | in B |
| D1 | 23, 28 | 29, 33 | in B |
| I1 | 35, 36 | 34, 34 | in B |
| F1 | 42, 45 | 31, 32 | in B |
| E1 | 37, 46 | 37, 57 | 43, 57 |
| E2 | 36, 50 | 40, 59 | 41, 69 |
| E3 | 36, 52 | 42, 70 | 42, 60 |

- Disclosure: phase 1 used these seasons' half-time and full-time scores (its development
  window was 2017/18–2024/25), so the aggregate comeback rates of these matches were seen
  then. Their in-play prices, which this test is about, have never been seen.

**Power.**
- With all 902 matches usable, the test detects a gap of **3.7 points** (realized minus
  implied, two-sided 5%, 80% power, at a win rate near 20%).
- At B's usable rate (94%, about 850 matches) it detects 3.8 points, about the ~870 matches
  needed for phase 1's corrected 3.8-point gap. That is just short.
- If the lower leagues (E1–E3) turn out unpriced, only the top five's 382 matches remain.
  That detects 5.7 points, under-powered for a 3.8-point gap. The pass rule stays the same
  either way.

**Purchase.**
- Same as B: one historical bulk h2h snapshot per slot at `request_at` (kickoff + 55 min), at
  the same ten books (one region, about 10 credits). Expected cost **7,090 credits** (709 × 10).
- Same spending rules: `CappedBudget` with the 3,000 reserve; the first call budgeted at 20;
  stop on any call over 10 or on any HTTP/network error; never buy a slot twice; dry run first.
- Snapshots before late 2022 may be 10 minutes apart. A slot whose returned snapshot falls
  outside the 48–62-minute window is counted as off half-time and never re-bought.
- **Lower-league guard (E1, E2, E3).** Slots are bought in time order. A guarded league's
  snapshot is "priced" when at least one event in it had started and has a DraftKings or
  Betfair (`betfair_ex_eu`/`betfair_ex_uk`) h2h quote updated after kickoff and at most 5
  minutes before the snapshot.
  - If a league's first 3 bought snapshots include none that is priced, the fetch buys no more
    of that league and records it in `guard.json`.
  - That league's remaining matches count as **not priced**: excluded and reported, not
    failures.
  - The guard never applies to the top five.

**Analysis.**
- Same quote check, Pinnacle median tolerance 0.05 with the Betfair fallback, 48–62-minute
  window and event matching as B.
- Matches are joined to football-data by league, season, home and away.
- **Primary:** $1 on the trailing favourite at DraftKings' passing in-play price, ROI with a
  match-resampled 95% range (2,000 draws, seed 0), on **C minus B only**.
  - **Pass:** at least 100 usable matches and the range entirely above 0.
  - **Fail:** otherwise. A range entirely below 0 reads "the in-play price over-prices the
    comeback".
- Descriptive, none of it part of the verdict:
  - Betfair after 2%;
  - realized minus implied, overall and by band;
  - the phase-1 model's blend weight c;
  - per-league and per-season tables;
  - B alone;
  - C+B pooled.
- Run once. Any bug found afterwards is fixed, rerun and disclosed with both runs named.

**Amendment, 2026-10-25, before any design-C purchase (lead review of PR #110).**
- The empty-data guard applies to **every league-season**, not only to E1–E3. Archived in-play
  prices are verified only from 2024/25 on (design B and the two probes), and about two-thirds
  of C is the top five's 2022/23 and 2023/24.
- The rule is otherwise the same. If a league-season's first 3 bought snapshots, oldest first,
  include none with a started event freshly priced by DraftKings or Betfair, no more of that
  league-season is bought, and its matches count as **not priced** (excluded and reported).
- This changes what is bought, not how a match is judged.
- If whole seasons turn out unpriced, the judged sample shrinks below the power figures above;
  the pass rule stays the same.

### Phase 2, step 3: result (2026-10-25; run once)

**Runs.**
- Fetch: run 40176031079, from the default branch at cap 7,090. It bought **607 snapshots**
  at exactly 10 credits each, **6,070 credits** (17,136 → 11,066), with no stop. The bodies are
  in the cache `comeback-odds-40176031079` and that run's 7-day artifact.
- Analysis: run 40176653428, `task=comebacks2` with `design=C`, no key, run once.
- Comeback spend in all: 1,920 (B) + 6,070 (C) = 7,990 of the approved 9,010, plus the two
  10-credit probes.

**The guard (amendment of 2026-10-25).** Three league-seasons showed no started event freshly
priced by DraftKings or Betfair in their first 3 snapshots, so no more of them was bought:

| League-season | Slots in C | Bought (unpriced) | Slots skipped |
| --- | --- | --- | --- |
| E0 2022/23 | 46 | 3 | 43 |
| F1 2022/23 | 42 | 3 | 39 |
| D1 2022/23 | 23 | 3 | 20 |

- 102 slots holding **115 matches** count as not priced (excluded and reported). The guard
  cost 90 credits on the 9 unpriced snapshots and saved about 1,020.
- Every other league-season was priced, including SP1 and I1 2022/23 and all three lower
  leagues in every season.

**Counts.**
- 709 slots: 0 missing, 0 off half-time, 102 not priced (115 matches).
- 902 trailing favourites − 115 not priced − 27 with no matching event = **760 usable rows**.
- Quote-check failures by book: DraftKings 28 (25 stale, 3 not quoted); Pinnacle 234 (152 off
  the other books' median by more than 0.05, 57 stale, 24 not quoted, 1 not moved); Betfair UK
  5 (2 stale, 3 not moved); Betfair EU 2 (not moved); FanDuel 36, BetMGM 27, Paddy Power 25,
  Bovada 19 stale (Bovada also 3 not moved).
- Pinnacle's fair price was used on 526 matches and Betfair's on 174 (700 with a fair price).

**Primary: $1 on the trailing favourite at DraftKings' in-play price, C minus B.**
- 732 bets, 123 won (16.8%), average odds 6.71 (break-even 20.9%).
- **ROI −25.1% (95%: −38.3% to −10.2%)**.

**Verdict (pre-registered rule): fail. The range is entirely below 0, so it reads "the in-play
price over-prices the comeback".** Nothing changes live. The comeback idea is closed: there
is no edge in backing the trailing favourite at the half-time price.

**Second line (descriptive): Betfair's exchange back price after 2% commission.** 602 bets,
109 won, average odds 8.22, ROI −18.1% (−34.6% to −0.1%).

**Secondary (descriptive).**
- **Realized vs market-implied:** won 17.0% against the market's fair 21.3% on 700 matches,
  a gap of **−4.3 points (−6.9 to −1.6)**. Phase 1's gap (+3.8 points) was against a model
  with no game state. The in-play market prices comebacks well above that model, and above
  what happened.
- **Blend weight of the phase-1 model beside the in-play price:** c = 0.54 (−0.05 to +1.23)
  on 698 matches (a 0.14, b 0.64). The range includes 0.

**By band (descriptive only, not a test).**

| Favourite | Bets | Won | Avg odds | Break-even | DraftKings ROI (95%) | Realized − implied (matches) |
| --- | --- | --- | --- | --- | --- | --- |
| 45–55% | 423 | 55 | 7.97 | 16.6% | −29.0% (−47.9% to −9.2%) | −3.6 pts (−6.7 to −0.2; 406) |
| 55–65% | 216 | 41 | 5.69 | 23.2% | −17.0% (−42.1% to +10.5%) | −4.4 pts (−9.8 to +0.7; 204) |
| 65–75% | 71 | 13 | 3.69 | 31.7% | −49.8% (−73.7% to −23.2%) | −15.8 pts (−23.5 to −7.0; 68) |
| 75%+ | 22 | 14 | 2.32 | 45.7% | +51.3% (−3.3% to +113.7%) | +17.9 pts (−2.8 to +37.9; 22) |

**By league (descriptive only).**

| League | Bets | Won | Win rate | Break-even | Avg odds | DraftKings ROI (95%) |
| --- | --- | --- | --- | --- | --- | --- |
| E0 | 44 | 17 | 38.6% | 30.1% | 4.78 | +49.2% (−20.7% to +139.2%) |
| SP1 | 77 | 20 | 26.0% | 24.3% | 5.57 | +9.4% (−36.7% to +65.2%) |
| D1 | 33 | 8 | 24.2% | 22.6% | 6.62 | −13.8% (−65.3% to +45.8%) |
| I1 | 57 | 9 | 15.8% | 20.6% | 6.86 | −39.0% (−74.0% to +3.4%) |
| F1 | 32 | 1 | 3.1% | 21.7% | 6.55 | −87.5% (−100.0% to −62.5%) |
| E1 | 144 | 26 | 18.1% | 20.7% | 6.55 | −19.5% (−46.8% to +11.9%) |
| E2 | 171 | 25 | 14.6% | 20.0% | 6.70 | −22.5% (−52.9% to +10.6%) |
| E3 | 174 | 17 | 9.8% | 17.8% | 7.85 | −52.3% (−72.9% to −28.5%) |

- The top five (243 bets) come to −10.7% and the three lower leagues (489 bets) to −32.2%,
  bet-weighted from the rows above (no ranges).
- The top five's C bets are 2022/23 and 2023/24 only, minus the three guarded league-seasons.

**By season (descriptive only).**

| Season | Bets | Won | Win rate | Break-even | DraftKings ROI (95%) |
| --- | --- | --- | --- | --- | --- |
| 2022/23 | 186 | 23 | 12.4% | 19.4% | −35.9% (−61.1% to −7.2%) |
| 2023/24 | 364 | 69 | 19.0% | 22.4% | −26.3% (−43.4% to −7.7%) |
| 2024/25 | 182 | 31 | 17.0% | 19.5% | −11.5% (−42.2% to +20.2%) |

(2024/25 in C is the lower leagues only; the top five's 2024/25 is design B.)

**B alone and C+B pooled (descriptive).**
- B alone (2024/25, top five): 198 bets, 55 won, ROI +7.3% (−19.2% to +34.3%), as reported in
  step 2.
- C+B pooled: 930 bets, 178 won (19.1%, break-even 21.8%), ROI **−18.2% (−30.0% to −5.1%)**.
  - Betfair after 2%: 800 bets, −10.0% (−24.6% to +5.3%).
  - Realized − implied: −2.6 points (−5.2 to −0.1) on 898 matches.
  - c = 0.53 (+0.02 to +1.10) on 894 matches.
  - Pooled bands: 45–55% −17.8% (522 bets); 55–65% −18.5% (261); 65–75% −37.0% (108); 75%+
    +31.4% (39).

**Reading it.**
- Over the 732 judged bets, trailing favourites won 16.8% of the time. DraftKings priced them
  at a break-even of 20.9% and the margin-free market at 21.3%. Backing them lost a quarter of
  the stake.
- Design B's positive season was not a pattern. Its best-looking band (45–55%, +29.9% in B)
  is −29.0% in C, the after-the-fact cut the pre-registration ruled out.
- The model's blend weight c is positive in both C (0.54) and the pooled data, with the pooled
  range just above 0. That is descriptive and it does not make a bet: the price and the model
  together still sit above what happened.
- The losses are biggest in the lower leagues (E3 −52.3%) and in France (1 win in 32), and
  the only positive bands are tiny (75%+, 22 bets). None of these are tests.
- Any new idea from these tables, laying the trailing favourite included, needs its own
  pre-registration on seasons not used here (2025/26 or later), and nothing is bought without
  the owner. The ESPN goal-timeline study (written up in its own section) stays descriptive.

## Goal timelines from ESPN (owner, 25 Oct; exploratory, free; method written before any data is fetched)

**Why.** Phase 1 only saw the score at half-time (football-data) and the first goal in the five
Understat leagues. The owner wants to know when in a match a favourite trails and how often it
comes back from each scoreline and minute. The headline is **trail first**: the underdog
scores the opening goal at any minute, including the matches where the favourite equalises
before half-time and so never appears in a half-time snapshot.

**Order rule.** Design C's pre-registration (commit dca429e) landed before any timeline was
fetched. Nothing here changes C's frozen rules or re-reads C. Everything is exploratory: a
minute or band that looks good becomes a **new** pre-registration on fresh seasons.

**Data.** `lab/timelines.py`.
- **Fetch** (`odds-check.yml` `task=timelines-fetch`, no key, counts only): ESPN's site
  scoreboard, one call per league and date, only on football-data's match days. It covers the
  eight leagues in `espn_news.SLUGS` (eng.1–eng.4, esp.1, ger.1, ita.1, fra.1), 2017/18 to
  2024/25, with 2025/26 dropped first.
  - Each day is reduced to the events' teams, ids, status, score and scoring plays (clock and
    team), and cached for good in `espn-timelines-*`.
  - At most 2 requests a second, retries on 5xx and network errors only, and a time budget
    per run. It resumes from the cache.
- **Join:** by league, season, both names (`espn_news.team_name`) and date, a day either
  side. The 90-minute and half-time scores rebuilt from ESPN's goals must equal
  football-data's, or the match is dropped and counted. Match rates are reported per league
  and season.
- **Favourites and fair means:** as in phase 1, from Pinnacle's closing 1X2 and over/under
  2.5. The benchmark is phase 1's time-scaled Poisson with a uniform scoring rate, which
  under-counts second-half goals (phase 1's exploratory check), so residuals lean positive.

**What it reports**, with match-resampled 95% ranges throughout:
1. **Trail first.** Realized win/draw/loss against the fair model from the opening goal's
   minute. Minute bands are 0–15, 15–30, 30–45 (first-half stoppage time included), 45–60,
   60–75 and 75+, reported by favourite band and by league.
2. **What happens next.** For trail-first matches where the first goal came before
   half-time: the share still behind, level or ahead at half-time, and how each group ends.
3. **Behind at a given minute.** At 15, 30, 45, 60 and 75: how often the favourite is behind,
   and the win/draw rate one down and two or more down against the model, by band.
4. **The timing curve of trail first per league, E1–E3 included** (Understat lacked them).
5. **Cost of a priced trail-first test.** One historical snapshot about 5 minutes after each
   opening goal. The wall clock is kickoff + minute, plus 17 minutes for a second-half goal.
   Goals in one league and 5-minute bucket share a snapshot. Counts are per league and season
   for 2022/23–2024/25, beside the half-time design's slots.

### Goal timelines: results (2026-10-25; exploratory, 0 credits)

**Runs.**
- Fetch: run 40176002361 (`task=timelines-fetch`). 6,636 scoreboard requests, 0 failed, no stop
  on the time budget. Every football-data match day for the eight leagues, 2017/18–2024/25, is
  in the cache `espn-timelines-40176002361`.
- Analysis: run 40184239564 (`task=timelines`). It is descriptive, like everything below.

**Join.**
- 16,109 favourites (Pinnacle's close ≥ 45%). **15,838 joined (98.3%)**.
- 30 were dropped because ESPN's goals didn't rebuild football-data's 90-minute or half-time
  score: E1–E3 22, D1 4, SP1 2, F1 2.
- 241 had no ESPN event, all from club names the join does not map yet:
  - D1 2023/24 50 and 2024/25 47 (two or three promoted clubs);
  - F1 16–23 in every season but 2022/23 and 2023/24 (one club);
  - I1 2017/18 21.
  These are whole clubs missing, mostly small promoted sides. The by-league lines for D1 and F1
  lean slightly towards the other clubs. Mapping them is a free fix for a later run.
- The fair-model benchmark needs Pinnacle's close and O/U 2.5 in a season with ≥ 90%
  coverage (phase 1's rule). 3,789 of the 4,980 trail-first matches are benchmarked.

**Reading the residuals.** Every residual is realized minus phase 1's time-scaled Poisson model.
That model spreads goals evenly over 90 minutes and knows nothing about the score. Phase 1
showed it under-counts second-half goals, so a positive residual is expected. Design C then
showed the in-play market already prices comebacks above this model, and above what happened.
A positive residual here is therefore **not** an edge against any price.

**1. Trail first (the headline).** The underdog scored the opening goal in **4,980 of 15,838
matches (31.4%)**.

| First goal | Matches (benchmarked) | Fav. won | Drew | Model's win | Win − model (95%) | Avoided defeat − model |
| --- | --- | --- | --- | --- | --- | --- |
| 0–15 | 1,709 (1,345) | 34.4% | 26.6% | 30.8% | +3.4 (+1.0 to +5.9) | +6.0 (+3.5 to +8.6) |
| 15–30 | 1,215 (907) | 29.6% | 27.6% | 25.0% | +4.9 (+1.8 to +7.6) | +4.9 (+1.7 to +8.1) |
| 30–45 | 923 (678) | 24.5% | 27.5% | 19.1% | +6.1 (+2.9 to +9.2) | +4.7 (+1.1 to +8.6) |
| 45–60 | 545 (402) | 16.3% | 30.3% | 12.4% | +3.8 (+0.4 to +7.4) | +7.6 (+2.5 to +12.4) |
| 60–75 | 334 (258) | 8.4% | 23.1% | 5.7% | +3.3 (−0.1 to +6.8) | +4.0 (−1.5 to +10.1) |
| 75+ | 254 (199) | 0.4% | 16.1% | 1.0% | −0.4 (−1.1 to +0.7) | +6.1 (+1.1 to +11.1) |
| All | 4,980 (3,789) | 25.9% | 26.6% | 22.1% | **+4.1 (+2.8 to +5.4)** | +5.6 (+4.0 to +7.1) |

- The gap against the model is about the same size from the 1st minute to the 75th, +3 to +6
  points. There is no minute where it opens up. That looks like the benchmark's known
  shortfall, not a timing pocket.

| Favourite (pre-match) | Trail first | Fav. won | Model's win | Win − model (95%) |
| --- | --- | --- | --- | --- |
| 45–55% | 2,774 | 20.5% | 17.3% | +3.7 (+2.1 to +5.4) |
| 55–65% | 1,384 | 27.7% | 23.5% | +5.3 (+2.6 to +7.9) |
| 65–75% | 544 | 36.2% | 32.7% | +2.6 (−1.9 to +7.2) |
| 75%+ | 278 | 51.1% | 44.6% | +4.6 (−2.2 to +11.5) |

| League | Trail first | Fav. won | Model's win | Win − model (95%) |
| --- | --- | --- | --- | --- |
| E0 | 653 | 32.3% | 25.8% | +7.5 (+3.9 to +11.5) |
| I1 | 579 | 32.1% | 24.0% | +7.1 (+2.9 to +11.3) |
| SP1 | 571 | 26.4% | 21.9% | +5.8 (+1.9 to +9.9) |
| F1 | 513 | 25.9% | 22.4% | +3.2 (−1.0 to +7.3) |
| D1 | 462 | 26.4% | 26.6% | −1.6 (−5.8 to +2.8) |
| E1 | 767 | 22.9% | 19.6% | +3.6 (+0.4 to +7.1) |
| E2 | 767 | 24.6% | 20.2% | +5.9 (+2.5 to +9.1) |
| E3 | 668 | 18.6% | 18.5% | −0.5 (−3.8 to +2.8) |

**2. What happens next (first goal before half-time, 3,847 matches).**

| At half-time | Share | Fav. won (95%) | Drew | Lost |
| --- | --- | --- | --- | --- |
| Still behind | 70.1% (2,697) | 19.2% (17.8 to 20.7) | 25.9% | 54.9% |
| Level | 22.1% (850) | 48.4% (45.2 to 51.6) | 35.5% | 16.1% |
| Ahead | 7.8% (300) | 81.3% (77.0 to 85.7) | 14.7% | 4.0% |

- A favourite that falls behind before half-time is still behind at the break 70% of the
  time. The 30% that are level or ahead by then never appear in a half-time snapshot. Those are
  the matches the trail-first framing adds.

**3. Behind at a given minute** (share of all 15,838 matches where the favourite is behind).

| Minute | Behind | One down: won, model, win − model (95%) | Two+ down: won, model, win − model (95%) |
| --- | --- | --- | --- |
| 15' | 9.9% | 32.7%, 28.4%, +4.2 (+1.7 to +6.9) (1,485) | 14.1%, 9.6%, +4.1 (−3.7 to +13.3) (78) |
| 30' | 15.0% | 28.2%, 22.4%, +5.3 (+3.1 to +7.4) (2,098) | 11.8%, 7.5%, +6.8 (+2.4 to +11.8) (272) |
| 45' | 17.9% | 22.2%, 16.2%, +5.9 (+4.0 to +7.8) (2,314) | 7.1%, 3.9%, +5.5 (+2.7 to +8.5) (520) |
| 60' | 18.8% | 13.8%, 9.4%, +4.5 (+2.8 to +6.1) (2,247) | 3.9%, 1.5%, +2.9 (+1.3 to +4.9) (725) |
| 75' | 18.8% | 6.9%, 3.1%, +4.1 (+2.8 to +5.3) (2,097) | 0.5%, 0.2%, +0.2 (−0.2 to +0.8) (886) |

- One goal down, the favourite's win rate drops from 33% at 15' to 7% at 75'. By favourite
  band, one down at 45': 18.1% (45–55%), 23.4% (55–65%), 30.5% (65–75%), 41.2% (75%+).
- One down at 45' (22.2% won, model 16.2%) is close to phase 1's half-time result (19.7% vs
  14.0%, any deficit), now from ESPN's timelines. On design C's priced half-time matches the market said 21.3% and 17.0% happened.

**4. Timing of trail first, E1–E3 included.** The underdog scores first in 28–34% of
favourites' matches in every league: I1 28.5%, SP1 29.5%, D1 29.9%, E0 30.9%, F1 31.2%, E2
33.0%, E1 33.8%, E3 33.9%. The median opening-goal minute is 22–27, and 32–41% of trail-first goals
(34% pooled) come in the first 15 minutes. E1–E3 have slightly more trail-first matches, and E1
and E3 slightly later first goals (medians 27 and 26, against 22–25 in the top five).

**5. Cost of a priced trail-first test** (one snapshot about 5 minutes after each opening goal,
shared within a league's 5-minute bucket; eight leagues, 2022/23–2024/25).
- **1,942 trail-first matches need 1,882 snapshots, about 18,820 credits.** The half-time design
  needed 901 slots for B and C together. Opening goals rarely share a 5-minute bucket, so almost
  every match is its own snapshot.
- The top five alone: 1,046 matches, 1,036 snapshots, about 10,360 credits.
- Per league-season: 45–118 matches, 450–1,110 credits.
- The balance is 11,066 credits.

**What this says.**
- Favourites that fall behind come back more often than an even-rate, score-blind model says,
  at every minute and in most leagues, by a fairly constant 3–6 points. That is consistent with
  phase 1, and with the market already knowing it: at half-time the market priced these
  comebacks above both the model and the outcome (design C).
- There is no minute band where the gap grows. That would have been the case for buying
  earlier-in-the-match prices.
- **Recommendation: don't buy a trail-first price test.** It would cost about twice design C,
  more than the current balance, to test a gap that the half-time prices already showed the
  market covers.
- If the owner still wants it, it would need a new pre-registration on seasons not used here
  (2025/26 onward) and the owner's approval. Nothing here is a test.

## Heavy favourites (75%+) trailing at half-time: free scope and DRAFT pre-registration (owner via the lead, 26 Oct)

**Why this exists.** In design C's 75%+ band, 22 bets won 14 (64%, against the market's
46%), ROI +51% (−3% to +114%). In B's 75%+ band, 17 bets won 8 (47% vs 41%), +5.7%. Pooled,
39 bets won 22, +31% (−5% to +73%). The band was picked **after** the results were seen, out
of four bands, on 39 bets, so it is a new hypothesis. It can only be tested on data that
neither B nor C used. Nothing here is a test, and nothing is bought.

**Scope run.** `lab/heavy_favourites.py` (`odds-check.yml` `task=heavy-favourites`; run
40289687098, football-data only, no key, 0 credits).
- **Favourite:** the Shin-fair pre-match chance from Pinnacle's close, else the market
  average's close (`AvgCH/D/A`).
- **Trailing:** behind at half-time on football-data's score.
- **Slots:** one league's trailing heavy favourites with the same kickoff (UK `Time` →
  UTC), one historical snapshot each at kickoff + 55 min, 10 credits.

**1a. The eight live leagues, 2025/26 and 2026/27 to date** (data B and C never used).

| League | Heavy favourites, 25/26 + 26/27 | Trailing at half-time | Slots | Credits |
| --- | --- | --- | --- | --- |
| E0 | 16 + 4 | 0 + 1 | 1 | 10 |
| SP1 | 30 + 12 | 2 + 0 | 2 | 20 |
| D1 | 25 + 7 | 1 + 1 | 2 | 20 |
| I1 | 17 + 6 | 3 + 0 | 3 | 30 |
| F1 | 24 + 2 | 2 + 0 | 2 | 20 |
| E1 | 13 + 0 | 1 + 0 | 1 | 10 |
| E2 | 1 + 1 | 0 | 0 | 0 |
| E3 | 0 | 0 | 0 | 0 |
| **Total** | **158** | **11** (10 one goal down) | **11** | **110** |

- Pinnacle's close is in football-data's 2025/26 files for only 30–55% of matches per
  league, and in none of 2026/27 so far. So 95 of the 158 favourites, and 4 of the 11
  trailing, are priced from the market average's close.

**1b. Other football-data leagues with an Odds API sport key, 2022/23–2025/26.**

| League (sport key, to confirm free) | Heavy favourites | Trailing | Slots | Credits | By season, 22/23 · 23/24 · 24/25 · 25/26 |
| --- | --- | --- | --- | --- | --- |
| N1 Eredivisie (`soccer_netherlands_eredivisie`) | 191 | 20 | 20 | 200 | 6 · 7 · 5 · 2 |
| SC0 Scottish Premiership (`soccer_spl`) | 158 | 16 | 16 | 160 | 6 · 5 · 4 · 1 |
| P1 Primeira Liga (`soccer_portugal_primeira_liga`) | 225 | 15 | 15 | 150 | 6 · 4 · 3 · 2 |
| G1 Greek Super League (`soccer_greece_super_league`) | 146 | 12 | 12 | 120 | 3 · 5 · 3 · 1 |
| T1 Turkish Super Lig (`soccer_turkey_super_league`) | 99 | 11 | 11 | 110 | 1 · 4 · 3 · 3 |
| B1 Belgian Pro League (`soccer_belgium_first_div`) | 51 | 2 | 2 | 20 | 0 · 2 · 0 · 0 |
| D2, I2, SP2, F2 (second tiers) | 21 | 2 | 2 | 20 | 0 · 1 · 1 · 0 |
| **Total** | **891** | **78** (70 one down) | **78** | **780** | 22 · 28 · 19 · 9 |

- Pinnacle's close prices every match through 2024/25. In 2025/26 the average's close fills
  in, for 97 of the 891 favourites and 3 of the 78 trailing.
- T1 2022/23 has 29 matches with no price at all.
- football-data's "extra" leagues (Austria, Switzerland, the Nordics and others) carry no
  half-time score, so they can't be counted for free and are left out.
- Archive coverage is the real unknown:
  - C's guard found the top five's 2022/23 in-play archive empty for E0, F1 and D1 (but
    priced for SP1 and I1).
  - Nobody has checked whether DraftKings quotes Greek, Turkish, Scottish or Belgian matches
    in play at all. Betfair more likely does.
  - Expect part of 1b to come back "not priced".

**Everything together: 89 trailing heavy favourites in 89 slots, 890 credits.** At C's usable
rate (81% of trailing favourites became DraftKings bets) that is **about 72 bets**. A heavy
favourite trails at half-time in about 8.5% of its matches (89 of 1,049). That is why the
counts are small: a 75%+ favourite rarely trails.

**2. Power.** The gap is the realized win rate minus the market's fair chance (about 46% in
this band), with a two-sided 95% range.

| True gap | Bets for the range to exclude 0 at 80% power | Where the expected range just touches 0 (50% power) | With ~72 bets |
| --- | --- | --- | --- |
| +12 points (what was seen) | 136 | 67 | about a 53% chance the range excludes 0 |
| +6 points (a likely shrunk value) | 542 | 266 | about a 17% chance |

- With ~72 bets, the smallest gap found 80% of the time is about 16 points.
- An ROI test at these odds (about 2.3) needs about the same numbers.
- Neither 1a, 1b, nor both together reach the 80%-power size for +12. For +6 it would take
  every trailing heavy favourite in these leagues for five to six seasons.
- Going forward: about 9 a season come from the eight leagues and about 20 from the six
  other top divisions, so about 24 usable bets a season. Reaching 136 would take more than
  five seasons.

**3. DRAFT pre-registration. Not approved, not to be bought.** It is written down so the rule
exists before any price is seen. The scope above says a purchase would most likely end
"inconclusive".
- **Rule:** $1 on the favourite priced 75%+ before kickoff (Shin-fair, Pinnacle's close,
  else the market average's close) that trails at half-time, at DraftKings' in-play price
  from the historical snapshot at kickoff + 55 min. The quote checks are C's, unchanged:
  48–62 min window, updated after kickoff, at most 5 min old, moved against the favourite.
  Betfair after 2% is the second line.
- **Sample:** a frozen slot list written from this scope, never from results:
  - the eight leagues in 2025/26 and in 2026/27 up to the freeze date;
  - N1, SC0, P1, G1, T1, B1 and the four second tiers, 2022/23–2025/26;
  - nothing B or C used.
  89 slots, 890 credits as of 26 Oct.
- **Guard:** C's, on every league-season. If the first 3 bought snapshots, oldest first, show
  no started event freshly priced by DraftKings or Betfair, buying stops for that
  league-season and its matches count as not priced.
- **Pass rule:** at least `MIN_MATCHES` 60 usable DraftKings bets **and** the ROI 95% range
  (2,000 match resamples, seed 0) entirely above 0. "Fail" is the range including or below 0.
  Under 60 usable is "inconclusive", and nothing changes either way.
  - Why 60: it is just under the 67 bets at which a true +12-point gap's expected range
    reaches 0. Below that, even a real effect of the size seen can't show.
  - C used 100. Lowering the bar here is a concession to the sample's size, and is stated
    as one.
- **Secondary (descriptive):** realized minus implied, Betfair, and per league and season.
  No further band cuts.
- **Cap:** 900 credits (89 slots at 10, plus the first call budgeted at 20), with the 3,000
  reserve. Run once, by the lead, only after the owner approves the spend.

**4. Why it might be real, and why it might not.**
- **Might be real:** an in-play price has to move fast on the score, and a goal down at
  half-time is the loudest thing on the screen. For a side that was 75%+ before kickoff, the
  gap in quality is still there in the second half (more shots, more possession, a deeper
  bench). If the market leans on the score more than on that gap, the comeback of a truly
  dominant side would be underpriced, while ordinary favourites (45–65%), whose edge is
  thinner, would not be. That is the pattern in C's bands.
- **Might not be real:**
  - It is the best of four bands, chosen after the results.
  - It rests on 39 bets, and its range already touches 0.
  - The same move has already failed once: B's 45–55% band was +29.9% on 99 bets, and the
    same band in C was −29.0% on 423.
  - C's 65–75% band, right next door, was −49.8%. A real "dominant side" effect should
    strengthen gradually with the favourite's chance, not jump from −50% to +51% at 75%.
- **The honest prior:** most of the +51% is noise. The free counts say any priced test would
  be under-powered for a realistic gap.

**Recommendation.** Don't buy now. If the owner wants to keep the idea alive cheaply, the next
step would be a forward live log, which is not a test:
- in the leagues above, when a 75%+ favourite trails at half-time (ESPN's free scoreboard),
  one live `/odds` call for that league, 1 credit, records DraftKings' and Betfair's price;
- about 30 matches a season, about 30 credits;
- it would be judged later under a rule frozen before it starts.
That needs a design and the owner's approval of its own.

## Cash-out: when to take risk off a winning underdog bet (owner via the lead, 28 Oct; exploratory, 0 credits)

**The question.** "If we bet heavy underdogs and then cash out when it's at 50%, what's the
best way to take risk off once we've squeezed out enough value that the swings after aren't
worth it?"

**Part 1: the framework.**
- **A fair cash-out never changes expected value.** It sells the position at the market's own
  chance of it winning. A fair price is a martingale: on average it equals what the position
  will pay at the end. Cashing out at a fair price swaps the same average for less spread. It
  is risk control, not profit.
- **A real cash-out offer carries a margin.** The book pays less than fair, so every cash-out
  lowers expected value by about the margin times the position's value.
- **Value isn't "squeezed out" over time.** If the bet had an edge when it was placed, the
  edge sits in the position until settlement. Selling early hands the rest of it, plus the
  cash-out margin, back to the book. A position that has gone up in value has not banked
  anything until it settles or is sold.
- **The Kelly view.** At any moment the right exposure is the Kelly stake on your current
  edge at the current in-play price.
  - A partial cash-out is just rebalancing down to that stake.
  - Cash out fully only when your edge at the current price is no bigger than what the
    cash-out costs.
  - With no edge at the live price, Kelly holds nothing, and a fair cash-out is the right
    way to get there.
- **The honest starting point.**
  - Our pre-match picks don't beat the close: Model 12% CLV −4.6%, Blend Lean −3.1%.
  - Long shots carry the biggest margin (the favourite–longshot bias in "Price-band edge
    rule").
  - A cash-out rule moves risk around. It can't turn a negative entry into a positive one.

**Part 2: a free check on data we already hold** (`lab/cashout.py`, `odds-check.yml`
`task=cashout`, run 40638748115, no key, cached snapshots only).

*What was measured.*
- The design B and C half-time snapshots are exactly the matches where the pre-match underdog
  **led at half-time**: the moment a winning underdog bet would be cashed out.
- 930 matches had a passing DraftKings in-play quote (C 732, B 198).
- For each, per $1 on the underdog at Pinnacle's pre-match close (football-data; every row
  had Pinnacle's close):
  - **hold** = the actual result;
  - **cash out at half-time** = entry odds × the underdog's margin-free chance at
    DraftKings' in-play price × (1 − margin) − 1, for margins of 0% (fair), 5% and 10%;
  - **Kelly partial** = keep the share of the position that quarter Kelly supports at
    DraftKings' live price on phase 1's in-play model chance (bankroll 100 per $1 entry),
    and cash out the rest.
- Ranges resample matches (2,000 draws).

*Read this first.*
- Every return here is **conditional on the underdog already leading at half-time**, so the
  absolute numbers (+1.7 per $1) are far above anything a real underdog strategy earns.
- Only hold against cash out is meaningful.
- This reuses design C's data, where the in-play price over-priced the trailing favourite
  (17.0% won vs 21.3% implied). It is largely the same finding seen from the leader's side.
- Underdogs that were level or behind at half-time aren't in the sample: we bought snapshots
  only where the favourite trailed.

*Results (per $1 staked pre-match).*

| Group | Matches | Underdog won vs in-play chance | Hold: mean (sd) | Fair cash-out: mean (sd) | Hold − cash out at 0% · 5% · 10% (95%) | Kelly partial at 5%: mean (sd), share kept |
| --- | --- | --- | --- | --- | --- | --- |
| All | 930 | 55.4% vs 54.0% (+1.4 pts, −1.7 to +4.5) | +1.74 (2.77) | +1.69 (0.83) | +0.05 (−0.13 to +0.23) · +0.18 (+0.01 to +0.36) · +0.32 (+0.14 to +0.50) | +1.76 (1.96), 54% |
| Design C | 732 | 57.1% vs 55.1% (+2.0, −1.5 to +5.3) | +1.76 (2.61) | +1.68 (0.80) | +0.08 · +0.21 · +0.35 (+0.16 to +0.52) | +1.75 (1.85), 51% |
| Design B | 198 | 49.0% vs 50.0% (−1.0, −7.5 to +5.2) | +1.69 (3.31) | +1.74 (0.93) | −0.05 · +0.09 · +0.22 (−0.22 to +0.68) | +1.78 (2.32), 64% |
| Underdog priced 3.0–4.0 | 242 | 63.2% vs 60.9% (+2.3, −3.5 to +8.0) | +1.34 (1.79) | +1.26 (0.48) | +0.08 · +0.19 · +0.31 (+0.09 to +0.52) | +1.24 (1.28), 52% |
| Heavy underdog, 4.0+ | 688 | 52.6% vs 51.6% (+1.0, −2.5 to +4.5) | +1.88 (3.03) | +1.85 (0.87) | +0.04 · +0.18 · +0.32 (+0.11 to +0.55) | +1.94 (2.12), 55% |
| Half-time chance under 40% | 132 | 40.2% vs 31.1% (**+9.0**, +0.7 to +17.2) | +2.19 (4.36) | +1.64 (0.83) | +0.55 (−0.17 to +1.30) · +0.68 · +0.81 (+0.09 to +1.56) | +2.18 (3.14), 75% |
| Half-time chance 40–55% ("cash out at 50%") | 436 | 48.4% vs 48.5% (−0.1, −4.9 to +4.5) | +1.36 (2.53) | +1.43 (0.57) | −0.07 (−0.31 to +0.19) · +0.06 · +0.18 (−0.06 to +0.43) | +1.44 (1.87), 67% |
| Half-time chance 55%+ | 362 | 69.3% vs 69.0% (+0.3, −4.4 to +4.8) | +2.04 (2.20) | +2.03 (0.96) | +0.01 · +0.16 · +0.31 (+0.11 to +0.51) | +1.99 (1.35), 30% |

- No underdog was priced under 3.0 pre-match: the favourites in B and C were all 45%+.

*What it shows.*
- **At a fair price, cashing out changed nothing on average** (+0.05 per $1, −0.13 to
  +0.23). It cut the spread of outcomes by about 70% (sd 2.77 → 0.83). That is the
  martingale, measured.
- **At a real cash-out margin it costs money.** It cost 0.18 per $1 staked at 5% and 0.32 at
  10%; both ranges sit above 0 in favour of holding. That is about the margin times the
  position's value (about 2.7 per $1).
- **"Cash out at 50%"** (underdog 40–55% to win at half-time): the in-play price was right
  (48.4% won vs 48.5%). Holding and a fair cash-out were the same. Any real margin makes
  cashing out the worse choice on average, with a much smaller spread.
- **The partial (Kelly) line** kept about half the position on average. It landed between
  hold and cash out on spread (sd 1.96 against 2.77 and 0.83). Its mean was slightly above
  both, because it held more where the model saw value (75% kept below a 40% half-time
  chance).
- **One hint, exploratory only.** Where the leading underdog was still priced under 40% at
  half-time, it won 40.2% against the price's 31.1% (+9.0 points, +0.7 to +17.2, 132
  matches). This is design C's over-priced trailing favourite seen from the other side, and
  it is one of several cuts. It would need its own pre-registration on unused seasons, and
  the owner's approval, before any credits.

**The answer in plain English (for the owner).**
- Cashing out doesn't lock in value. It sells your bet back at the book's price minus a fee.
- In 930 real matches where an underdog led at half-time:
  - at a fair price, cashing out left the average return exactly where holding left it, and
    removed about 70% of the swings;
  - at a typical 5–10% cash-out margin, it cost about 6–12% of the bet's value each time;
  - at your "50%" point, the in-play price was accurate, so there was nothing left to
    squeeze.
- So use a cash-out only to cut risk you can't afford. Then the smart way is partial: keep
  the share Kelly says your remaining edge supports, and sell the rest. If you see no edge at
  the live price, sell the lot.
- None of this fixes the entry. Our pre-match underdog picks lose to the closing price, and
  no cash-out rule can make a losing bet a winning one.

## In-play archive probes: how far back are half-time prices kept? (owner-approved 28 Oct)

**What and why.** The 75%+ heavy-favourite idea ("Heavy favourites (75%+)" above) needs
priced half-times that B and C never used. Design C's guard found 2022/23 unpriced for E0, F1
and D1 but priced for SP1 and I1. These probes settle where each league's archive starts.
- **Probe:** one historical bulk h2h snapshot (10 books = one region, 10 credits) per
  league-season, at kickoff + 55 min of the busiest weekend kickoff between October and
  April, taken from football-data.
- **"Priced":** design C's guard (`snapshot_priced`), meaning a started event freshly quoted
  in play by DraftKings or Betfair.
- **Walk-back:** each league walks back from its newest unknown season and stops at the first
  unpriced one. E0, F1 and D1 weren't probed.
- **Run:** `lab/archive_probe.py`, `odds-check.yml` `task=archive-probe`, run 40640666882,
  after a dry run (40640474263).
- **Spend:** 28 paid snapshots, **280 credits**. Balance 4,938,756 → 4,938,476 on the
  upgraded plan; the owner lifted the 200 cap the same day. Two snapshots came back empty and
  cost 0.
- The bodies are in the cache `archive-probe-40640666882`.

| League | Season (slot) | Priced? | Started events | Books quoting in play, fresh |
| --- | --- | --- | --- | --- |
| SP1 | 2021/22 (3 Oct 2021) | **yes** | 2 | DraftKings, FanDuel, BetMGM, Bovada, Paddy Power, Pinnacle |
| SP1 | 2020/21 (3 Oct 2020) | **yes** | 2 | DraftKings, FanDuel, BetMGM, 888, Paddy Power, Pinnacle |
| SP1 | 2019/20 (3 Nov 2019) | no: the archive has no snapshot (empty, 0 credits) | 0 | none |
| I1 | 2021/22 (17 Oct 2021) | **yes** | 3 | DraftKings, FanDuel, Bovada, Paddy Power, Pinnacle |
| I1 | 2020/21 (3 Jan 2021) | no | 7 | 888 only |
| E1 | 2021/22 (2 Oct 2021) | no | 10 | none |
| E2 | 2021/22 (2 Oct 2021) | no | 12 | Pinnacle, Paddy Power, Bovada (no DraftKings or Betfair) |
| E3 | 2021/22 (2 Oct 2021) | no | 12 | none |
| N1 | 2022/23 (23 Oct 2022) | **yes** | 3 | DraftKings, Bovada, Unibet, Pinnacle |
| N1 | 2021/22 (2 Oct 2021) | no | 2 | Pinnacle, Paddy Power, Bovada |
| P1 | 2022/23 (13 Nov 2022) | **yes** | 3 | DraftKings, FanDuel, Bovada, Paddy Power, Unibet, Pinnacle |
| P1 | 2021/22 (3 Oct 2021) | no | 2 | none |
| SC0 | 2022/23 (1 Oct 2022) | **yes** | 5 | DraftKings, FanDuel, Paddy Power, Bovada, Pinnacle |
| SC0 | 2021/22 (16 Oct 2021) | no | 5 | none |
| B1 | 2022/23 (23 Apr 2023) | **yes** | 7 | all ten, Betfair included |
| B1 | 2021/22 (10 Apr 2022) | no | 7 | none |
| T1 | 2022/23 (5 Mar 2023) | **yes** | 1 | DraftKings, FanDuel… (one started match) |
| T1 | 2021/22 (23 Jan 2022) | no | 3 | Pinnacle, Paddy Power, Bovada |
| G1 | 2022/23 (12 Mar 2023) | **yes** | 7 | DraftKings, FanDuel, BetMGM, Bovada, Paddy Power, Unibet, Pinnacle |
| G1 | 2021/22 (6 Mar 2022) | no: no snapshot (empty, 0 credits) | 0 | none |
| D2 | 2022/23 (9 Apr 2023) | **yes** | 5 | all ten |
| D2 | 2021/22 (17 Apr 2022) | **yes** | 5 | DraftKings, FanDuel, Bovada, Paddy Power, Pinnacle |
| D2 | 2020/21 (3 Jan 2021) | no | 5 | none |
| I2 | 2022/23 (1 Oct 2022) | **yes** | 7 | DraftKings, Bovada, Paddy Power, Pinnacle |
| I2 | 2021/22 (30 Apr 2022) | no | 9 | none |
| SP2 | 2022/23 (1 Oct 2022) | **yes** | 2 | DraftKings, FanDuel, Paddy Power, Pinnacle |
| SP2 | 2021/22 (10 Oct 2021) | **yes** | 3 | DraftKings, Paddy Power, Pinnacle |
| SP2 | 2020/21 (11 Apr 2021) | no | 3 | none |
| F2 | 2022/23 (12 Nov 2022) | **yes** | 9 | DraftKings, FanDuel, Bovada, Paddy Power, Unibet, Pinnacle |
| F2 | 2021/22 (2 Oct 2021) | no | 8 | none |

**What it says.**
- DraftKings' in-play prices are in the archive from **2022/23 in every league probed**:
  the six other top divisions and four second tiers newly checked, plus C's priced SP1, I1
  and the English lower leagues.
- They go back further only in Spain: SP1 to 2020/21, SP2 and D2 to 2021/22, I1 to 2021/22.
- Before mid-2020 the archive holds no snapshots at all.
- **Caveat: one snapshot per league-season tests one date.** Coverage may start part-way
  through a season, which is why C's guard checks its first three snapshots. So a "no" here
  means "not on that date". E0, F1 and D1 2022/23 (unpriced in C) fit the same picture of
  coverage arriving during 2022.
- Some unpriced seasons still had **Pinnacle** quoting in play (E2, N1 and T1 in 2021/22).
  A test whose price is DraftKings or Betfair can't use them. A Pinnacle-priced test could,
  but that would be a different rule and needs its own pre-registration.
- **Seasons newly known to be priced for a 75%+ test:** SP1 2020/21–2021/22; I1, D2 and SP2
  2021/22; N1, P1, SC0, B1, T1, G1, D2, I2, SP2 and F2 from 2022/23. The counts for these
  follow in the heavy-favourite count below.

## Heavy favourites (75%+), design H: final pre-registration (frozen 2026-10-28, before any purchase)

The owner approved buying this test on 28 Oct, via the lead. This replaces the DRAFT in
"Heavy favourites (75%+)" above. It is written and committed before any design-H snapshot is
bought, and nothing in it changes after a price is seen.

**Hypothesis (new; picked after C's results, so judged only on data no earlier test used).** A
favourite priced 75%+ before kickoff that trails at half-time wins more often than
DraftKings' in-play price implies, enough to make a profit backing it.

**Sample (frozen).** `src/soccer_stats/lab/comeback_h_slots.json`, SHA-256 starting
`4c37e90908306a4e`.
- **Built by:** `heavy_favourites --slots-out` (`odds-check.yml` `task=heavy-slots`, run
  40641592811), from football-data only, reading half-time scores, never full-time ones.
- **Favourite:** Shin-fair chance ≥ 75% from Pinnacle's close, else the market average's
  close. The file marks which (`price`).
- **League-seasons:** every one the archive probes of 28 Oct showed priced:
  - the eight leagues in 2025/26 and 2026/27 to date (to the files of 28 Oct);
  - SP1 2020/21 and 2021/22;
  - I1, D2 and SP2 2021/22;
  - N1, P1, SC0, B1, T1, G1, D2, I2, SP2 and F2 from 2022/23 to 2026/27 to date.
- **Excluded:** every slot bought by B or C (none overlap).
- **103 slots holding 103 trailing heavy favourites.** Leagues with any: N1 22, SC0 16,
  P1 16, G1 12, T1 11, I1 8, SP1 7, B1 3, D1 2, F1 2, E0 1, E1 1, SP2 1, I2 1. E2, E3, D2
  and F2 have none.
- Cups and the other leagues still being counted are **not** in this test. They can become
  a separate pre-registered sample.

**Purchase.**
- `comeback_fetch --slots H` (`odds-check.yml` `task=comeback-fetch`, `design=H`): one
  historical bulk h2h snapshot per slot at kickoff + 55 min, at the same ten books (one
  region, 10 credits).
- **Estimate 1,030 credits; cap 1,236** (estimate + 20%), run by the lead or with the lead's
  sign-off.
- The same spending rules as C:
  - `CappedBudget` with the 3,000 reserve;
  - the first call budgeted at 20;
  - stop on a call over 10 credits or any HTTP/network error;
  - never buy a slot twice;
  - a dry run first.
- Bodies go to the same `comeback-odds-*` cache: different snapshot times, and guard keys
  that don't overlap with C's.
- **Guard (C's, every league-season):** if a league-season's first 3 bought snapshots, oldest
  first, show no started event freshly priced by DraftKings or Betfair, no more of it is
  bought and its matches count as **not priced**. A league-season with fewer than 3 slots is
  bought in full.

**Analysis (run once; `comebacks2 --slots H`, `task=comebacks2`, `design=H`, no key).**
- C's quote checks, unchanged: the 48–62-minute window; updated after kickoff; at most 5
  minutes old; moved against the favourite; Pinnacle within 0.05 of the other books' median,
  else Betfair for the fair chance.
- Matches are joined to football-data by league, season, home and away, with the favourite
  priced exactly as in the slot list.
- **Primary:** $1 on the trailing favourite at DraftKings' passing in-play price. ROI per $1
  with a match-resampled 95% range (2,000 draws, seed 0).
- **Pass:** at least **100** usable DraftKings bets **and** the ROI range entirely above 0.
- **Fail:** at least 100 usable and the range includes 0 or sits below it. A range entirely
  below 0 reads "the in-play price over-prices the comeback".
- **Inconclusive:** under 100 usable bets. Nothing changes live in any case. A pass only
  justifies proposing a live paper track to the owner.
- **Secondary (descriptive):** the realized − implied win-rate gap with its range, against the
  in-play price's margin-free chance; Betfair after 2%; per league and per season; Pinnacle-
  vs average-priced favourites; the phase-1 model's blend weight c where Pinnacle's
  over/under exists.

**What to expect, stated before buying.**
- At C's usable rate (81%), 103 matches give about **83 usable bets**, under the 100
  minimum. The most likely verdict is therefore "inconclusive", whatever the prices show.
- With 83 bets, a true +12-point gap would show a range above 0 about 59% of the time. A
  6-point gap would only show it about 20% of the time.
- The purchase still buys the only fresh prices that exist for these league-seasons. Pooling
  them later with a separately pre-registered cup or extra-league sample would need its own
  rule, written before that sample's prices are seen.

## Strategy search with a locked holdout (owner via the lead, 28 Oct): design, written before any search runs

**The owner's ask.** "Study the data we already have to find a strategy (position sizing,
when to cash out…) that would have had positive ROI. I know this can be overfitting, but we
can try first." This is an open search, built so luck can be told from a real edge
afterwards. This section is the design. It was written and committed before any search code
ran, and the results will follow below it with run ids.

**1. The holdout, locked first.**
- `lab.harness.Holdout` locks every football-data row from **2024-07-01 onward**
  (2024/25, 2025/26, 2026/27) in all eight leagues.
- Also locked: every live paper ledger and odds log on data-log, and DraftKings' 2025/26 E0
  file (`E0_dk.json` and its trades).
- The search scores 2017/18–2023/24 only. In practice, seasons where Pinnacle's early and
  close prices both cover ≥ 90% are the ones scored: 2019/20–2023/24, `lean_backtest`'s rule.
- The comeback snapshots (B, C, H) are spent on their own tests. They appear only in the
  in-play exit arms, labelled exploratory, and never in the multiple-comparison check.

**2. The search space.** Every rule tried is logged as an arm and counted, losers included.
- **Base selections**, all walk-forward on earlier data, refit every 28 days:
  - (a) the raw model's best side at an edge ≥ t;
  - (b) Blend Lean's z ≥ k on the blend;
  - (c) the market alone: every side in a price band.
  - t ∈ {0, 5, 12, 20%}, k ∈ {0.5, 1, 2}.
- **Filters**, at most two combined:
  - league (each of eight);
  - price band (≤1.50, 1.50–2.00, 2.00–3.00, 3.00–5.00, >5.00);
  - edge or z band (the pooled quintiles of the search window);
  - Lean's driver (market/model);
  - side (home/draw/away);
  - favourite/underdog;
  - day of week (weekend/midweek);
  - kickoff cluster (lone kickoff vs a shared slot);
  - team-strength gap (the model's rating gap in quintiles);
  - line movement (below).
- **Line movement.** football-data holds only two prices, the early one (about 2–4 days out)
  and the close. A rule that uses the move from early to close can only be decided **at the
  close**. Those arms bet at Pinnacle's close and are judged on ROI only: their CLV is 0 by
  construction, and that is said beside them. Rules decided at the early price can't see
  the move.
- **Markets:**
  - 1X2;
  - draw no bet (from the 1X2, as in "Draw no bet");
  - Asian handicap and over/under 2.5 at Pinnacle's own prices, through
    `match_markets.clean_prices`, with the model's chances from the same walk-forward score
    matrices.
- **Sizing**, applied to each selection's bets in time order:
  - flat $1;
  - ¼ and ½ Kelly on the blend's chance (where one exists), capped at 5%;
  - a stop rule: stop a rule for the rest of its season after a 20-unit drawdown.
  Sizing changes the money curve, not which bets are placed. It is reported beside flat and
  never counted as a separate edge.
- **Exits (exploratory, the comeback sample only):** hold, full cash-out and Kelly partial
  cash-out at half-time, as in "Cash-out". These arms are outside the multiple-comparison
  check because their data has been spent.

**3. Scoring, inside the search window only.**
- For each arm: bets, ROI per $1 and CLV against Pinnacle's Shin-fair close, each with a
  match-resampled 95% range (2,000 draws, seed 0).
- An arm with fewer than 100 bets is kept in the count and flagged.

**4. Correcting for the search.**
- **Number of arms tried:** every arm, losers included.
- **The best arm** by mean CLV, and the best by ROI.
- **The bootstrap of the maximum** (White's reality check):
  - recentre every arm's per-bet CLV to mean 0 (the no-skill null);
  - resample matches jointly across all arms 2,000 times;
  - take the maximum mean CLV over arms in each draw.
  The p-value is the share of draws whose maximum reaches the best arm's real mean. The same
  is done for ROI.
- **The share of arms with CLV above 0,** against the share the recentred null gives. Prices
  carry a margin, so most arms should sit below 0. A real edge has to beat the null's share,
  not 50%.

**5. Finalists and the holdout.**
- **Rule, fixed now:** an arm is a finalist only if:
  - its CLV 95% range is entirely above 0 in the search window;
  - it has at least 300 bets;
  - the reality-check p for CLV is below 0.05.
  At most three, ranked by the lower end of the CLV range. Line-movement arms (CLV 0 by
  construction) can't qualify.
- If no arm passes, the holdout stays locked and the search ends with "no strategy found".
- Otherwise the holdout is opened **once**, for the finalists only, with a logged reason
  (`Holdout.unlock`), and they are scored on 2024/25 onward with the same metrics.
- A finalist whose holdout CLV range is above 0 becomes a pre-registered live paper track.
  Nothing goes live otherwise, and the owner decides.

**Delivery:**
- `lab/strategy_search.py` and `odds-check.yml` `task=strategy-search`, no key (no purchase
  is planned);
- results in this section with run ids;
- a plain-English summary for the owner: what was tried, what the best looked like, how
  likely that was luck, and what the holdout said.

**Implementation notes (28 Oct, written with the code and before the full run;
`lab/strategy_search.py`).** These pin down choices the design left open. None was picked
from a result. The only run before this note was a two-league smoke test (E1 and E2, 200
draws) to check the code works; it is not a result.
- **Refits:** the model refits weekly (`backtest.walk_forward`, as every backtest here); the
  blend and σ every 28 days (`lean_backtest.walk_forward`).
- **Lean bases** exist only for 1X2 and draw no bet, the markets with a fitted blend. AH and
  over/under have the edge and every-side bases.
- **Filters:**
  - "Strength gap" = |model home chance − model away chance|, cut at the window's quintiles;
  - "Weekend" = Saturday or Sunday;
  - "Shared kickoff" = another match in the league kicks off at the same date and time
    (football-data's `Time`);
  - "Favourite" = the side with the highest margin-free early chance in its market;
  - edge and z quintiles are cut per market;
  - driver = "market" when z with the model's term removed reaches 1.
- **Every side** bets every side of every match, so its arms hedge within a match. They
  still count as arms.
- **The reality check's universe** is the arms that could be finalists: those with at least
  300 bets. A 2-bet arm can't be a finalist, so it can't set the bar. The same check over
  every arm is printed beside it.
  - Each arm gets an adjusted p: the share of draws whose recentred maximum reaches its
    mean. A finalist needs p < 0.05. For the best arm this is White's p.
  - The no-skill null's share of arms above 0 is measured on the same draws.
- **Sizing** runs on the finalists and the ten best arms by the CLV range's lower end, not
  on all of them. Kelly needs the blend's chance, so AH and over/under get flat and the
  stop rule only.
- **Exits:** the exploratory exit arms are the "Cash-out" section's numbers on the spent
  comeback sample. They are quoted, not rerun.
- **Holdout scoring** is written only if a finalist appears. It then reads the holdout once,
  after a logged `Holdout.unlock(reason)`, with the window's frozen cuts. With no finalist,
  no code ever reads 2024/25 onward.
