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
