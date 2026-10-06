# Edge research log

Owner: the edge-finder agent. This log records where a betting edge could exist and what
the data says. Each claim gives a sample size, a range, and an out-of-sample or
time-split check. Code: `src/soccer_stats/edge/` (tests in `tests/test_edge.py`). Jobs run
through `.github/workflows/odds-check.yml` with `task` = `props`, `match` or `shots`. These
jobs are read-only: they never push and never deploy.

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

## Round 2: match markets beyond 1X2 (moneyline, 2026-10-06)

Owner: moneyline. Code: `src/soccer_stats/match_markets.py` (tests in
`tests/test_match_markets.py`), run with `soccer-stats match-markets` or *Backfill DraftKings
odds* with `match_markets` ticked (no key, no push; run 37525340740). This covers
experiments C (Asian handicap and other totals) and D (the blend at Pinnacle's early price).

**Setup.**
- Data: football-data E0, 2017/18–2025/26. Live model settings (Dixon-Coles, xG weight 0.7,
  weekly walk-forward refits): 3,302 out-of-sample predictions.
- Markets:
  - 1X2: 3,142 matches.
  - Over/under 2.5: 2,401 matches. Pinnacle prices start in 2019/20, and football-data has
    no other totals line, so "totals other than 2.5" can't be tested on free data.
  - Asian handicap: 2,412 matches, from 2019/20. There is one line per match (AHh early,
    AHCh close), with Pinnacle's two-way prices plus the average and maximum at the early
    line. The line moved between early and close in 38% of matches, and those get no CLV.
    20% of matches had a push or a half stake.
- AH chance: a side's break-even chance, (W + HW/2) / (W + HW/2 + L + HL/2), from the
  model's score matrix. Quarter lines are split into two half-stakes. Blend fits weight
  each match by its unpushed share.
- Blend: `match_calibration.fit`, now with row weights and optional extra signal terms.
  It is fitted on Pinnacle's fair close, refitted every 28 days on earlier matches only,
  and applied to the margin-free price at the time of the bet.
- Trade rule: one bet per match, on the side with the larger edge. The sweep covers 2–20%.
  All strategies bet on the same matches (those the blend covers).
- CLV = price × Pinnacle's fair close (Shin) − 1. Ranges are bootstrap 95%.
- **Price check:** football-data's average and maximum prices hold a few broken rows: 10
  AH average rows, 12 AH maximum rows and 10 1X2 maximum rows had overrounds far below
  100%. The first run, without the check, showed absurd +100% to +700% AH returns at those
  prices. Rows with an overround outside −3% to +20%, or more than 10 points from
  Pinnacle's chance, are now blanked and counted in the output.

**Log loss** (out of sample, same matches; lower is better):

| Market | Matches | Model | Blend | Pinnacle early | Pinnacle close | Model weight c (live fit) |
| --- | --- | --- | --- | --- | --- | --- |
| 1X2 | 2,816 | 0.9614 | 0.9504 | 0.9530 | **0.9489** | 0.06 (refits −0.41 to +0.38) |
| O/U 2.5 | 2,079 | 0.6794 | 0.6770 | 0.6759 | **0.6755** | 0.11 (refits −0.43 to +0.23) |
| Asian handicap | 2,090 | 0.7066 | 0.6949 | 0.6952 | **0.6939** | 0.12 (refits +0.05 to +0.78; b = 0.52) |

The model is worse than Pinnacle's close in every market. The blend, which takes the
close as its input, is still worse out of sample than the close alone. On AH the fit
shrinks Pinnacle's price (b ≈ 0.5) and gives the model a small positive weight. That
weight was largest in early refits and has fallen to 0.09–0.12.

**Trade rule at Pinnacle's early price** (a bettable sharp price; CLV vs Pinnacle's fair close):

