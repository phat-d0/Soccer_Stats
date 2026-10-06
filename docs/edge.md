# Edge research log

Owner: the edge-finder agent. This log records where a betting edge could exist and what
the data says. Each claim gives a sample size, a range, and an out-of-sample or
time-split check. Code: `src/soccer_stats/edge/` (tests in `tests/test_edge.py`). Jobs run
through `.github/workflows/odds-check.yml` with `task` = `props`, `match`, `shots` or
`signals`. These jobs are read-only: they never push and never deploy.

## Round 2 (2026-10-06)

### Summary

1. **No free signal earns blend weight beside Pinnacle's early price.** I tried 8 ideas.
   Seven could be tested (14 tests, plus a bet test each). Added to a conditional-logit
   blend on Pinnacle early, none lowered the result's log loss out of sample. Every
   gain's range includes 0 (best: +0.0002, range −0.0017 to +0.0019).
2. **Two signals do predict the closing-line move, but too weakly to bet.** Soft books
   leaning away from Pinnacle early (`soft_vs_sharp`), and recent xG form (`xg_form`,
   `xg_vs_goals`), each have a slope whose Bonferroni range excludes 0, with the same
   sign in 6 of 6 or 8 of 8 seasons. But they explain 0.1–1.6% of the move out of
   sample. Backing them at Pinnacle's early price gives CLV of −1.6% to −3.7%. That is
   better than a random early bet (−3.6%) for two of them, but never positive.
3. **Late team news can't be tested yet.** `fpl_news/` on data-log holds two days (5–6
   Oct 2026) and no match has been played since logging began. football-data gives only
   two untimed prices per match. Proposal M2 logs the prices we already fetch, so it can
   be tested in December at no credit cost.
4. **The best-over-price sample is costed at about 2,100 credits, not run.** A 100-credit
   pilot comes first, with a fixed go/kill rule (section 2).
5. **Credits used this round: 0.** The balance stays as the publish log reports it.

### 1. Free signals vs Pinnacle's early-to-close move

The code is `signals.py`, run with `task=signals` (football-data and Understat, no
credits). It covers 2017/18–2025/26: 3,302 matches with walk-forward model chances,
3,142 with both Pinnacle 1X2 prices and 2,400 with both over/under 2.5 prices.

Each signal was fixed before the run, built from earlier matches only (6-match form
window, not tuned), and tested two ways, season by season on earlier seasons only:

- **Move:** OLS of the move (change in log(home/away) for 1X2, or in logit(over 2.5) for
  totals, both margin-free by Shin) on the signal. I report the slope with its range,
  out-of-sample R², and how many test seasons agree in sign. The bet test backs the side
  the signal favours at Pinnacle early, on the largest fifth of signals (cut-off from
  earlier seasons), and gives its CLV against the fair close.
- **Blend:** `score_k = a_k + b·log(early_k) + d·signal`, with log loss on the result.

**Correcting for the number of tests.** 7 signals × 2 tests = 14. All ranges are
99.64%, Bonferroni for 14. Counting the bet test as a third family (21) moves the level
to 99.76%; it changes no conclusion.

**Baseline.** The 1X2 move has a standard deviation of 0.171 in log(home/away), and the
totals move 0.120 in logit. A random side bet at Pinnacle early has a CLV of −3.6%
(1X2 home/away) or −3.1% (totals).

| Signal | Market | Rows | Slope (range) | Out-of-sample R² | Same sign | Bets | CLV (range) | Blend gain (range) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Soft books vs Pinnacle early | 1X2 | 2,412 | +0.63 (+0.26 to +0.97) | +1.6% | 6/6 | 455 | −2.5% (−4.0 to −1.1) | 0.0000 (−0.0001 to 0.0000) |
| xG form (6-match xG difference) | 1X2 | 3,142 | +0.008 (+0.001 to +0.017) | +0.2% | 5/8 | 559 | −1.6% (−2.1 to −1.1) | +0.0002 (−0.0017 to +0.0019) |
| xG form minus goal form | 1X2 | 3,142 | +0.012 (+0.002 to +0.022) | +0.1% | 8/8 | 524 | −3.7% (−4.9 to −2.5) | −0.0004 (−0.0025 to +0.0018) |
| Rest days | 1X2 | 3,142 | −0.003 (−0.008 to +0.003) | −0.1% | 5/8 | 1,545 | −3.1% (−3.8 to −2.5) | −0.0006 (−0.0016 to +0.0004) |
| Model vs Pinnacle early | 1X2 | 3,142 | +0.019 (−0.005 to +0.043) | −0.1% | 6/8 | 579 | −4.6% (−6.0 to −3.2) | −0.0005 (−0.0038 to +0.0027) |
| xG total form minus goal total | O/U 2.5 | 2,400 | −0.003 (−0.011 to +0.007) | −0.6% | 2/6 | 482 | −2.9% (−3.7 to −2.0) | −0.0013 (−0.0065 to +0.0037) |
| Model vs Pinnacle early | O/U 2.5 | 2,400 | +0.001 (−0.033 to +0.039) | −0.3% | 4/6 | 427 | −3.6% (−4.4 to −2.8) | −0.0004 (−0.0015 to +0.0007) |

Blend log loss for 1X2, scored on 2018/19–2025/26: Pinnacle early 0.9551, with the best
signal 0.9550, Pinnacle close 0.9489. The close is 0.006 better than the early price;
no signal recovers any of that gap.

What the table shows:
- **Soft vs sharp.** When the market average rates a side higher than Pinnacle early
  does, Pinnacle's close moves toward that side. The correlation is +0.09 to +0.17 in
  every season except 2024/25 (+0.02). This is the strongest and steadiest signal: some
  information reaches the soft books, or the books' customers, before Pinnacle's early
  line. It is still worth only −2.5% CLV at Pinnacle early. football-data takes the average and
  Pinnacle's early price at the same moment, so the signal is bettable in principle.
- **xG form.** The early price under-weights recent xG a little, and the close corrects
  it. The slope is tiny: a 1-goal-per-match xG-difference edge moves the close about
  0.8% in log(home/away). Betting it gives the best CLV here (−1.6%), about 2 points
  better than random, and still negative. The live model already uses xG (weight 0.7),
  and the model-vs-early signal shows the market prices the rest by the close.
- **The model itself** neither predicts the move nor earns weight. This matches round 1
  (CLV −4.0% at Pinnacle early) and the DraftKings blend fit (c ≈ −0.03).
- **Rest and totals:** nothing. Rest only counts league matches; football-data and
  Understat have no cup or European fixtures, so true congestion is not measured.
- **Sample-size check:** with about 2,800 test matches, an R² of 0.5% would be
  detectable. The tests were not short of data; the signals are small.

**Verdict:** the early price already holds almost everything free public data knows.
None of these signals justifies a model change for betting.

### 1b. Late team news (idea 8: not testable yet)

- `fpl_news/2026-10.jsonl` has 305 rows from 7 snapshots (5 Oct 08:03 to 6 Oct 19:05).
  The 2026/27 season's first listed match is 10 Oct, so no news has a match to compare
  against.
- No free source gives timestamped historical FPL changes or timestamped soft prices.
  football-data's early and close prices carry no time.
- To make it testable, see proposal M2 below: log the DraftKings 1X2 prices that
  `publish.yml` already fetches, with their time. It touches `publish.yml` and
  data-log, so the lead owns it. Then compare each FPL status change (for a regular
  starter) with the price move in the next and previous hour. About 100 matches
  (mid-December) gives a first read. The cost is 0 credits: publish already pays for
  these calls.

### 2. Costed plan: best over price across FanDuel, 1xBet and Kambi (not run)

The question: does the best over price across the over-only books cut FanDuel's 10-point
gap enough for the calibrated blend to pay? Nothing is spent until the owner approves.

**Cost.** The Odds API charges 10 credits per market per region for a historical event
call; a `bookmakers=` list of up to 10 books counts as one region. With
`bookmakers=fanduel,onexbet,betrivers,ballybet,unibet` and both markets
(`player_shots`, `player_shots_on_target`), a close snapshot costs 20 credits. The event
lists for matches already in the FanDuel cache are cached too (0 credits).

| Stage | What | Calls | Credits |
| --- | --- | --- | --- |
| 0. Pilot | 5 cached 2025/26 matches (Aug, Oct, Dec, Feb, Apr), close snapshot | 5 | 100 |
| 1. Sample | 100 more 2025/26 matches with a cached FanDuel close, from the first month the pilot finds 1xBet or Kambi | 100 | 2,000 |
| Total | | 105 | **about 2,100** |

- The run needs at least 5,100 credits left at the start (2,100 plus the 3,000 live
  reserve) because the key is shared with the baseball app. It stops at the cap.