| Market | Strategy | Edge | Bets | ROI (95% range) | CLV (95% range) |
| --- | --- | --- | --- | --- | --- |
| 1X2 | model | 12% | 1,419 | +2.2% (−9.8 to +14.9) | −4.2% (−4.8 to −3.6) |
| 1X2 | blend | 2% / 12% | 992 / 63 | −2.3% / +17.7% (−34 to +77) | −2.7% / −2.1% (−4.8 to +0.8) |
| O/U 2.5 | model | 12% | 285 | +3.2% (−9.4 to +16.4) | −2.9% (−3.7 to −1.9) |
| O/U 2.5 | blend | 2% / 12% | 266 / 8 | −2.4% / −15.5% | −2.8% / +0.5% (−5.7 to +5.1) |
| AH | model | 12% | 744 | +1.8% (−4.8 to +8.1) | −2.0% (−2.5 to −1.5) |
| AH | blend | 2% | 647 | −1.4% (−8.9 to +6.2) | −0.2% (−0.7 to +0.3) |
| AH | blend | 8% | 142 | +7.2% (−8.6 to +23.3) | +0.8% (−0.4 to +2.0) |
| AH | blend | 12% | 45 | +32.7% (+6.2 to +58.1) | −0.4% (−2.4 to +1.5) |

What the numbers say:
- **The model's own picks lose to the close in every market.** At 12% CLV is −4.2% on
  1X2, −2.9% on O/U and −2.0% on AH. A random AH side at Pinnacle's early price costs
  about half the 2.3% margin (−1.2%), so the model's AH picks are worse than random.
  Thresholds from 2% to 20% give the same CLV.
- **AH blend: CLV is about 0 at Pinnacle's own early price** (−0.2% to +0.8% at 2–8%). This
  is the best result of any match strategy so far, but no range is above 0.
  - The +32.7% ROI at 12% comes from 45 bets, all in 2019/20–2021/22. It is one cell out
    of about 200 (3 markets × 4 prices × 3–4 strategies × 6 thresholds), and its CLV is
    −0.4%. So it is noise, not an edge.
  - Since 2022/23 the blend has found no AH bets at 12%: as the fit converged on the price,
    the large disagreements stopped.
- **Soft prices.** The market average early (a DraftKings stand-in) gives model CLV of
  −6.8% on 1X2, −4.7% on O/U and −3.2% on AH at 12%. The maximum early gives the model −0.3%
  to −0.8% and the AH blend +1.2% to +2.6% (2–8%), but the maximum can't be bet in full
  (round 1).
- **ROI at Pinnacle's close** (CLV 0 by definition): every model and blend range includes 0
  in every market.

**Signals the price might lack (task 2).** Each is a walk-forward blend term on the same
matches. The figure is the change in log loss against the plain blend; negative means the
signal helps.

| Market | Signal | Change in log loss (95% range) | Note |
| --- | --- | --- | --- |
| 1X2 | xG form (6-match xG difference) + xG minus goals | +0.0004 (−0.0012 to +0.0018) | repeat of the edge-finder's test |
| O/U 2.5 | xG in play + xG minus goals (totals) | +0.0004 (−0.0034 to +0.0042) | repeat |
| AH | xG form + xG minus goals | +0.0003 (−0.0008 to +0.0015) | new market, same window, no tuning |
| AH | soft_vs_sharp (average vs Pinnacle 1X2, early) | +0.0001 (−0.0000 to +0.0002) | the edge-finder's suggestion |

None helps out of sample. Two strategies show positive ROI with negative CLV, which marks
it as noise:
- O/U blend with xG at 12%: +15.9% (−0.2 to +32.0) on 171 bets, CLV −2.3%.
- AH blend with soft_vs_sharp: the same 45 bets as the plain blend.

Late team news can't be tested: the FPL log on `data-log` (`fpl_news/`) starts on
2026-10-05, and no Premier League match has been played since. Any `x_*` column on the
joined frame becomes a blend variant once a few hundred settled matches carry it.

**Verdict.** Asian handicap and over/under 2.5 behave like 1X2. The model adds no
information to Pinnacle's price, its own picks lose 2–4% CLV, and the blend does no better
than break-even against the sharp close. Don't add AH or O/U bets to the live rule. The
only near-zero CLV is at Pinnacle's own price, so a bettor gains nothing over the margin.
To test late team news, live prices must first be logged with timestamps (the
edge-finder's M2), so December's matches can be replayed.