- Code needed (edge-finder, about 1 hour): a `props.best_over` backfill reusing
  `player_odds.backfill`'s budget guard, caching each call as
  `{event_id}_close_multi.json` beside the FanDuel files (so the paid
  `player-odds-history-*` cache is extended, never replaced), and a `task=best_over`
  in `odds-check.yml` with a `cap` input.
- **Stop after the pilot** if 1xBet and the Kambi books are both missing from 4 of the 5
  snapshots (round 1 saw no 1xBet in March 2025). That costs 100 credits, not 2,100.

**What is measured** (fixed now, before any data), on starters (Understat) at the close,
lines priced by FanDuel and at least one other book, ranges resampling whole matches:

1. *Price lift*: best price ÷ FanDuel price − 1 on the same player, market and line.
2. *Gap at the best price*: average implied chance (1/best odds) minus the win rate,
   overall and for odds ≤ 1.5 (where FanDuel's gap is 12–16 points).
3. *Back-all ROI at the best price*, overall and for odds ≤ 1.5 (FanDuel: −42.6% and
   −13.8% to −22.0%).
4. The main strategy (`blend_lineup`, 12% edge) replayed at the best price. At about
   0.2 bets a match this gives only about 20 bets: reported, not decisive.

With about 100 matches and about 100 starter lines a match, the gap's 95% range is
roughly ±1.5 points, and the paired price lift is much tighter.

**Decision rule:**
- **Go** to a full-season sample (about 380 matches, about 7,600 credits; a separate
  owner decision) only if the starters' gap at the best price is 4 points or less with
  the upper end of its range at 6 or less, **or** back-all ROI on odds ≤ 1.5 at the best
  price is −5% or better. At a 4-point gap, the blend's 12% edge rule would see bets
  with a real expected value near zero, so the full sample is what decides.
- **Kill** player shot betting for good if the median price lift is under 5% or the gap
  stays above 6 points.
- My prior: about 15% chance of passing. 1xBet's match margins are the thinnest of the
  named books (about 3%), which is the reason to try; but every book here is over-only,
  and over-only menus are where books put their margin.


### Proposals for the moneyline session ("Soccer: Moneyline")

| # | Proposal | Why | Cost |
| --- | --- | --- | --- |
| M1 | **No model change from this round.** Keep `xg_weight` 0.7 and the Pinnacle-fitted blend as they are. Don't add xG form, rest or soft-book features. | None earned out-of-sample blend weight (section 1). The model's own blend weight is about 0. | 0 |
| M2 | **(Now with the lead: the moneyline session handed it on.)** **Log the live DraftKings 1X2 and totals prices with their timestamp** on each `publish.yml` run, to `data-log/odds_log/YYYY-MM.jsonl` (append-only, like `fpl_news`). | It's the only way to test late team news (1b) and intraday moves. The prices are already fetched, so no credits. About 2 KB per run. | 0 credits, about 1 hour |
| M3 | (Optional, for the lead) If match paper trades are kept for display, report CLV against Pinnacle's fair close beside ROI. It's precise at about 200 bets; ROI isn't. | Round 1 and round 2 both find CLV is the only number tight enough to decide. | 0 |

### Ranked next steps (round 2)

| # | Step | Expected value | Cost | Owner |
| --- | --- | --- | --- | --- |
| 1 | M2: log live prices with timestamps, then test team news in December | Low to medium; the only untested free idea | 0 credits | lead-reviewer (touches `publish.yml` and data-log; moneyline handed it on), then edge-finder |
| 2 | Asian handicap and other totals lines on football-data (round 1's C): is the model's CLV better there? | Low after this round: the model earns no weight on 1X2 or O/U 2.5 | 0 credits | moneyline (in progress: `match_markets.py` on `team/moneyline`) |
| 3 | Best-over pilot (section 2), only if the owner wants player bets kept alive | Low (prior about 15%) | 100 credits, then 2,000 | edge-finder, owner approval |
| 4 | Recheck for two-sided EPL player shots in December (round 1's F) | Low | 5 credits | edge-finder |

Recommendation: do 1 (free). Run the 100-credit pilot only if the owner wants to keep
player bets. Otherwise accept that free public data doesn't beat the EPL closing line,
and keep the app as a model display and an honest record.

## Round 1 (2026-10-06)

### Summary

1. **No bookmaker on The Odds API prices EPL player shots on both sides.** I checked all
   five regions (us, us2, uk, eu, au) in one live snapshot and two historical snapshots.
   Every book that lists player shots lists the over only. So no book gives a measurable
   margin or a fair price to bet against. The CLAUDE.md first step ("find a two-sided
   book") is closed for now.
2. **Shot counts are not the problem.** Understat's counts match ESPN's player by player:
   99.8% of 617 player-matches are identical, the average difference is −0.002 shots, and
   no "1+ shots" bet would settle differently. Team totals match football-data too
   (ratio 0.998). FanDuel's roughly 10-point gap is real margin, not a data mismatch.
3. **Line shopping helps the match bets, but nothing turns clearly positive.** The trade
   rule's closing line value (CLV) against Pinnacle's fair close:
   - −6.6% at the average book's price;
   - −4.0% at Pinnacle's early price;
   - −2.3% at the best of seven named books.

   Only football-data's market maximum shows positive CLV. That maximum can't be fully
   bet: it takes the best price across offshore and stale books. Its CLV also fell from
   +1.8% in 2019/20 to −2.7% in 2025/26. Over 1,700–2,100 bets, every ROI range includes 0.
4. **No slice of FanDuel's player lines is close to fair.** Of 87 slices, the best is
   heavy favourites at the close (odds ≤ 1.25): −13.8% ROI. It got worse in the holdout
   season (−16.6%).

### Credits

| Call | Credits | Left after |
| --- | --- | --- |
| Live event odds, Arsenal v Leeds (5 regions, 2 markets) | 5 | 22,947 |
| Historical event list, 2026-10-03 (season not started) | 1 | 22,946 |
| Historical event list, 2025-03-15 | 1 | 22,945 |
| Historical event odds, Man City v Brighton, 2025-03-15 14:00 | 100 | 22,845 |
| Live event odds again (second run) | 5 | 22,840 |
| Historical event list, 2026-09-26 (season not started) | 1 | 22,839 |
| Live event odds again (third run) | 5 | 22,834 |
| Historical event list, 2026-09-26 and 2026-04-11 | 2 | 22,832 |
| Historical event odds, Arsenal v Bournemouth, 2026-04-11 10:30 | 100 | 22,732 |
| **Total this round** | **220 of the 300 cap** | **22,732** |

The balance was 22,952 at my first call (22,962 on 6 Oct; the shared key had already
spent 10 elsewhere). The 2026/27 Premier League has not kicked off yet: the earliest
listed match is 10 Oct 2026. So the two 2026/27 historical dates found no finished
match, and those runs cost 1 credit each.

### 1. Two-sided player props

The probe is `props.probe` (`task=props`). Each live call covered all five regions and
both markets (`player_shots`, `player_shots_on_target`). Live calls cost 5 credits, about
1 per region. Historical calls cost 100, which is 10 × 2 markets × 5 regions.

| Snapshot | Books with player markets | Markets | Lines with both over and under |
| --- | --- | --- | --- |
| Live, Arsenal v Leeds, 4 days before kickoff | ballybet, betparx, betrivers, unibet (Kambi), tabtouch | on target only | 0 |
| History, Man City v Brighton, 2025-03-15, 1 h before | ballybet, betrivers, fanduel | both | 0 |
| History, Arsenal v Bournemouth, 2026-04-11, 1 h before | ballybet, betrivers, fanduel, onexbet | both | 0 |

- Not one line had both sides at any book. No book in the uk or au regions prices shots
  at all, apart from TABtouch (on target only). Pinnacle, the Betfair exchange,
  DraftKings and BetMGM show no EPL player shots.
- New books, all over-only:
  - **1xBet** (eu): 165 shot lines on 40 players, more than FanDuel's 153.
  - **Kambi** books (BetRivers, BallyBet, Unibet, betPARX): about 50 shot lines on about
    22 players.
- These books still allow line shopping on overs (next experiment B). Without an under
  price, though, a fair chance can only come from our own model.
- I couldn't measure two-sided margins: there is no data.

### 2. Line shopping on match markets (football-data, no credits)

The job is `task=match`, seasons 2017/18–2025/26. It uses the live model settings
(Dixon-Coles, xG weight 0.7, weekly walk-forward refits). That gives 3,302 out-of-sample
predictions, all matched to football-data prices.

Rules for the test:
- The trade rule is fixed at a 12% edge, one bet per match, 1 unit per bet.
- "early" is football-data's pre-closing price (taken one to three days out); "close" is
  the last price before kickoff.
- CLV = price × Pinnacle's fair close (Shin) − 1.
- Ranges are bootstrap 95%.
- Nothing was tuned on these results.

**Margins.** These are the average sums of 1/odds minus 1:

| Book | 1X2 early | 1X2 close | O/U 2.5 close |
| --- | --- | --- | --- |
| Pinnacle | 2.8% | 2.6% | 3.0% |
| Betfair exchange, after 5% commission | 3.6% | 3.6% | 2.9% |
| 1xBet | 2.6% | 3.0% | – |
| Bet365 | 4.9% | 5.5% | 5.1% |
| William Hill / bwin / Interwetten / BetVictor | 5.0–5.7% | 4.7–5.8% | – |
| Market average | 4.5% | 4.3% | 4.9% |
| Best of 7 named books | 2.1% | 2.0% | 2.9% |
| Market maximum | 0.5% | −0.5% | 0.3% |

**The trade rule at each price source:**

| Price | Bets | ROI | ROI range | CLV | CLV range | Beat close |
| --- | --- | --- | --- | --- | --- | --- |
| Pinnacle early | 1,745 | −0.3% | −10.6% to +10.4% | −4.0% | −4.5% to −3.5% | 33% |
| Bet365 early | 1,590 | −1.8% | −12.3% to +9.5% | −5.9% | −6.5% to −5.4% | 27% |
| Average early ("DraftKings-like") | 1,277 | −3.3% | −13.8% to +8.5% | −6.6% | −7.2% to −6.0% | 24% |
| Best named early | 1,939 | +2.3% | −7.5% to +12.4% | −2.3% | −2.9% to −1.8% | 38% |
| Best named close | 2,124 | +3.4% | −6.3% to +12.9% | −3.0% | −3.1% to −2.8% | 12% |
| Max early | 1,712 | +3.3% | −7.5% to +13.9% | −0.2% | −0.8% to +0.3% | 46% |
| Max close | 1,970 | +3.8% | −5.6% to +13.2% | +1.0% | +0.8% to +1.2% | 54% |

- CLV is the precise number. ROI on about 1,800 bets at average odds of 5.5 has a ±10%
  range, so ROI alone can't separate +3% from −7%.
- A random bet at Pinnacle's early price has a CLV of about minus its margin (−2.8%). The
  rule's picks come out at −4.0%, so they do no better than random against the sharp
  close. This confirms the DraftKings finding that the claimed edges pick out model errors.
- **Time split:**
  - Max-close CLV by season, from 2019/20 to 2025/26: +1.8, +1.7, +1.8, +1.6, +1.1, −0.4,
    −2.7%.
  - Best-named-early CLV is negative in 8 of 9 seasons. The exception is 2017/18, at
    −0.0%.
  - The positive maximum in early seasons is the old aggregate catching outlier books. It
    has not survived the last two seasons.
- **Compare DraftKings** (`E0_dk.json`, 2025/26, 172 trades): ROI −17.3% (range −41% to
  +11%), CLV vs Pinnacle −6.4%. That is the same as the market-average row above. Betting
  at a US soft book costs about 4 points of CLV over shopping the best named book.
- The threshold sweep (2–20%) at Pinnacle, best named and Max shows flat CLV across
  thresholds. A higher claimed edge buys no better CLV. This is descriptive only; the 12%
  rule was not changed.
- **Verdict:** line shopping cuts the loss from about −6.5% to about −2.5% CLV. It does
  not find an edge. The match model needs a better signal; better prices alone won't do it.

### 3. FanDuel player lines without the model

The data is `backtest/E0_player_lines.csv.gz`: 178,311 over lines from 749 matches,
2023/24 (sparse) to 2025/26. The code is `fanduel.scan`. Each slice's return comes from
backing every over in it. Ranges resample whole matches, Bonferroni-widened for the 87
slices examined (8 one-way dimensions and 6 two-way crossings, each with at least 300
lines).

Starters at the close (73,565 lines) lose −42.6% (range −44.5% to −40.5%): an implied
32.9% against 23.1% won. By slice:

| Slice | Lines | Implied | Won | Gap | ROI |
| --- | --- | --- | --- | --- | --- |
| odds ≤ 1.25 | 6,172 | 89.7% | 77.5% | 12.2 pts | −13.8% |
| odds 1.25–1.5 | 5,471 | 72.9% | 56.9% | 16.0 | −22.0% |
| odds 2–3 | 8,851 | 40.5% | 26.1% | 14.4 | −35.9% |
| odds 5–10 | 12,818 | 14.7% | 8.3% | 6.4 | −44.2% |
| odds > 10 | 20,211 | 5.4% | 2.0% | 3.4 | −64.5% |
| Shots 1+ | 11,651 | 72.3% | 57.9% | 14.4 | −21.9% |
| Shots on target 1+ | 11,808 | 39.0% | 28.8% | 10.2 | −29.8% |
| Forwards | 13,741 | 43.9% | 32.8% | 11.0 | −37.6% |
| Defenders | 20,695 | 24.3% | 14.7% | 9.6 | −50.2% |
| Close odds shortened since the look | 15,958 | 33.1% | 23.0% | 10.1 | −40.1% |
| Close odds drifted since the look | 13,949 | 32.1% | 21.7% | 10.3 | −43.1% |

What the table shows:
- The margin is a near-constant 10–16 points of probability. As a share of the price it
  is lightest on short odds and enormous on longshots, a strong favourite-longshot bias.
- Timing barely matters. Starters at the look lose −43.1%, starters at the close −42.6%.
- Line moves carry no usable signal: shortened −40%, drifted −43%.
- The gap widened from 8.5 points (2024/25) to 11.3 points (2025/26).
- **Holdout:** the top 5 slices on 2023/24–2024/25 all lose on 2025/26. Close and
  odds ≤ 1.25 goes from −11.2% to −16.6% (range −18.5% to −14.7%). No slice's widened
  range reaches 0.
- **Verdict:** no part of FanDuel's player menu is close to fair. To win, a bet would need
  an edge of 14 points or more of ROI on the shortest prices, and more everywhere else.

### 4. Shot counts: Understat vs settlement-style sources

This answers the player-shots agent's question (`task=shots`, 2025/26).

| Check | Sample | Understat | Other source | Exact match |
| --- | --- | --- | --- | --- |
| Team shots vs football-data HS/AS | 760 team-matches | 12.48 | 12.50 | 96.8% |
| Team on target vs HST/AST | 760 | 4.04 | 4.19 | 86.6% |
| Player shots vs ESPN summaries | 617 player-matches, 21 matches | 0.878 | 0.880 | 99.8% |
| Player on target vs ESPN | 617 | 0.282 | 0.289 | 99.4% |

- ESPN's player names matched for 617 of 650 rows; 33 were skipped as ambiguous.
- "1+ shots" disagrees on 0 of 617 rows.
- On target runs 2–3% low against both sources: one shot in 40 is "on target" for them
  but not for Understat (for example, shots blocked on the line). That moves on-target
  implied chances by under 1 point.
- FBref blocks automated clients and FotMob's API needs signed requests, so I didn't try
  them. ESPN (Opta-fed) is reachable from Actions.

### Next experiments, ranked by expected value per cost

| # | Experiment | Expected value | Cost | Owner |
| --- | --- | --- | --- | --- |
| A | **Stop spending on FanDuel player shots.** Don't build the lineup feed or the blend for live trading. Keep the model for display. | Avoids a sure −20% to −40% | 0 | player-shots (decision), lead-reviewer |
| B | **Best over across FanDuel, 1xBet and Kambi** on a sample: about 100 matches at the close, with `bookmakers=fanduel,onexbet,betrivers`. One call for up to 10 named books costs the same as one region, about 20 credits a snapshot. The question is whether the best over cuts the 10-point gap to under 4 points, where the calibrated blend might pay. | Low to medium: all are over-only books with similar margins | about 2,000 credits | edge-finder |
| C | **Different match markets with two-sided sharp prices**: Asian handicap and totals other than 2.5 (football-data has AH and Pinnacle AH closes). Test whether the model's CLV is better there than on 1X2, where it is −4%. | Medium | 0 credits | moneyline |
| D | **Use the market inside the match model** (the blend idea from player_calibration, applied to 1X2): logit-blend the model with Pinnacle's early price, walk-forward, and measure CLV against the close. This is the standard way to turn a model that loses on its own into one that adds to the market. | Medium | 0 credits | moneyline |
| E | **Late team news** (lineups an hour before kickoff) against early soft prices: CLV from the early price to the close at the best named book. Needs lineup history, so ESPN `starter` flags backfilled from summaries. | Medium, but costly to build | 0 credits, high build cost | moneyline + edge-finder |
| F | Check again in December whether any EPL book adds unders (one live call, 5 credits). | Low | 5 credits | edge-finder |

Recommendation: do C and D first (both free, on data we already have), then B only if
the owner wants to keep player bets alive.
