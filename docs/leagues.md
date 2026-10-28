# More leagues: live odds and what they cost in Odds API credits

The owner turned on live odds for every league on 8 Oct 2026: La Liga (SP1), Bundesliga
(D1), Serie A (I1), Ligue 1 (F1) and the EFL Championship (E1) now fetch DraftKings odds
beside the Premier League (E0), whose behaviour hasn't changed. League One (E2) and League
Two (E3) followed on 13 Oct ("League One and League Two live" below). Historical DraftKings odds
for the new leagues are not approved (cap 0), so they have no DraftKings backtest. This
page shows what each league costs. **No credits were spent to work this out.**

The Odds API key is shared with the baseball app. The balance was about 22,600 on 7 Oct.

## How a league is switched on (or off)

1. Set `live=True` (or `False`) for the league in `src/soccer_stats/leagues.py` (the
   `LEAGUES` registry) and merge. All eight football-data leagues are `True` now; the
   three UEFA cups are registered `False` (priced only).
2. Each publish run then builds that league's fixtures with its own match model, fetches
   its DraftKings odds by the refresh rule below, and logs them to
   `odds_log/<code>_<YYYY-MM>.jsonl`. The app gets every fixture tagged with its league.
3. **Paper trades for that league stay off** until it has its own learned minimum edge:
   `backtest/<code>_dk.json` → `edge_threshold.min_edge` on `data-log`, or else the research
   lab's committed level (`src/soccer_stats/lab/min_edge.json` → `leagues[code]`, learned on
   Pinnacle prices). The lab reports null for all five (SP1, D1, I1, F1 and E1;
   `docs/lab.md`). A DraftKings level for the league needs its own DraftKings backtest, which
   needs historical DraftKings odds, which cost credits. That's a separate decision. With
   no level, the league shows fixtures and odds only.

## Refresh rule

One refresh is one call: h2h from one bookmaker, which costs 1 credit. Until 10 Oct it also
asked for `totals` and cost 2: DraftKings lists no main `totals` market for soccer on The
Odds API (the market probe's key discovery, run 37889595473: only `alternate_totals`, which
the bulk endpoint can't return), so the call was billed for a market that never came back
(0 over/under rows in October's odds log). The budget still counts each refresh at 2
credits (`odds_feed.BUDGET_COST`), so at any balance the refresh rhythm is exactly what it
was; the leagues still never refresh more often than the floors below.

| League | Rule |
| --- | --- |
| Premier League (live) | Unchanged: every hour, around the clock, and every 30 minutes within 2 hours of a kickoff (budget permitting). |
| SP1, D1, I1, F1, E1, E2, E3 | Nothing unless the league has a match within 48 hours. Then every 3 hours, hourly from 6 hours before kickoff, and every 30 minutes in the last 2 hours. |

### Who gets the budget when credits run low

- **The Premier League budgets as if it were alone** (share 1). With a healthy balance
  that changes nothing: at about 22,600 credits even a six-way split leaves ~1,900 calls
  per league for the month, far more than the hourly floor needs, so every league sits at
  its floor. It matters only when credits run low: at 3,000 credits an even six-way split
  would stretch E0 from hourly to about every 2 hours. Priority keeps it hourly.
- **The other leagues split the balance eight ways (six until 13 Oct) and keep 3,000
  credits in reserve**
  (`odds_feed.MATCHDAY_RESERVE_CREDITS`, the same reserve the player-odds fetches keep).
  Below that they stop fetching and keep their cached odds. They read the freshest balance
  any league has seen (the key is shared, and E0 refreshes most often), so they pause
  even when they haven't fetched for days.
- **E0 keeps the original 20-credit floor** (`RESERVE_CREDITS`), as before. The new
  leagues stop long before it, so they never eat into what E0 or the baseball app needs.

## Publish run time

Each publish fits one Dixon-Coles model per live league: about 0.02 seconds each. The
data files are cached between runs (`raw-data-*`). Finished seasons never re-download,
and the live season's football-data and Understat files refresh every 12 hours. Before
this change the "Build site" step took about 5 seconds on a warm cache (E0 only). With six
leagues, expect about 10-20 seconds on a warm cache, and up to about a minute on the first
run or every 12 hours, while the five new leagues' files download. For comparison, the
`estimate_month` step (`backfill.yml` run 37841834127) downloads every league's
Understat season and finished in 16 seconds, `uv sync` included. That is well inside the
15-minute schedule, so fits aren't cached. A league that fails to load publishes no cards
and says why; the others still publish.

## Estimated cost per month

`soccer-stats estimate-credits --month YYYY-MM` replays the scheduled publish runs over
that month's fixtures (from Understat) under the rule above. You can also run it with
*Backfill DraftKings odds* → `estimate_month`, which needs no key and spends nothing.

The figures are upper bounds: GitHub delays or skips some scheduled runs, so real use is
lower. The table was made at 2 credits a call; since 10 Oct a call costs 1 (h2h only), so
the calls are unchanged and the credits halve (last column).

**October 2026** (most kickoff times fixed; `backfill.yml` runs 37841834127 and 37843392959):

| League | Matches | Calls | Credits / month (2 a call, until 10 Oct) | Credits / month (1 a call) |
| --- | --- | --- | --- | --- |
| Premier League | 38 | 774 | **1,548** | **774** |
| La Liga | 36 | 310 | 620 | 310 |
| Bundesliga | 36 | 207 | 414 | 207 |
| Serie A | 43 | 311 | 622 | 311 |
| Ligue 1 | 36 | 224 | 448 | 224 |
| Championship (October 2025 calendar*) | 53 | 198 | 396 | 198 |
| **All five new leagues** | | | **2,500** | **1,250** |
| **All six** | | | **4,048** | **2,024** |

\* Understat has no Championship schedule, and football-data lists only played matches,
so the Championship is priced on the same month a season earlier. It has the most
matches (24 teams, midweek rounds) but costs the least, because each round kicks off
together: Saturday 15:00 and midweek 19:45 UK time.

I also ran November and December 2026 (runs 37841726640 and 37841730546). The four
leagues came out at only 126–156 credits a month. Their schedules for those months still
use placeholder kickoff times, so all of a round's matches look like one kickoff. Treat
October's figures as the realistic ones. My synthetic "typical month" calendar (four full
rounds, usual kickoff slots) gave 478–720 credits per league, in line with October.

## What that means for the balance

- **Premier League alone (until 8 Oct):** about 1,500 a month, so roughly 15 months on
  22,600 credits before counting the baseball app.
- **All six leagues (live from 8 Oct):** about 4,050 a month at 2 credits a call; about
  2,025 a month from 10 Oct at 1 credit a call (h2h only), so roughly 11 months on
  22,300 before team totals (~340 a month) and the baseball app. The new leagues stop at
  3,000 credits, so E0 then runs on alone.
- **One extra league:** about 400–620 a month. Serie A and La Liga cost the most, with
  more matches spread over Friday to Monday. The Bundesliga is cheapest, because most of
  its round kicks off at the same Saturday time.

Ways to spend less, if needed (none are built yet):
- refresh the new leagues only from 24 hours out instead of 48 (saves roughly a third);
- (done 10 Oct, all leagues) drop the totals market, which halved each call to 1 credit.

## Things to check on the first live runs

- **Team names.** The odds feed maps The Odds API's names to football-data's (`TEAM_NAMES`
  in `odds_feed.py` and a unique-prefix match). Names only mapped for English clubs so far
  may leave some fixtures without odds. The publish log prints each league's fixture
  count and odds source; look for fixtures missing odds and add the spellings.
- **xG team names.** The research lab found Understat xG matching only 89–90% of 2025/26
  La Liga and Bundesliga matches: probably a promoted club or two spelled differently
  (`xg.TEAM_NAMES`). Unmatched matches fall back to goals in the fit, so nothing breaks,
  but check the publish log's xG coverage for each league once it's live.
- **Team news and the blend.** FPL team news and the model+price blend (`p_bet`) are
  Premier League only. Other leagues use the raw model's chance.
- **The Championship has no xG.** Understat doesn't cover it, so it runs the goals-only
  model (the research lab's round-5 bake-off also used goals only for E1). Its upcoming
  fixtures come from football-data's fixtures file, which covers about the next week.

## Fast settlement from ESPN (18 Oct, lead; 0 credits)

The owner saw many paper trades still open after their matches ended. Domestic match and
corner trades settled only once football-data posted results: data-log's ledgers showed a
median of 41 h after kickoff for match trades (13–67 h) and 37 h for corner trades
(12–39 h), against about 3 h for the Champions League, which already settled from ESPN.

What changed (`espn_results.py`, `paper.awaiting_results`, `cli.espn_settlement`):

- Each paper step lists the live match and corner trades open past kickoff (within 14
  days) that football-data can't settle yet, and asks ESPN's free scoreboard only for those
  leagues (all eight domestic slugs in `espn_news.SLUGS`) and kickoff days, from 1 h 45 min
  after kickoff. A match counts as final when ESPN marks it completed, its status isn't
  postponed, abandoned, cancelled, suspended, forfeited or delayed, and the 90-minute score
  is known (`cups.parse_events`).
- Names are mapped to the trades' football-data names with `espn_news.team_name` against
  the league's clubs (football-data's results plus the trades' names). An event settles a
  trade only if both names map exactly, home and away the right way round, the kickoff is
  within 3 h, and no other event that day fits or lands on the same club. Otherwise the
  trade stays open and the log lists it ("no ESPN match", "ambiguous ESPN match").
- Corner trades read the final match summary's `wonCorners` per team (by ESPN team id,
  then name, then home/away). No counts: corners wait for football-data's HC/AC.
- Settlement order: football-data first when it already has the result (it also carries
  Pinnacle's close for `clv_pinnacle`), else ESPN. Every settle event records
  `result_source` (`"football-data"` or `"espn"`; the Champions League `"espn"`).
- **Later check (rule):** when football-data's result arrives for a trade ESPN settled, one
  update event adds `result_check` (`"ok"` or `"mismatch"`), football-data's `check_score`
  and, for match trades, Pinnacle's CLV. ESPN's settlement stands; a mismatch prints a
  "Result check" or "Corner check" line in the publish log. Corner counts can differ
  between data providers by a corner now and then, so watch the corner mismatch rate in the
  first weeks; if it is material, the lead can switch corner settlement back to
  football-data only.
- Budget per run: 60 s, 3 failed requests (after one retry each) or 80 requests, 10 s
  timeout. Scoreboard days with every match final, or two days past, and summaries with
  both counts are cached for good in `data/raw/espn_results/` (the `raw-data-*` cache).
  Any failure only means those trades wait for football-data; the ledger write never
  depends on ESPN. Log line: "ESPN results: N of M matches final (C with corner counts),
  R requests (F failed)".
- **Settlement runs:** publish runs hourly and every 15 minutes from 10:00 to 21:59 UTC,
  but GitHub throttles scheduled runs (gaps up to 6.5 h on 10–12 Oct). `close_runs.plan`
  therefore adds one publish dispatch 2 h 15 min after the last kickoff of each cluster
  (kickoffs up to 12 min apart), skipped when a close run starts within 20 min after that
  time, and the planner hands over to a new planner run while a settlement is still due.
  A dispatched publish spends Odds API credits only where a refresh is due anyway:
  DraftKings refreshes follow each league's refresh floor and budget
  (`odds_feed.fetch_odds` downloads only when the interval has passed), team-total and
  corner snapshots are recorded per event and snapshot, so they are never bought twice,
  and player lines keep their own per-event refresh interval. The credit estimates already
  assume every scheduled run happens, so these runs stay inside them; against today's
  throttled schedule a settlement run can at most take a refresh that was due (about 1
  credit per league with a kickoff in the next 48 h, and only if its floor has passed).

## Backtests in every live league (21 Oct, owner; 0 credits)

The owner saw no backtest for League One, League Two or the Champions League, and Model
12% had one only for the Premier League (`E0_dk.json`, DraftKings' own prices, 2025/26).
Both live match strategies are now replayed in every live league (`edge12_backtest.py`,
`lean_backtest.py`, `cup_backtest.py`; `backfill.yml` `edge12_backtest` and
`lean_backtest`, no key). `E0_dk.json` stays as it is, named in each file's
`draftkings_replay`.

- **Domestic leagues** (`kind: "early"`): the model's walk-forward chances at Pinnacle's
  early quoted price from football-data, $1 a bet, CLV against Pinnacle's margin-free close,
  every season both prices cover 90% of the matches (2019/20–2024/25; 2025/26 is about half
  priced and left out). Model 12% = the live rule on the model's own chance; Blend Lean as
  before (`lean_backtest`). Pinnacle's early price is sharper than DraftKings', so this is a
  tougher test than the live one.
- **Champions League** (`kind: "close"`): no early cup prices exist, only the closes bought
  on 13 Oct, so both rules bet DraftKings' close on the cross-league model's weekly
  walk-forward chances (2024/25–2025/26; clubs the model doesn't cover and low-data matches
  skipped, as live), CLV against Pinnacle's close at the same moment. That CLV shows how far
  DraftKings' close sits from Pinnacle's, not price movement. Kept out of the pooled numbers.

### Results (branch runs, printed only: `backfill.yml` 39627296311 Model 12%, 39627301375 Lean)

Model 12%, $1 a bet, ROI and CLV with 95% ranges:

| League | Bets | Won | Break-even | ROI | CLV |
| --- | --- | --- | --- | --- | --- |
| Premier League | 1,138 | 24.5% | 24.9% | +4.1% (−8.5% to +18.5%) | −4.2% (−4.9% to −3.5%) |
| La Liga | 930 | 24.2% | 25.2% | −5.7% (−18.6% to +7.6%) | −4.7% (−5.6% to −4.0%) |
| Bundesliga | 627 | 23.8% | 23.7% | +3.9% (−13.6% to +21.7%) | −4.4% (−5.3% to −3.5%) |
| Serie A | 856 | 19.3% | 24.4% | −23.4% (−35.4% to −11.4%) | −4.8% (−5.6% to −3.9%) |
| Ligue 1 | 851 | 22.2% | 24.6% | −4.2% (−18.0% to +11.4%) | −4.4% (−5.3% to −3.6%) |
| Championship | 1,666 | 31.1% | 32.2% | −4.3% (−11.8% to +3.3%) | −4.5% (−5.0% to −4.1%) |
| League One | 1,443 | 30.1% | 32.4% | −9.1% (−17.4% to −0.6%) | −4.4% (−4.9% to −4.0%) |
| League Two | 1,399 | 29.2% | 31.3% | −5.9% (−13.7% to +2.5%) | −5.3% (−5.8% to −4.9%) |
| **Pooled (early)** | **8,910** | 26.6% | 28.4% | **−5.7% (−9.7% to −1.6%)** | **−4.6% (−4.9% to −4.4%)** |
| Champions League (at the close) | 163 | 20.9% | 23.0% | −2.9% (−34.3% to +30.8%) | −9.4% (−10.4% to −8.5%) |

Every league's CLV range is below 0; the pooled ROI range is too. The 2019/20–2020/21 seasons
were the kind ones (pooled ROI −1.5% and +4.8%); the last two seasons lost 13.7% and 12.1%.

Blend Lean (1σ+), same layout (domestic leagues as the 10 Oct file, refreshed cache; League
One and League Two new):

| League | Bets | Won | Break-even | ROI | CLV |
| --- | --- | --- | --- | --- | --- |
| Premier League | 351 | 26.5% | 27.7% | −4.2% (−22.6% to +15.3%) | −3.1% (−4.0% to −2.1%) |
| La Liga | 360 | 43.6% | 44.6% | −2.1% (−14.7% to +11.3%) | −0.8% (−1.3% to −0.3%) |
| Bundesliga | 55 | 23.6% | 30.8% | −24.1% (−61.3% to +21.7%) | −1.8% (−4.8% to +1.5%) |
| Serie A | 274 | 54.7% | 51.5% | +5.4% (−6.7% to +18.0%) | −3.6% (−4.6% to −2.6%) |
| Ligue 1 | 431 | 43.4% | 44.3% | −2.5% (−14.2% to +8.8%) | −2.8% (−3.5% to −2.0%) |
| Championship | 412 | 24.3% | 24.0% | +1.4% (−16.8% to +20.5%) | −4.0% (−5.1% to −3.0%) |
| League One | 518 | 52.7% | 55.8% | −5.8% (−14.3% to +2.4%) | −3.8% (−4.4% to −3.3%) |
| League Two | 228 | 42.1% | 41.0% | −1.5% (−17.6% to +14.5%) | −4.1% (−5.1% to −3.2%) |
| **Pooled (early)** | **2,629** | 40.7% | 41.4% | **−2.3% (−7.6% to +3.1%)** | **−3.1% (−3.4% to −2.8%)** |
| Champions League (at the close) | 0 | | | | |

League One's Lean picks are mostly short-priced favourites (avg odds 1.88, 443 of 518
market-driven); League Two's mostly model-driven (152 of 228). Pooled Strong (2σ+) 186 bets,
ROI −8.8% (−24.1% to +7.2%).

**Champions League Lean: 0 bets.** The blend needs 300 earlier matches with a model chance
and Pinnacle's close before its first fit; there are 302 in the two bought seasons, so the
first 28-day block with 300 behind it starts on 26 May 2026 (σ 0.0493) and holds 1 of the 295
candidate matches, which doesn't reach 1σ. `MIN_ROWS` was not lowered. A Champions League Lean
backtest needs more bought seasons (or must wait for 2026/27's live closes).

Coverage, Champions League: 2024/25 189 finished matches, 150 with a model chance (not
low-data), 180 with DraftKings' close, 183 with Pinnacle's, 143 candidates; 2025/26 189,
157, 184, 189, 152.

## Blend Lean odds range (24 Oct, owner; 0 credits)

**The rule.** From `trades.LEAN_RANGE_FROM` (2026-10-24 07:32 UTC, the commit that adds it),
Blend Lean opens a trade only when the picked side's DraftKings decimal odds are in
1.50 < odds <= 3.00 (`trades.LEAN_ODDS_RANGE`; the backtests' band convention, so +200 =
3.00 stays in and -200 = 1.50 is out). The pick itself is unchanged: the home/draw/away side
with the highest z, at z >= 1. When that side is outside the range, no Lean trade opens
for the match; the rule does not fall back to another side. New Lean trades record
`odds_range` and `rule_version: 2` (older trades have no `rule_version` and read as version
1, any odds). Model 12% and both corner tracks are unchanged, and so is the Lean backtest
(`lean_backtest.json` still replays the old rule).

**The owner's reasons.** "+200 or longer: they lose too often." "-200 or shorter: they
happen too little and the payout isn't worth it."

**The evidence at decision time.**
- Live paper (data-log at 24 Oct 00:34 UTC, 22 Lean trades, 15 settled): <=1.50 had 4
  trades (2 of 3 settled won, break-even 73%); 3.00-5.00 had 11 (1 of 8 won, break-even
  26%, ROI -45%, 95% range -100% to +65%). Far too few trades to tell anything apart.
- Backtest (`lean_backtest.json` of 23 Oct, eight domestic leagues pooled, Pinnacle's early
  price), CLV by odds band: <=1.50 -1.9% (232 bets), 1.50-2.00 -2.7% (632), 2.00-3.00 -3.1%
  (712), 3.00-5.00 -3.8% (790), >5.00 -3.0% (261). Every band is below 0, and the shortest
  band is the least bad, so the backtest gives no support for cutting the short end; the
  long end is a little worse than the middle. ROI ranges include 0 in every band.

**Tracking.** A pick that clears 1 sigma outside the range writes a shadow entry instead of a
trade: the same fields plus `shadow: true` and `excluded_by: "odds_range"`, id = the Lean id
plus `|shadow`, in its own file `paper_trades/<code>_lean_shadow_<season>.jsonl`, never the
real ledger. A shadow opens only while its match has no Lean trade and no shadow yet (the
old rule bet once per match). Shadows close, get CLV and settle through the same
`update_ledger` code, but never count in the real P/L or Moneyline's combined summary. The
Lean live section gets the variant "Blend Lean, all odds (old rule)" (`lean_all_odds`) =
every real Lean trade plus the shadows (a real trade on a match that already has a shadow
is left out of it: the old rule had bet the shadow). Its `since_defined` compares, on
trades opened from `LEAN_RANGE_FROM` under the new code (`rule_version` 2 and shadows; a
trade the old code opened in the window before the merge reached the publish job is
counted as `old_code` and left out), the live rule's summary with the old rule's, and the
CLV difference in range minus all on `clv_dk` (the judged measure; `clv_pinnacle` beside
it), on the summaries' won/lost trades, ranges resampling matches. Above 0 means the cut
helps. This lets us see whether the cut helps instead of guessing; nothing changes again
unless the owner decides.

**Backcast (owner, 24 Oct).** The new rule's line, "Blend Lean (new rule)", runs from Lean's
first trade: before `LEAN_RANGE_FROM` it is the Lean trades already inside the range (the
section's `backcast`: count, settled, first and last opened, `lean_started`), after it the
trades the rule opens. The filter only removes trades, so nothing new is opened and the
ledger keeps every trade: the out-of-range ones stay in the old rule's line and in
Moneyline's combined record. The old rule's line is every real Lean trade plus the shadows.
`since_defined` stays the judged part; `before_defined` on the old-rule section gives the
pre-rule period for both lines, descriptive only, since the cut was chosen after seeing it.
At the decision (data-log, 24 Oct):

| Lean trades before the rule | Trades | Settled | Won–lost | Profit | ROI | CLV (DraftKings) |
| --- | --- | --- | --- | --- | --- | --- |
| All odds (old rule) | 22 | 15 | 5–10 | −$46.90 | −31% | −3.3% |
| 1.50–3.00 (new rule, backcast) | 7 | 4 | 2–2 | −$7.50 | −19% | −1.0% |
| Cut (outside the range) | 15 | 11 | 3–8 | −$39.40 | −36% | −4.2% |

The backcast CLV difference, in range minus all, is +2.4% (95% range −1.2% to +7.5%):
descriptive, on the trades the cut was picked from.

## Kelly sizing tracked beside flat $10 (24 Oct, owner; 0 credits)

**Principle.** Kelly needs a true edge, and every strategy's CLV is below 0, so Kelly on the
model's own claimed edge would over-bet exactly where the model is most over-confident.
Kelly is therefore a tracked alternative sizing over the same trades: no new bets, and flat
$10 stays the record (`sizing.py`).

**Defaults** (stated in every `sizing` block so the owner can change them): a $1,000 paper
bankroll at each strategy's first trade; quarter Kelly (`KELLY_FRACTION` 0.25) with the stake
capped at 5% of the bankroll (`KELLY_CAP`), nothing when f* <= 0; f* = (p x d - 1) / (d - 1)
for the decimal odds d taken and a calibrated chance p:
- Blend Lean: `p_bet` (the blend).
- Model 12%: the blend's chance for the trade's side (`p_blend`), recorded at open from 24 Oct;
  older trades get it once from the logged DraftKings row they opened on (its `p_bet`, else the
  league's current coefficients on its fair price and model chances: `p_blend_from`
  "odds_log" / "recomputed"). A second block, "Kelly on the raw model (over-confident)", sizes
  on the raw `model_p` to show why it isn't used.
- Corners: model (f)'s chance given no push; model (f) is over-confident (slope about 0.56), so
  this is illustrative.

**Path.** Sequential: a stake comes from the bankroll at the trade's `opened_at`, which holds
only trades settled by then; P/L lands at `settled_at`; pushes and voids return the stake.
Backtest bets are decided 4 days before kickoff and settle that day (the Champions League at
the close). Each block: bankroll now, profit, ROI on staked, max drawdown ($ and share of the
peak), growth per bet (log of end over start bankroll per settled bet), bets staked and at
zero stake, median stake and the curve (at most 200 points).

**Live at the start** (data-log, 24 Oct; $1,000 each):

| Strategy | Trades | Flat $10 | Quarter Kelly | Kelly staked / zero |
| --- | --- | --- | --- | --- |
| Blend Lean (new rule) | 7 | -$7.50 | +$1.08 | 7 / 0 |
| Blend Lean, all odds (old rule) | 22 | -$46.90 | -$21.86 | 22 / 0 |
| Model 12% (blend chance) | 175 | -$142.30 | -$40.00 | 25 / 150 |
| Model 12%, Kelly on the raw model | 175 | -$142.30 | -$340.95 | 175 / 0 |
| Corners vs Pinnacle | 72 | -$6.20 | -$114.66 | 72 / 0 |
| Corners vs Pinnacle, below 60% | 28 | +$43.20 | +$125.06 | 28 / 0 |
| Corners vs DraftKings | 9 | -$2.00 | -$12.40 | 9 / 0 |

Model 12%'s blend chance came from the logged row for 138 trades (the fit live when each opened)
and is missing for 8 (four Premier League trades of 5 Oct, before the odds log, and four
Champions League ones whose logged row can't give one: stake 0, counted). For 29, opened on
10 Oct at 09:20, the logged row has no blend chance and the first `match_blends.json` on data-log
is of 10 Oct 20:08, so no earlier fit exists: their chance comes from the current fit and is
flagged `p_blend_from: "approx_current_fit"` (a little look-ahead; the coefficients barely
moved between the three versions of 10 and 13 Oct). On the blend's chance Kelly
stakes only 25 of the 175 Model 12% trades: on the other 150 the blend sees no edge at all.
Kelly on the raw model more than doubles the flat loss (max drawdown 36%), the over-betting
the principle warns about. Corners on model (f) lose more under Kelly than flat for the same
reason. The backtests' sizing is in both backtest files (`sizing`; branch runs below).

**Flat is not held to the bankroll.** The flat line stakes $10 on every bet whatever the
bankroll, so its profit equals the record's; it can run below $0, and its max drawdown then
reads over 100% (it would have gone broke). Kelly stakes a share of what is left, so it never
goes below $0.

**Backtests** (branch runs of `backfill.yml` on 24 Oct, printed only: 40077939042 Lean,
40077940825 Model 12%; every bet in time order, decided 4 days before kickoff at Pinnacle's
early price, one $1,000 bankroll for the pooled domestic leagues):

| Pooled, eight domestic leagues | Bets | Flat $10 | Quarter Kelly | Kelly on the raw model |
| --- | --- | --- | --- | --- |
| Blend Lean (old rule, all odds) | 2,629 | $420 (-$580; drawdown 74%) | $257 (-$743; drawdown 80%) | - |
| Model 12% | 8,909 | -$3,995 (-$4,995; broke) | $666 (-$334; drawdown 50%) | $0.07 (broke) |

Growth per bet (log of end over start bankroll): Lean flat -0.0003, Kelly -0.0005; Model 12%
Kelly -0.00005, Kelly on the raw model -0.0011. Lean's Kelly stakes every bet (median $6);
Model 12%'s Kelly on the blend's chance stakes 1,840 of the 8,909 (median $2.25): on the rest
the blend sees no edge.

| League | Lean flat | Lean Kelly | 12% flat | 12% Kelly (staked) | 12% Kelly, raw model |
| --- | --- | --- | --- | --- | --- |
| Premier League | -$147 | -$128 | +$467 | -$31 (516) | -$742 |
| La Liga | -$61 | -$119 | -$532 | -$16 (181) | -$882 |
| Bundesliga | -$133 | -$187 | +$243 | -$61 (234) | -$126 |
| Serie A | +$148 | +$164 | -$2,000 (broke) | +$20 (63) | -$988 |
| Ligue 1 | -$109 | -$186 | -$359 | -$95 (263) | -$832 |
| Championship | +$56 | -$190 | -$712 | -$176 (571) | -$984 |
| League One | -$302 | -$468 | -$1,295 (broke) | -$9 (12) | -$987 |
| League Two | -$33 | -$17 | -$807 | $0 (0) | -$990 |
| Champions League (close) | 0 bets | 0 bets | -$38 | $0 (0: no blend chance) | -$279 |

What it says: Kelly does not rescue Lean (its edge is not real, so larger stakes on larger
claimed edges lose more, in five of the eight leagues); for Model 12%, Kelly on the blend is
mostly "don't bet", which is why it loses far less than flat; Kelly on the raw model loses 74% or
more of the bankroll in seven of the eight domestic leagues (all but the Bundesliga), the
over-betting the principle warns about. Flat $10 stays the record.

## Draw no bet tracks (26 Oct, owner; 0 credits)

The owner asked for every moneyline bet to be draw no bet (DNB) "unless you found evidence to
not do that, or maybe track both". The 24 Oct backtest (`docs/lab.md` "Draw no bet", run
40140494754) is that evidence. DNB lost to Pinnacle's close about as much per dollar as 1X2:
−4.8% vs −4.6% for Model 12%, −3.2% vs −3.1% for Lean. It won more often and swung less. So both
are tracked: **the 1X2 trades stay the record**, and each match strategy gets a DNB paper track
beside it. The tracks are descriptive, and nothing changes live unless the owner decides.

**Rules** (`paper_dnb.py`, `trades.dnb_*`):

| | Model 12%, draw no bet (`edge12_dnb`) | Blend Lean, draw no bet (`lean_dnb`) |
| --- | --- | --- |
| Chance | the raw model's, without the draw: p_side / (p_home + p_away) | the blend's `p_bet`, without the draw |
| Rule | edge = chance × DNB price − 1 ≥ 12% (per stake not refunded) | z = (chance − 1/DNB price) / σ_DNB ≥ 1 |
| Odds range | none | 1.50–3.00, on the **DNB** price; no other side is tried |
| σ | – | `sigma_dnb` per league in `match_blends.json` (`fit-match-blends`); none = no trades |

- **Price:** DraftKings' h2h that is already fetched (no new market, 0 credits). DNB = side ×
  (1 − 1/draw): back the side and cover the draw at the same book. DraftKings' own DNB price may
  carry a bigger margin than this synthetic one. A check of its real DNB price would cost a credit
  or two and needs the owner's OK.
- **Gates:** the same as the parent: DraftKings odds at most 3 h old, before kickoff, not
  `low_data`, all three prices quoted. $10, one trade per match and track, chosen independently of
  the parent's pick.
- **Settlement:** a draw refunds the stake (`status: "void"`, `push: true`), so ROI counts only bets
  that weren't refunded.
- **CLV:** against DraftKings' DNB close (the last logged h2h before kickoff, margin removed with
  Shin), and against Pinnacle's DNB close once football-data has the match. Each is also given per
  $ staked (`clv_dk_staked`, `clv_pinnacle_staked`: × the close's no-draw chance), which compares
  directly with the parent's CLV.
- **Ledger:** the tracks keep their own ledger (`paper_trades/<code>_dnb_<season>.jsonl`), out of
  `load_ledger`, the record summaries and Moneyline's combined totals.
- **In data.json:** each track is a section in Moneyline's `live.strategies`, after its parent and
  that parent's other variants (`variant_of`, `adds: "dnb"`, `compare`). The trades are in the live
  section's `dnb_trades`.
- **Backcast from 14 Oct** (owner rule, 28 Oct: a new strategy or track starts on the original
  portfolio's date). `paper_dnb.backcast`, through the shared `backcast.py`, replays both tracks
  from 14 Oct up to `DNB_FROM` on the DraftKings odds log. Each logged h2h row is what a publish
  saw at its `logged_at`: the prices, the quote time, and the model's `p` and `p_bet` then. The
  live gates and rules run on each row in time order: odds downloaded at most 3 h before, kickoff
  ahead, all three prices, Model 12% on the logged raw `p`, and Lean on the logged `p_bet` with the
  odds range on the DNB price. So a trade opens at the first logged moment its rule fires.
  - `sigma_dnb` didn't exist before the 26 Oct refit. Lean's backcast uses that refit's value,
    and its trades say `sigma_from: "26 Oct refit"`.
  - Entries carry `backcast: true` and `backcast_from: "odds_log"`. They close on the odds log's
    last pre-kickoff quote and settle from football-data or ESPN, as live.
  - A match that already holds a live trade on the track is left alone.
  - They are never judged. `compare` counts only trades opened from `DNB_FROM`, and each section's
    `backcast` block (`backcast.block`) gives `applied, started, until, defined_at, trades,
    settled, from, to, note, descriptive: true`, plus `backcast_from` (and Lean's `sigma_from`).
    Lean's new-rule `backcast` block has the same shape, plus `lean_started`.
  - Limits: the log is deduplicated on the price, so a model change with no price change isn't
    its own observation; `low_data` isn't logged, so that gate can't be replayed.
  - A dry run on data-log (28 Oct) opens 109 Model 12% DNB trades (E0 11, SP1 12, D1 6, I1 6, F1 9,
    E1 15, E2 21, E3 15, UCL 14) and 2 Lean DNB trades (F1). Lean's DNB picks are rare on the
    logged prices: none in seven leagues, and E1's only pick is outside the range.

## DraftKings history (28 Oct, lead brief 2; lead-approved, cap 200,000)

The goal is to replay every live strategy at DraftKings' own prices in every league, not
only at Pinnacle's early price, and with Pinnacle's price taken from the same snapshot.

**Coverage probe** (`dk_history.py`, `odds-check.yml` `task=dk-history`, run 40647443891,
400 credits). It bought one ten-book snapshot per league and season, on the first Saturday
of November at 09:00 UTC, walking back from 2024/25:

- The archive is empty before mid-2020: every 2019/20 snapshot had 0 events.
- DraftKings is priced from 2020/21 in E0, SP1, D1, I1, E1 and E2, and from 2021/22 in F1
  and E3. Their 2020/21 snapshots had no DraftKings at all.
- Pinnacle is in the same bodies everywhere except E2 2020/21–2022/23 and E3 2021/22. It
  covers about half of E1's events in 2021/22 and 2023/24.

**Estimate.** A 48 h look plus the close, kickoffs within 15 minutes sharing, ten books at 10
credits a snapshot, covered seasons only: 16,018 snapshots, **160,180 credits**.

| League | Credits |
| --- | --- |
| E0 | 24,460 |
| SP1 | 34,340 |
| I1 | 30,680 |
| F1 | 18,680 |
| D1 | 17,380 |
| E1 | 16,940 |
| E2 | 10,100 |
| E3 | 7,600 |

**The buy** (lead sign-off, 28 Oct). `backfill.yml` with `books=ten`, `league`, `seasons`,
`max_credits` and `dry_run` off, from the default branch, one run at a time:

- E0 first, at cap 25,000, `seasons` 2020-2024. Report its real cost and its DraftKings and
  Pinnacle coverage per season.
- Then SP1, I1, D1, F1, E1, E2 and E3. F1 and E3 start at 2021.

The rules, from `odds_history.backfill`:

- h2h at the live call's ten books, Pinnacle included.
- Every asked book's prices are kept in `data/raw/odds_history/<code>/`, the paid
  `odds-history-` cache. It is archived right after the run (#128) and never bought twice.
- Each season's first 3 snapshots in time order with no DraftKings price stop that season
  (design C's guard). Cached snapshots count toward the guard.
- A call billed over 10 credits stops the run.
- The 3,000 reserve applies (`keep_credits`).
- Each call is budgeted at 10 at least.

The run prints each season's events and how many DraftKings and Pinnacle priced, plus a
`BACKFILL_ODDS_JSON` line. No backtest runs in it, so `E0_dk.json` stays as it is. The
backtests at DraftKings' prices are re-run afterwards (method below); the results follow it.

### Backtests at DraftKings' prices: method (written 28 Oct, before any run)

The lead asked for this on 28 Oct, and it is fixed here before the code runs. The question is
what the two live match strategies would have done at DraftKings' own prices, the book they
trade at, with CLV measured against Pinnacle's close taken from the same snapshot. The
Pinnacle backtests (`lean_backtest.json`, `edge12_backtest.json`) answer the same question at
Pinnacle's early price, a sharper and different book.

- **Matches.** Every match of a league's seasons that are both:
  - scored by the Pinnacle backtests (football-data's Pinnacle early and close cover at least
    90% of the matches, `lean_backtest.coverage`); and
  - bought in the DraftKings history.

  That means 2020/21–2024/25, or 2021/22–2024/25 for F1 and E3, less any season the
  Pinnacle backtests leave out.

  A match is a candidate when DraftKings priced its full 1X2 in the 48 h look snapshot
  (`odds_history.price_at` at kickoff − 48 h, the price updated no more than 6 h before).
  Matches are joined on football-data's names and kickoff day.
- **Model.** The same walk-forward model chances the Pinnacle backtests use
  (`backtest.walk_forward`, xG weight 0.7 where Understat has xG, else goals only, from
  2019-07-01 with 2 seasons of burn-in). This is the same `_early_preds` call with the same
  `--from-season` 2019 as `backtest-lean`. (The first version of this note said 2022-07-01,
  which is the live fit's start, not the backtests'. It was corrected before any run.)
- **Entry.** DraftKings' quoted decimal price at the 48 h look. The break-even is 1/decimal,
  margin kept, as live.
- **Model 12%.** `trades.best_pick` on the raw model chance at 12% against that price, one
  pick per match, $1. This is exactly the live rule.
- **Blend Lean.** The live rule, `trades.lean_pick` at 1σ, "strong" at 2σ, $1, one pick per
  match:
  - The blend is `match_calibration.fit` on Pinnacle's margin-free close beside the model's
    walk-forward chance, refitted every 28 days on matches played before the block. The block
    is set by the look time, kickoff − 48 h. σ comes from each fit's own rows. This is
    `lean_backtest.walk_forward`, as the live fit.
  - The blend is fed DraftKings' Shin-fair look price, as `publish.add_lean` feeds the card's
    `implied`.
  - The driver is the blend with the model term removed.
  - Two lines are reported: the live rule, inside `trades.LEAN_ODDS_RANGE` (1.50 < odds ≤
    3.00), and the old rule at every price.
- **Close and CLV.** The close is the snapshot just before kickoff (`price_at` at kickoff, the
  price updated no more than 6 h before).
  - The judged CLV is entry odds × Pinnacle's Shin-fair close − 1, from Pinnacle's prices in
    that same close snapshot. A bet without Pinnacle there has no CLV and is counted as
    `clv_missing`. There is no fallback to football-data.
  - CLV against DraftKings' own close is reported beside it, descriptive, as live `clv_dk`.
- **Same matches at Pinnacle.** The same two rules on the same candidate matches at
  football-data's Pinnacle early price, with CLV against football-data's Pinnacle close. This
  shows what changes with the book, not with the sample.
- **Reported per league, then pooled once all eight are in:**
  - bets, win rate vs break-even, ROI per $1 with a match-resampled 95% range (2,000 draws,
    seed 0);
  - CLV vs Pinnacle's close with its range, CLV vs DraftKings' close, beat-the-close share;
  - per season, per tier and driver (Lean), and per odds band (`bands.ODDS_BANDS`);
  - DraftKings' coverage per season: candidates out of the season's matches, at the look and
    at the close.
- **Verdict.** A rule shows an edge at DraftKings when the 97.5% end of its pooled CLV range
  vs Pinnacle's close is above 0. This is the same pass rule as the DNB and Pinnacle
  backtests. ROI is reported, not judged: its ranges are wide. Per-league numbers are
  descriptive.
- **Where it runs, and the app** (lead, 28 Oct: the owner wants the results in the app).
  `backfill.yml` input `dk_price_backtest`: every league whose ten-book bodies are in the
  cached `odds-history-`. It uses no key and no credits, and it reruns as each league's bodies
  land. It writes data-log `backtest/lean_backtest_dk.json` and `edge12_backtest_dk.json` in
  exactly the shape of `lean_backtest.json` and `edge12_backtest.json`, built by their own
  `build`:
  - per league: metrics, seasons, odds, edge and CLV bands, calibration and sizing;
  - `all`, and bets newest first.

  Each league entry has `kind: "draftkings"`. In its `coverage` rows, `early_share` is
  DraftKings at the look and `close_share` is Pinnacle's close in the same body. Each entry
  also carries `clv_dk` and `pinnacle_same_matches`. The top level adds `book`, `verdict` and
  `coverage_note`. `paper.run` publishes them as `portfolio.strategy_backtests.lean_dk` and
  `.edge12_dk` through the same `app_section`.

### The buy: results (28 Oct)

All eight leagues were bought from the default branch, one run at a time: **160,180 credits,
exactly the estimate** (balance 4,589,590 after). There were no guard blocks, no call over 10
and nothing bought twice. Each run was archived right after (#128).

| League | Seasons | Run | Credits |
| --- | --- | --- | --- |
| E0 | 2020/21–2024/25 | 40706048261 | 24,460 |
| SP1 | 2020/21–2024/25 | 40711644722 | 34,340 |
| I1 | 2020/21–2024/25 | 40717483089 | 30,680 |
| D1 | 2020/21–2024/25 | 40724126165 | 17,380 |
| F1 | 2021/22–2024/25 | 40726795759 | 18,680 |
| E1 | 2020/21–2024/25 | 40730770419 + 40737491347 | 6,140 + 10,800 |
| E2 | 2020/21–2024/25 | 40741249022 | 10,100 |
| E3 | 2021/22–2024/25 | 40745267032 | 7,600 |

E1's first run stopped on an HTTP 502 from The Odds API after 614 snapshots. The lead
approved a resume, which skipped the cached snapshots (#146 now labels a 5xx as a server
error).

The backfill logs count DraftKings and Pinnacle per event and snapshot, which includes
matches days away from kickoff. At the match level both are much higher: see the coverage
table below.

### Backtests at DraftKings' prices: results (28 Oct)

Final run 40747442368 (data-log 2ef254e; method above, 0 credits). Three name fixes came
first, from the backtest's own unmatched-names list (#140, #147):

- Inter and Verona;
- Darmstadt and St Pauli;
- St Etienne and West Brom.

No feed name is unmatched now.

**Coverage**: the share of each season's matches DraftKings priced 48 h before kickoff, with
the candidate count. Pinnacle's close is in the same snapshot for 100% of them in every
league and season, and no season is partial (`partial_coverage` is empty).

| League | 2020/21 | 2021/22 | 2022/23 | 2023/24 | 2024/25 |
| --- | --- | --- | --- | --- | --- |
| E0 | 100% (367) | 100% (373) | 99% (367) | 100% (368) | 100% (374) |
| SP1 | 99% (364) | 96% (358) | 98% (360) | 99% (372) | 99% (369) |
| D1 | 100% (299) | 98% (289) | 97% (297) | 100% (294) | 100% (293) |
| I1 | 99% (356) | 99% (360) | 97% (353) | 99% (369) | 95% (343) |
| F1 | – | 98% (363) | 98% (353) | 99% (298) | 100% (299) |
| E1 | 99% (522) | 98% (525) | 98% (526) | 100% (522) | 91% (486) |
| E2 | 100% (523) | 94% (498) | 98% (514) | 99% (517) | 100% (513) |
| E3 | – | 97% (509) | 98% (514) | 100% (524) | 100% (521) |

**Pooled verdict: neither live rule shows an edge at DraftKings' prices.** Each pooled CLV
range vs Pinnacle's close is below 0. The ranges are 95%, resampling matches.

| Rule | Bets | Won vs needed | CLV vs Pinnacle's close | ROI per $1 |
| --- | --- | --- | --- | --- |
| Model 12% | 5,324 | 27.0% vs 29.4% | −7.4% (−7.7% to −7.1%) | −8.4% (−13.3% to −3.8%) |
| Blend Lean, all odds | 881 | 37.9% vs 40.4% | −5.8% (−6.4% to −5.2%) | −7.7% (−16.7% to +1.9%) |
| Blend Lean, live rule (1.50–3.00) | 443 | | −5.0% (−5.7% to −4.2%) | −4.0% |

- CLV against DraftKings' own close (descriptive): Model 12% −7.2% (−7.4% to −6.9%).
- Lean by driver:
  - market-driven: 423 bets, CLV −3.7% (−4.5% to −3.0%), ROI −2.4%;
  - model-driven: 458 bets, CLV −7.7% (−8.7% to −6.8%), ROI −12.7%.

  The same order as the Pinnacle backtest: the model's own view costs more.
- Lean ROI by odds band:

  | ≤1.50 | 1.50–2.00 | 2.00–3.00 | 3.00–5.00 | >5.00 |
  | --- | --- | --- | --- | --- |
  | −11.7% | −1.0% | −6.6% | −15.9% | −0.9% |

  Every range spans 0, so the bands are descriptive.

**Per league.** For Model 12% the CLV ranges are shown; every one is below 0. "Pinnacle" is
the same rule on the same matches at football-data's Pinnacle early price.

| League | Model 12% bets | CLV | ROI | Same matches at Pinnacle: CLV (bets) | Lean bets | Lean CLV | Lean ROI | Lean at Pinnacle: CLV |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| E0 | 864 | −6.4% (−7.2% to −5.6%) | −7.7% | −4.4% (968) | 143 | −5.6% | −16.9% | −3.0% |
| SP1 | 431 | −8.5% (−9.7% to −7.3%) | −14.8% | −4.6% (710) | 97 | −1.8% | +17.1% | −0.9% |
| D1 | 311 | −7.4% (−8.7% to −6.0%) | 0.0% | −3.6% (490) | 22 | −3.4% | −45.8% | −0.8% |
| I1 | 427 | −8.4% (−9.5% to −7.3%) | −27.6% | −4.5% (667) | 76 | −6.9% | −7.9% | −3.4% |
| F1 | 462 | −6.6% (−7.6% to −5.5%) | −12.8% | −4.8% (556) | 83 | −4.5% | −7.1% | −3.0% |
| E1 | 1,083 | −6.4% (−7.0% to −5.9%) | −2.5% | −4.6% (1,294) | 187 | −8.9% | −15.4% | −4.0% |
| E2 | 969 | −7.7% (−8.3% to −7.0%) | −8.2% | −4.5% (1,243) | 215 | −4.8% | −1.5% | −3.8% |
| E3 | 777 | −8.7% (−9.4% to −7.9%) | −4.3% | −5.9% (967) | 58 | −8.4% | −11.5% | −4.3% |

**The book costs about 2.7 points.** On the same candidate matches at Pinnacle's early price,
pooled CLV is:

- Model 12%: −4.7% on 6,892 bets, against −7.4% at DraftKings;
- Lean: −3.1% on 2,332 bets, against −5.8% at DraftKings.

League by league, DraftKings is 1.8 to 3.9 points worse for Model 12%. Betting at the book
the strategies trade at widens a loss that already exists at the sharper price; it doesn't
create one. This agrees with the Pinnacle backtests (`docs/leagues.md` "Backtests in every
live league") and with the live paper record.

**No live change.** Both rules stay as they are: Lean live and Model 12% archived. The
backtest files are in the app as `strategy_backtests.lean_dk` / `.edge12_dk` (Portfolio →
Moneyline → Backtest, "at DraftKings' prices"), and their `coverage_note` names any partial
season automatically (#148).

## In-play snapshots (28 Oct, lead brief 2; owner-approved, about 580 credits a month)

A dataset collected from now on for the in-play questions: comebacks, cash-out, and heavy
favourites trailing at half-time. We buy no history for it. Twice a match, one live h2h call
per league records every match in play at ten books, with ESPN's score and clock at that moment.
Nothing trades on it.

- **When.** `closes.yml` already plans the close and settlement runs. It now also dispatches
  `inplay.yml` for each kickoff cluster:
  - at the cluster's first kickoff + 58 min (`inplay55`, half-time);
  - at the first kickoff + 78 min (`inplay75`).

  These come from `close_runs.INPLAY` with a 3-minute shift. A cluster spans up to 12 minutes, so
  each call lands 8 minutes before to 6 minutes after the target for every kickoff in it. That is
  inside the run's own windows (`inplay_log.WINDOWS`: 45–65 and 65–90 minutes after kickoff, no
  overlap).
- **What the run does** (`inplay_log.run`, CLI `log-inplay`):
  - It reads data-log's DraftKings logs (kickoffs) and the in-play call record.
  - For each league with a kickoff in a window that the call record doesn't hold yet, it makes a
    free events call first, which shows whether anything has started and gives the balance.
  - Then it makes one live `/odds` call: h2h at the same ten books as the pre-match call, one
    region, 1 credit.
  - The cost is read from `x-requests-last`. The call keeps the shared 3,000 reserve and stays
    under a monthly guard of 2,000 credits (`MONTHLY_CAP`, against a runaway loop).
- **What is logged** (data-log, permanent):
  - `odds_log/<code>_inplay_<YYYY-MM>.jsonl` has one row per started match and book: league,
    home and away (football-data names), kickoff, kind (the nearest), `minutes_since_kickoff`,
    `checked_at`, book, prices, margin, `last_update`, `age_min`, and ESPN's `espn_score`,
    `espn_clock`, `espn_state` and `espn_goals`. Every match in play goes in, not only the due
    ones.
  - `odds_log/inplay_calls_<YYYY-MM>.jsonl` has one row per call: league, kinds, kickoffs
    covered, `fetched_at`, status, cost, `credits_left`, events, rows.
  - The record is read before every call, so a (league, kind, kickoff) is never snapshotted twice.
  - The run writes to a pending folder and appends to the newest data-log on each push attempt,
    so a push race never repeats a paid call. On a final failure the rows go to a 30-day artifact.
- **Cost.** One credit per league per cluster per kind. The lead's estimate is about 580 a month
  across the nine live leagues.
- **Only the default branch** gets the key and writes to data-log. A run anywhere else reads and
  prints, and buys nothing.

### Odds-only competitions (28 Oct, lead-approved; about 1,100 credits a month more)

Research's 75%+ heavy-favourite idea needs in-play prices from more competitions, going
forward (H2's forward sample). The same snapshots now also cover 34 odds-only competitions
(`inplay_events.COMPETITIONS`):

- the six domestic cups: FA Cup, EFL Cup, Copa del Rey, DFB-Pokal, Coppa Italia, Coupe de
  France;
- the Europa League and Conference League;
- the ten football-data leagues the archive prices in play beyond our eight: N1, P1, SC0, B1,
  T1, G1, D2, I2, SP2, F2;
- heavy_more's sixteen: Austria, Switzerland, Denmark, Norway, Sweden, Poland, Romania,
  Finland, Ireland, Russia, Japan, China, USA, Mexico, Brazil, Argentina.

They are odds only: no model, no paper trades, no cards in the app. The team names are the
feed's own; there is no football-data mapping, and Research's H2 joins them itself.

- **Kickoffs, 0 credits.** We fetch none of their pre-match odds.
  - publish.yml's data-log step runs `log-inplay-events`. It asks the free sports list which
    keys are in season, then each in-season competition's free `/events` list, at most every
    3 hours.
  - New (event, kickoff) pairs go to `odds_log/<code>_events_<YYYY-MM>.jsonl`, by kickoff
    month.
  - Each competition's ask, or its "inactive" status, goes to
    `odds_log/inplay_events_calls_<YYYY-MM>.jsonl`.
  - Every call's `x-requests-last` is checked: a list that bills stops the step.
- **Planning.** `close_runs.plan` gets those kickoffs as `inplay_ks`. They get the +58 and +78
  minute in-play runs and keep the planner going, but never a publish close or settlement run.
- **Snapshots.** These are `inplay_log.run` as for the live leagues: the free events check,
  then one `/odds` call per league, 1 credit. ESPN's score and clock come from each
  competition's own slug.
- **Empty pause** (corners' rule). Our ten books may not price minor leagues in play. If an
  odds-only competition's last 6 answered calls for a kind (`inplay55` or `inplay75`) logged no
  rows, over 2 or more kickoff days, that league-kind makes no call for 7 days after the last
  one (`inplay_log.paused`). The run's summary lists the paused ones.
- **Cost.** About 1,100 credits a month on average and about 1,400 at peak (modelled,
  ±30%):
  - cups about 60;
  - UEFA cups about 30;
  - the ten football-data leagues about 420;
  - heavy_more's sixteen about 590.

  The monthly guard `MONTHLY_CAP` is now 3,000. The real spend per competition is read from
  `inplay_calls_*`, and the first month's is reported in the weekly line.

## Champions League live (13 Oct, owner)

Owner's decision (13 Oct): switch the Champions League on, with both match paper
strategies (Blend Lean and the Model 12% test). The Europa League and Conference League
stay off. `leagues.py`: `UCL` is `live=True`, `cup=True`, "matchday" odds policy,
DraftKings h2h on The Odds API's main key `soccer_uefa_champs_league` (league phase and
knockouts; the qualifying rounds are another key and aren't fetched).

### The model: one fit across Europe's leagues

The domestic fits rate clubs only against their own league, so they can't price Arsenal
against Benfica. `cup_model.py` fits one goals-only Dixon-Coles model (the same model as
the domestic leagues, with time decay) over:

- the domestic matches of the leagues Champions League clubs come from: football-data's
  top divisions (England, Spain, Germany, Italy, France, Scotland, the Netherlands,
  Belgium, Portugal, Turkey, Greece) and its European "extra leagues" (Austria,
  Switzerland, Denmark, Norway, Sweden, Poland, Romania, Finland, Ireland). Every club's
  name carries its league code ("P1:Benfica"), so two clubs with the same name in
  different countries never merge;
- every finished match of the three UEFA cups, qualifying rounds included, from ESPN's
  free scoreboards. A club football-data doesn't cover keeps its own name (by ESPN id), so
  its matches still link the leagues it plays.

Within a league, clubs are tied together by their domestic matches; how strong one
league is against another is set only by its clubs' European results. Only the 730 days
before the prediction time are used, older matches count less (the domestic decay), a
small ridge keeps a club with a handful of matches finite, and the final (a neutral venue)
gets no home advantage. Extra-time matches count with their 90-minute score: from ESPN's
linescores, else from its goal events, trusted only when every goal has a minute and a
team and they add up to the final score (all seven extra-time knockout matches of 2024/25
and 2025/26 were recovered this way). One fit takes about 1.5 s on about 23,000 domestic
and 4,000 European matches.

**No look-ahead.** Every fit uses matches played before its own time only (domestic by
date, European by kickoff). The evaluation refits every Monday 00:00 UTC and predicts
that week's matches; tests check that the fit's last match is before its time and that
rewriting later results changes no prediction.

**Clubs without a model chance.** A club no football-data league covers gets no model
chance: its cards show DraftKings' odds only (`no_model`, `low_data`, so no paper trade of
either strategy opens), the match sheet says why, and the publish log lists the clubs.
2026/27 league phase (ESPN's calendar, run 38457777910): 32 of 36 clubs covered; no model
chance for Sabah (Azerbaijan), Shakhtar Donetsk (Ukraine), Slavia Prague (Czechia) and
Slovan Bratislava (Slovakia). In 2024/25 and 2025/26 the uncovered clubs were Dinamo
Zagreb, FK Qarabag, Kairat Almaty, Pafos, Red Star Belgrade, Shakhtar Donetsk, Slavia
Prague, Slovan Bratislava and Sparta Prague.

### Evaluation (walk-forward, against the bought closes)

`backfill.yml` run 38457777910 (`match_blends`, from the PR branch, printed only, no key,
0 credits). Every 2024/25 and 2025/26 Champions League match (378), the model's chance from
the Monday fit before it, beside Pinnacle's close bought on 13 Oct
(`odds_history/UCL_closes.jsonl`, run 38452265319). 307 matches have a model chance, 372 a
Pinnacle close, 302 both. Log loss (lower is better):

| Matches | Model | Pinnacle close | DraftKings close |
| --- | --- | --- | --- |
| All, 302 | 0.9537 | 0.9449 | |
| 2024/25, 145 | 0.9680 | 0.9524 | |
| 2025/26, 157 | 0.9404 | 0.9381 | |
| With DraftKings' close too, 290 | 0.9606 | 0.9528 | 0.9509 |

The model trails Pinnacle's close, as it does in every domestic league (by 0.009 here;
0.013 to 0.027 in the domestic fits). It is closer in 2025/26, when it had a full season
of the new format behind it. DraftKings' close is as sharp as Pinnacle's on these matches.

**Blend check, evaluation only (not used live):** a blend fitted on 2024/25's 145 rows and
scored on 2025/26's 157: model 0.9404, blend 0.9337, Pinnacle 0.9381. One season of 157
matches is far too few to call that a gain.

### The Blend Lean fit

`fit-match-blends` gains a Champions League entry: `match_calibration.league_fit` on the
model's walk-forward chances beside Pinnacle's bought close, its 300-match minimum
unchanged. **302 fit rows** (145 + 157), just over the minimum. The first run had 295,
because the seven extra-time matches had no 90-minute score; recovering their scores from
ESPN's goal events (not lowering the guard) brought it to 302. Fit (run 38457777910, fit
date 13 Oct): coef [a_draw −0.516, a_away −0.065, b 0.745, c 0.093], σ 0.0475, matches
18 Sep 2024 to 30 May 2026.

Read it with care:
- **b = 0.745 is below 1**: the blend pulls Pinnacle's price toward even (long shots up,
  favourites down), the opposite of La Liga, the Bundesliga, Serie A and Ligue 1
  (b 1.07–1.52). The model's weight c is small (0.09).
- **σ is 4.75 points**, 1.6 to 7.5 times the domestic leagues' (0.6–3.1), so a Lean pick
  needs the blend 4.75 points above DraftKings' break-even. Expect few Lean picks, mostly
  on outsiders.
- `league_fit`'s own walk-forward check covers **1 match** (the rows reach 300 only before
  the final), so it says nothing. The season split above is the only out-of-sample look.
- The rows won't grow by themselves: live runs log DraftKings, not Pinnacle. A refit
  each month changes nothing until more Pinnacle closes are bought.
- If the lead or owner prefers not to trade on a 302-row fit, the lowest sound option is
  to leave the UCL entry out of `match_blends.json` (no Lean picks, the 12% test runs);
  lowering the 300-row guard is not proposed.

**Lean backtest: left out.** `backtest-lean` replays Lean on Pinnacle's early price and
scores CLV against the close. The bought prices are closes only, so a replay could only
bet at the close and its CLV would be zero by construction. `backtest-lean` skips the
cup (its default leagues are the live non-cup ones).

### What runs live

- **Fixtures** from ESPN's `uefa.champions` scoreboard for the next 14 days (football-data
  has no cup files), placeholder knockout ties ("TBD", "Winner ...") skipped. Cards carry
  ESPN's team names.
- **DraftKings h2h** under the matchday rule (nothing without a kickoff within 48 hours,
  then every 3 hours, hourly from 6 hours out, every 30 minutes in the last 2 hours; pause
  below the shared 3,000-credit reserve). The Odds API's names pair with ESPN's by kickoff
  (within 3 hours) and names (`cup_odds.pair_events` and its aliases, seven added from the
  bought closes, and Qarabag's corrected to ESPN's "FK Qarabag"). An event that pairs with
  no fixture is listed in the log ("odds events unmatched").
- **Model chances** from one fit at publish time. The fit needs every European result in
  its window: ESPN days at least two days old are cached for good, nearer ones for 30
  minutes, and one run's cup requests stop after 90 seconds or 3 failed requests
  (`cup_model.Budget`). Until the history is complete, every card shows odds only and says
  the results are still loading. `backfill.yml` `match_blends` saves the ESPN days to the
  `cup-espn-*` cache and `publish.yml` restores it, so the first live runs start warm.
- **Both paper strategies** under the existing rules: the Model 12% test on the raw
  model's chance (`trades.PAPER_RULE = "fixed_raw"`), and Blend Lean once
  `backtest/match_blends.json` has the UCL entry. Ledger `paper_trades/UCL_<season>.jsonl`,
  odds log `odds_log/UCL_<YYYY-MM>.jsonl` (so `closes.yml` sees the kickoffs).
- **Settlement** on ESPN's 90-minute score (`cup_model.cup_results`; `soccer-stats paper`
  never asks football-data for the cup). An extra-time match without a 90-minute score
  waits and voids after 14 days, as any match without a result.
- **ESPN team news** (free): slug `uefa.champions`; the cards' names are ESPN's own, so
  they match as they are.
- **Not there:** no team corners (`corners_live.LEAGUES`) and no team totals
  (`TEAM_TOTAL_LEAGUES`) for the Champions League (test). No Teams tab block (its clubs
  come from many leagues; the tab says so). The Europa League and Conference League stay
  off.

### Cost

`backfill.yml` run 38455236172 (`estimate_month` 2026-10,2026-11,2026-12 with `cups`,
`cup_seasons` 2026; no key): ESPN's 2026/27 Champions League calendar under the matchday
rule at 1 credit a refresh: **Oct 86, Nov 86, Dec 43 credits** (Sep 60, Jan 65; league
phase 144 matches, 340 credits; the knockouts aren't on the calendar yet, so a full season
is about 680, as the 12 Oct estimate said). The other eight leagues' DraftKings refreshes in
the same run: Oct 2,402, Nov 1,569, Dec 1,662. All live spend goes from about 3,540 to about
3,630 a month. The fit and ESPN cost nothing.

### Checks after the merge

1. Dispatch `backfill.yml` on `claude/soccer-stats-scaffold` with `match_blends` ticked
   (other inputs default; no key, 0 credits, about 4 minutes). From the default branch it
   writes `backtest/match_blends.json` with the UCL entry to data-log and saves the ESPN
   days to the `cup-espn-*` cache.
2. Then dispatch `publish.yml`. Its log should show "Champions League: European results
   ... matches (N ESPN requests); M of K fixtures with a model chance; no model chance for
   ..." (no "incomplete"), "Match blends: ... Champions League sigma 0.0475 (302 matches)",
   and, once a kickoff is within 48 hours, "Champions League: K fixtures, odds DraftKings".
3. On a matchday: the first `UCL|2627|...` trades in `paper_trades/UCL_2627.jsonl`, rows in
   `odds_log/UCL_2026-10.jsonl`, no "odds events unmatched" for the Champions League, and
   settlement after the matches.

## League One and League Two live (13 Oct, owner)

Owner's decision (13 Oct, lead session), after the check below: "Switch them on please."
League One (E2) and League Two (E3) are `live=True` in `leagues.py`: goals-only (no
Understat, `understat=None`), the football-data schedule, the "matchday" refresh policy.

What runs there:
- **Live DraftKings h2h odds** under the matchday rule: nothing without a kickoff within
  48 hours, then every 3 hours, hourly from 6 hours out, every 30 minutes in the last 2
  hours. 1 credit a refresh. Both pause below the shared 3,000-credit matchday reserve, on
  the freshest balance any league has seen. Logged to `odds_log/E2_<YYYY-MM>.jsonl` and
  `odds_log/E3_<YYYY-MM>.jsonl` on data-log.
- **Cost:** about 364 credits a month for both (Oct 211 + 167, Nov 183 + 145, Dec 210 +
  176; `backfill.yml` run 38447622203 on the switched-on registry, identical to the 11 Oct
  estimate, run 38178582402). All eight leagues' DraftKings refreshes: Oct 2,402, Nov
  1,569, Dec 1,662 (November and December use placeholder kickoff times in the top
  flights, so October is the realistic month). All live spend, about 3,540 a month:
  DraftKings about 2,390 (2,024 for the six plus 364), team totals about 682, corners
  about 471.
- **Both match strategies**, $10 paper trades, each league's own ledger
  (`paper_trades/E2_<season>.jsonl`, `paper_trades/E3_<season>.jsonl`):
  - the fixed 12% test (`trades.PAPER_RULE = "fixed_raw"`) on the raw model's chance
    against DraftKings' quoted price, from the first publish with E2/E3 odds;
  - Blend Lean (1σ+) once `backtest/match_blends.json` has their fits. Until the refit
    runs from the default branch, `publish.add_lean` finds no E2/E3 entry, their cards get
    no `z` or `lean_pick`, and no Lean trades open there (the rule's σ is null).
- **ESPN team news** (free): slugs `eng.3` and `eng.4`, under the same 120-second and
  3-failure run budget. `MAX_SUMMARIES` goes from 60 to 90: a Saturday's 15:00 UK slot
  alone now has about 35–40 matches (Championship, League One and League Two) asking for a
  summary on every run in their last 90 minutes, beside the 3-hourly refresh of the next
  day's matches; at 60 the league processed last (League Two) could miss its lineups.
- **Not there:** no team corners (Pinnacle lists none; `corners_live.LEAGUES` stays the
  six) and no team totals (FanDuel doesn't list either league;
  `odds_feed.TEAM_TOTAL_LEAGUES` stays the five). The app's Teams tab shows ratings from
  goals (no xG, no player stats), and the match sheet's goals note says these leagues
  weren't in the total-goals test.

Team names (checked before the first publish, free):
- `odds-check.yml` run 38447588309 (`task=markets`, `leagues=E2,E3`, `cap=0`, new
  `--names`): The Odds API's free events list had 12 upcoming events in each league,
  which named all 24 clubs in each. Against football-data's 2026/27 E2/E3 files, three
  spellings didn't land: "Peterborough United" (football-data "Peterboro"), "Wimbledon"
  ("AFC Wimbledon") and "Bristol Rovers" ("Bristol Rvs").
- `odds-check.yml` run 38447592742 (`task=espn`, `leagues=E2,E3`, 21 days of
  scoreboards): ESPN named all 24 clubs in each league; "Peterborough United" and
  "Bristol Rovers" didn't land.
- `odds_feed.TEAM_NAMES` now maps those three and every other long form (e.g. "Wycombe
  Wanderers" → "Wycombe", "Stockport County FC" → "Stockport", "Accrington Stanley" →
  "Accrington"), so all 48 clubs map with or without the season's team list.
  `tests/test_leagues.py` lists every 2026/27 club with both feeds' spellings and checks
  no unmatched name, no two names on one club, and no duplicate card.

Blend fits (`backfill.yml` run 38447596291, `match_blends` from the branch, printed
only; Pinnacle's own close from 2022/23, 2025/26 only partly priced, 2026/27 none):

| League | σ | Matches | coef [a_draw, a_away, b, c] | Walk-forward log loss: model / blend / Pinnacle |
| --- | --- | --- | --- | --- |
| League One | 0.0161 | 1,691 (2022-07-30 to 2025-11-25) | [−0.048, 0.063, 0.957, 0.082] | 1.0301 / 1.0188 / 1.0173 (1,380 matches) |
| League Two | 0.0170 | 1,711 (2022-07-30 to 2025-10-25) | [−0.084, −0.043, 0.996, −0.192] | 1.0806 / 1.0546 / 1.0532 (1,404 matches) |

The other six leagues' fits in that run are identical to the live file's. In both new
leagues b is about 1 (the blend doesn't sharpen Pinnacle's price, unlike the top
flights), and the blend trails Pinnacle's close out of sample, as everywhere but Serie A.

After the merge: dispatch `backfill.yml` on `claude/soccer-stats-scaffold` with
`match_blends` ticked (writes `backtest/match_blends.json` with E2/E3 to data-log), then
`publish.yml`. Check the publish log for "League One: N fixtures, odds DraftKings",
"Duplicate fixtures: 0", no "odds events unmatched" line for League One or Two, the
"Match blends" line naming both, and "Team news (ESPN)" counts.

## League One and League Two check (11 Oct)

Owner's request (11 Oct, lead session): more midweek fixtures. Among our six leagues only
the Championship plays midweek, so League One (E2) and League Two (E3) are candidates.
Owner-approved at **10 credits at most** for the paid part. Nothing goes live from this
check; switching either on is a separate owner decision (taken on 13 Oct: both are
live, see the section above).

What is built:
- **Registry.** E2 and E3 are in `leagues.py` with `live=False`, Odds API sport keys
  `soccer_england_league1` / `soccer_england_league2`, `understat=None` (goals-only, like
  the Championship) and the "matchday" policy. A league that isn't live is never fetched
  (`odds_feed.fetch_odds`), never built (`publish` loops over `live_codes()` only) and is
  left out of `data.json`'s `leagues` list (`publish.league_list`), so the app shows no
  chip and paper trading never sees it. Corners, team totals and ESPN keep their own
  six-league lists. Tests in `tests/test_leagues.py`.
- **Free estimate.** `soccer-stats estimate-credits --month YYYY-MM` prices every
  registry league; E2/E3 print as "off" and are summed apart ("Not live (switching each
  on would add)"). football-data's season files only hold played matches, so, as for the
  Championship in round 7, a month is replayed on the same month of the 2025/26 calendar.
  `soccer-stats estimate-corners --extra-leagues E2,E3` adds the Pinnacle team-corner
  snapshot cost (look + close, 1 credit each), not counted in the live total.
  `backfill.yml` with `estimate_month` = `2026-10,2026-11,2026-12` runs both for every
  month listed (no key, no push).
- **Market probe.** `python -m soccer_stats.edge.market_probe --leagues E2,E3 --coverage`
  (`odds-check.yml` `task=markets`, `leagues=E2,E3`). Per league: the free events list,
  one `/events/{id}/markets` discovery call (1 credit), then one `/events/{id}/odds` call
  for the keys discovery found among `h2h`, `alternate_team_totals_corners` and `totals`
  (dropped in that reverse order if credits are short) at up to ten books, always
  including DraftKings, Pinnacle and FanDuel when listed: 1 credit per market, about 4 a
  league, about 8 in all. `CappedBudget` keeps the cap and the 3,000-credit reserve and
  counts `x-requests-last`; the key is never printed. The report lists books and keys,
  two-sided pairs and margins (h2h: sum of 1/price − 1 over home, draw and away), and a
  `VERDICT` line per league for DraftKings h2h, Pinnacle team corners and FanDuel.
  `--coverage` adds football-data's corner counts (HC/AC) and Pinnacle 1X2 (PSH/PSCH)
  shares for 2025/26 and 2026/27 (free download).

Runs:
- Paid probe (needs the lead's go, ≤10 credits): dispatch `odds-check.yml` on
  `claude/soccer-stats-scaffold` with `task=markets`, `cap=10`, `leagues=E2,E3` (other
  inputs default).

Results:
- Estimate (`backfill.yml` run 38178582402, branch `lead/l1-l2-check`, no key; each month
  replayed on the same month of 2025/26, an upper bound since GitHub throttles scheduled
  runs). DraftKings h2h, credits a month (matches), E2 / E3: Oct 211 (46) / 167 (49),
  Nov 183 (43) / 145 (48), Dec 210 (61) / 176 (60); both together about 364 a month
  (378 / 328 / 386). The six live leagues are unchanged by the registry entries (Oct
  2,024, Nov 1,241, Dec 1,276). Pinnacle team-corner snapshots (look + close, 1 credit
  each), E2 / E3: Oct 92 / 98, Nov 86 / 96, Dec 122 / 120; average 100 / 105 a month,
  which would not fit under the current 550 corner cap beside the six (~471).
- Dry run (`odds-check.yml` run 38178580830, `task=markets`, `leagues=E2,E3`, `cap=0`;
  0 credits, balance 22,038): the events lists are non-empty, 12 upcoming events in each
  (E2 first: Mansfield Town v Oxford United, Thu 15 Oct 19:00 UTC; E3 first: Shrewsbury
  Town v Bristol Rovers, Sat 17 Oct 11:30 UTC). Discovery was not run (cap 0).
- football-data coverage (same run): 2025/26 552 matches in each league, corner counts
  (HC/AC) 100%, Pinnacle early and close 1X2 30% (E2 29.9%, E3 30.3%: Pinnacle prices
  stop part-way through the season, as in E1 and the top flights); 2026/27 so far E2 90
  and E3 102 matches, corners 100%, Pinnacle 0%.
- Paid probe (`odds-check.yml` run 38179178621, 11 Oct 22:08 UTC, `task=markets`,
  `leagues=E2,E3`, `cap=10`): **6 credits** (22,037 → 22,031; per league 1 discovery + 2
  for the odds call, h2h and totals). One match each, 4–6 days before kickoff (E2
  Mansfield Town v Oxford United, E3 Shrewsbury Town v Bristol Rovers).
  - **DraftKings h2h: listed in both**, all three sides; margin 7.7% (E2) and 8.5% (E3)
    on that match, against about 4–5% in the top flights.
  - **Pinnacle: h2h and totals only, no team corners.** `alternate_team_totals_corners`
    is not in either league's discovery list (Pinnacle lists h2h, spreads, totals and
    their alternates and halves). Pinnacle h2h margin 5.6% (E2) and 6.3% (E3); totals
    4.6% (E2, line 2.75) and 5.8% (E3, line 2.25). A corner listing nearer kickoff wasn't
    checked; in the six live leagues Pinnacle's corners showed in the 9 Oct discovery.
  - **FanDuel: not listed** in either league, so no team-goal prices.
  - 40+ books list h2h in each league; the cheapest two-sided h2h after Pinnacle was
    Coolbet (7.1% / 6.8%).
- **Verdict.** Only the two match strategies could run in League One and League Two:
  Blend Lean and the fixed 12% test on DraftKings' h2h (a per-league blend can be fitted
  on football-data's Pinnacle closes from 2022/23; 2025/26 has them for only 30% of
  matches). No team-corner test (no Pinnacle corner prices) and no team-goal test (no
  FanDuel). Live odds would cost about 364 credits a month for both (estimate above);
  the corner cap needs no change. Earlier research (round 5, bake-off 2) found no model
  edge in either league against Pinnacle. The owner decides whether to switch them on
  (`live=True` in `leagues.py` plus a `fit-match-blends` refit).

### 24 Oct: Pinnacle now lists team corners in both, switched on

- **Check.** `odds-check.yml` run 40088231180 (`task=markets`, `leagues=E2,E3`, `cap=4`,
  24 Oct 11:01 UTC, about 30 minutes before the 11:30 kickoffs) spent **4 credits**
  (19,339 → 19,335). Pinnacle's discovery list now includes `alternate_team_totals_corners`
  in both leagues (League One: Huddersfield v Wigan; League Two: Bristol Rovers v Swindon);
  the 11 Oct probe (run 38179178621) didn't list it. DraftKings still lists only h2h and
  h2h_3_way there. All 48 team names still map (0 unmatched).
- **Listed, not priced.** The capped run's odds call asked for h2h only, so no corner prices
  were seen: the market is listed, not yet seen priced.
- **Decision.** The owner pre-approved (20 Oct) switching the corner test on if Pinnacle
  listed it, with the corner cap raised from 550 to 750: both are in `corners_live.LEAGUES`
  from 24 Oct, at Pinnacle only (`PINNACLE_ONLY`: the call asks `bookmakers=pinnacle`, so no
  DraftKings corner call and no DraftKings corner trade there).
- **Cost.** `backfill.yml` run 40089304166 (`estimate_month` 2026-10,2026-11,2026-12, no
  key): 674 / 638 / 716 credits for the eight corner leagues, average 676 against the 750
  cap. League One 92 / 86 / 122, League Two 98 / 96 / 120 (an upper bound: GitHub throttles
  scheduled runs). December is the tight month: 716 of 750.
- **Model (f).** The same run fitted it on the cached football-data files: League One 1,102
  matches with corner counts in the 730-day window (21 Oct 2024–21 Oct 2026), League Two
  1,104 (the minimum is 300); league means 5.4 home and 4.6 away (League One), 5.4 and 4.4
  (League Two). Model (f) was never tested in either league (the bake-offs ran on the top
  five and the Championship), so their live record is the only check.
- **Empty calls.** If Pinnacle lists the market but returns no lines, each look still costs
  1 credit: at the estimate's pace about 180-240 a month for both leagues. So in these two
  leagues only, a snapshot kind (looks and closes counted apart) whose last six answered
  calls logged no rows, over at least two runs or kickoff days, pauses for 7 days and then
  looks once more (`corners_live.paused_leagues`, shown in the publish log line). That keeps
  an unpriced listing to about 10 credits a league and snapshot kind in its first month and
  4-5 a month after. The other six leagues never pause: replayed on data-log's October call
  record (233 calls), a league-wide rule would have paused the Championship on 16 Oct (its
  looks are often empty while its closes are priced: 12 empty looks in a row, 8 in one run;
  the lead's review of PR #98); under the per-snapshot, two-run rule nothing in that record
  pauses, and the Championship's pattern in League One would pause only its looks.

## European cups: cost estimate (12 Oct)

Owner's request (12 Oct, lead session): cost out the Champions League (UCL), Europa League
(UEL) and Conference League (UECL), the midweek cups. **An estimate only: 0 credits spent,
nothing switched on.**

What is built (branch `lead/cups-cost`):
- **Registry.** `UCL`, `UEL`, `UECL` in `leagues.py` with `live=False`, `cup=True`,
  `understat=None`, the "matchday" policy and sport keys `soccer_uefa_champs_league`,
  `soccer_uefa_europa_league`, `soccer_uefa_europa_conference_league`. Like League One
  and Two they are never fetched, built, logged or listed in `data.json`
  (`publish.league_list`). `estimate-credits` skips them (football-data has no cup files)
  and points to `estimate-cups`. Tests: `tests/test_cups.py`, `tests/test_leagues.py`.
- **Calendar** (`cups.py`, free, no key): ESPN's site API scoreboard, one date per call,
  slugs `uefa.champions`, `uefa.europa`, `uefa.europa.conf` (main competition, 1 Aug to
  1 Jul) and their `_qual` slugs (qualifying, 15 Jun to 1 Sep). ESPN's
  `leagues[0].calendar` turned out to describe another season, so the run asks every day
  (334 + 78 requests per cup-season, none failed). Past days are cached in
  `data/raw/cups/`.
- **Estimates.** Live refreshes come from `odds_feed.estimate_credits` (matchday rule, 1
  credit a refresh, with each refresh's weekday). Snapshots use the team-total and corner
  estimator (a look 24 h out plus the close, 1 credit per market per snapshot). Historical
  bulk h2h costs 10 credits per region per market per snapshot, one snapshot per distinct
  kickoff time.
- **Team coverage.** Each cup team's ESPN name is matched to football-data's 2024/25 and
  2025/26 files (22 main divisions and 16 "extra leagues"). `cups.py` holds the aliases
  and a deny list for lookalikes (AEK Larnaca is not AEK Athens). The log prints every
  match, plus near names for the teams left unmatched.
- **How to run.** Dispatch `backfill.yml` with `estimate_month` set (any month) and `cups`
  ticked. It needs no key and pushes nothing; it takes about 7 minutes and leaves the
  log plus the 7-day artifact `cups-estimate` (full calendars). The market dry run is
  `odds-check.yml` with `task=markets`, `leagues=UCL,UEL,UECL`, `cap=0`.

Runs (branch `lead/cups-cost`, 0 credits each):
- Calendar and estimates: `backfill.yml` run 38192326466. Run 38192948263 repeated it with
  identical figures and the final team match.
- Market dry run: `odds-check.yml` run 38191998708 (balance 22,019 before and after).

**Calendar, 2025/26** (main competition):

| Cup | Matches | Tue | Wed | Thu | Other | Kickoff slots |
| --- | --- | --- | --- | --- | --- | --- |
| Champions League | 189 | 82 | 100 | 6 | 1 (Sat final) | 59 |
| Europa League | 189 | 0 | 12 | 177 | 0 | 34 |
| Conference League | 153 | 0 | 1 | 151 | 1 (Fri) | 27 |

- 189 = 144 league-phase matches + 16 play-off + 16 + 8 + 4 + 1. The Conference League
  has 108 league-phase matches.
- 529 of the 531 matches (99.6%) are on Tuesday to Thursday.
- 2024/25 has the same match counts, with 57, 37 and 33 kickoff slots.

**1. Live DraftKings h2h, credits a month** (matchday rule, 1 credit a refresh). These are
upper bounds, since GitHub throttles scheduled runs.

| Month (2025/26) | UCL | UEL | UECL | All three |
| --- | --- | --- | --- | --- |
| Sep | 87 | 46 | 3 | 136 |
| Oct | 59 | 50 | 62 | 171 |
| Nov | 86 | 52 | 52 | 190 |
| Dec | 47 | 26 | 48 | 121 |
| Jan | 69 | 50 | 0 | 119 |
| Feb | 87 | 52 | 52 | 191 |
| Mar | 86 | 66 | 52 | 204 |
| Apr | 105 | 84 | 74 | 263 |
| May | 57 | 44 | 44 | 145 |
| **Typical month** (median of months with matches) | **86** | **50** | **52** | **171** |
| **Season** (Sep–May) | **683** | **470** | **387** | **1,540** |
| 2024/25 season, same method | 676 | 511 | 424 | 1,611 |

- Tuesday to Thursday:
  - 74% of the Champions League's refreshes (507 of 683). The 48-hour window opens on
    Sunday and Monday before a Tuesday round.
  - 98% of the Europa League's and 97% of the Conference League's.
  - 1,340 of 1,540 (87%) for all three.
- Knockout months cost the most per match (April: 30 matches, 263 credits). The two legs
  of each tie are spread over Tuesday to Thursday, and every kickoff day gets its own
  48-hour run of refreshes.
- The six live leagues spend about 2,024 a month on DraftKings, about 3,200 with team
  totals and corners. All three cups would add about 5% (171 a month); the Champions
  League alone about 86.
- Qualifying rounds are not in the table. They run in July and August (ESPN's `_qual`
  slugs), outside the main sport keys; The Odds API would need its own qualification
  keys (not checked). In 2025: UCL 92 matches and 337 credits, UEL 82 and 284, UECL 256
  and 381, about 1,000 in all. Most of those teams can't be rated (below), so leave them
  out.

**2. Snapshot costs if the cups' team corners (Pinnacle) or team totals were logged.**
Base plan: a look 24 h out plus the close, 1 credit per market per snapshot, the same
estimator the live logs use.

| Per market | UCL | UEL | UECL | All three |
| --- | --- | --- | --- | --- |
| Season 2025/26 | 378 | 378 | 306 | 1,062 |
| Typical month (median) | 36 | 36 | 32 | 96 (average 118; 18 in May to 216 in Nov) |

- Team corners (one market, its own call) cost the row above: about 1,060 a season and
  up to 216 a month. That would not fit under today's 550 corner cap beside the six
  leagues (about 471).
- Team totals alone cost the same. With the match total in the same call (2 markets, as
  the live team-total log does) it is twice that, about 2,120 a season.
- Nobody knows yet whether Pinnacle quotes cup team corners, or FanDuel cup team totals.
  The probe in 5 below would check.

**3. Historical prices for a backtest.** The Odds API's historical bulk odds cost 10
credits per region per market per snapshot. One snapshot covers every listed match at
that moment, so a close costs one snapshot per distinct kickoff time. Prices are h2h only.

| | Slots | (a) Pinnacle close (eu) | (b) DraftKings close (us) | (c) Both, close + 24 h look |
| --- | --- | --- | --- | --- |
| UCL, 1 season (2025/26) | 59 | 590 | 590 | 2,360 |
| UCL, 2 seasons (+2024/25) | 116 | 1,160 | 1,160 | 4,640 |
| UEL, 1 season | 34 | 340 | 340 | 1,360 |
| UEL, 2 seasons | 71 | 710 | 710 | 2,840 |
| UECL, 1 season | 27 | 270 | 270 | 1,080 |
| UECL, 2 seasons | 60 | 600 | 600 | 2,400 |
| **All three, 1 season** | 120 | **1,200** | **1,200** | **4,800** |
| **All three, 2 seasons** | 247 | **2,470** | **2,470** | **9,880** |

- (c) = (slots + look times) × 10 × 2 regions; there are as many look times as slots.
  Adding the totals market doubles any option.
- A cheaper variant, not costed in the table: take one close snapshot per kickoff day, a
  few minutes before its first kickoff. That covers the 19:00 UTC matches about 2 h 15
  min early and cuts (a) and (b) by about half.
- The Odds API's historical data starts around mid-2020 for featured markets (h2h and
  totals). Earlier seasons (old format) are possible at the same cost per slot.
- Historical team corners aren't available (11 Oct corner-history probe: none at
  Pinnacle in any league), so a cup corner backtest can't be bought.

**4. The build needed** (effort in plain terms):

| Piece | Effort | Data, and is it free? |
| --- | --- | --- |
| Fixtures and results for settlement | Small to medium: an ESPN schedule loader (the parsing exists in `cups.py`), scores for settlement, and team-name maps between The Odds API, ESPN and football-data (about 100 clubs a season, most outside our maps) | ESPN, free. ESPN's cup summaries carry corner counts (`wonCorners` per team; checked on 9 completed 2025/26 matches, 3 per cup) |
| Cross-league team-strength model | Large: the core of the work | See below |
| Blend Lean for cups | Medium once the data exists (`match_calibration.league_fit` on cup matches) | Needs past Pinnacle closes for cup matches, which are not free: option (a) |
| The live pipeline | Medium: `publish` builds a league from football-data or Understat and its own fit, so a cup needs its own path (ESPN schedule, cross-league ratings). Paper trades and the odds log already work per league code | Free |
| The app | Small to medium (ui-designer): the competition filter and cards come from `data.leagues`; the Teams tab has no table for a cup and would show cross-league ratings | Free |

Cross-league team strength:
- **Coverage.** Of 2025/26's 108 league-phase teams, football-data's domestic files rate
  **82 (76%)**:
  - 38 (35%) are in our six fitted leagues.
  - 23 are in its other main divisions (Netherlands 6, Portugal 4, Greece 4, Belgium 3,
    Turkey 3, Scotland 3), with full columns and Pinnacle prices.
  - 21 are in its extra leagues (Poland 4, Switzerland 3, Austria 3, Norway 2, Denmark 2,
    Sweden 2, Ireland 2, Romania 2, Finland 1), with results and closing 1X2 only, no
    shots or corners.
  - Per cup: **Champions League 32 of 36 (89%)**, Europa League 30 of 36 (83%),
    Conference League 20 of 36 (56%).
  - Run 38192948263 counted 81. Its near-name list showed that CSU Craiova is
    football-data's "Univ. Craiova", which has been added since.
- **Not covered (26).** These teams would need domestic results from elsewhere (ESPN
  lists many of these leagues; not checked) or ratings from cup matches alone:
  - Czechia: Slavia Prague, Sparta Prague, Viktoria Plzen, Sigma Olomouc.
  - Croatia: Dinamo Zagreb, Rijeka.
  - Cyprus: Pafos, Omonia, AEK Larnaca.
  - Ukraine: Dynamo Kyiv, Shakhtar.
  - Serbia (Red Star), Hungary (Ferencvaros), Bulgaria (Ludogorets), Israel (Maccabi
    Tel-Aviv), Slovakia (Slovan Bratislava), Slovenia (Celje), Bosnia (Zrinjski).
  - Azerbaijan (Qarabag), Kazakhstan (Kairat), Armenia (Noah), Kosovo (Drita), North
    Macedonia (Shkendija), Iceland (Breidablik), Malta (Hamrun Spartans), Gibraltar
    (Lincoln Red Imps).
  - The Champions League's 4 are Slavia Prague, Qarabag, Kairat and Pafos.
  - Of the teams seen only in qualifying, 42 of 166 (25%) can be rated.
- **League offsets, walk-forward.**
  - Fit each domestic league's goals-only Dixon-Coles as now; its ratings are relative
    to that league's average.
  - A cup match then adds a strength offset per league (home attack = domestic attack +
    the league's offset, and so on), with one cup home advantage.
  - Fit the offsets on cup matches before the match date only, shrunk toward 0 (or
    toward a prior from UEFA's country coefficients), and refit every 28 days like
    `walk_forward`.
  - The training data is ESPN's past cup results (free). Seasons before 2024/25 use the
    old 32-team format but feature the same clubs, and the qualifiers link the small
    leagues.
  - The test: walk-forward log loss on 2024/25 and 2025/26 cup matches against
    Pinnacle's close, which needs option (a).

**5. Market availability** (free events lists only; `odds-check.yml` run 38191998708, cap
0, no discovery or odds calls). Each cup lists 18 events, the next round only:
- UCL: Tue 13 and Wed 14 Oct, at 16:45 and 19:00 UTC (first RC Lens v Sporting Lisbon).
- UEL and UECL: Thu 15 Oct (first Salzburg v AC Milan and Gent v AGF Aarhus).

A later probe at ≤10 credits (`cap=10`, the League One/Two check's code: 1 discovery
credit per cup, then an odds call for the keys found) would check, per cup: DraftKings
h2h (listed? margin), Pinnacle h2h, totals and team corners
(`alternate_team_totals_corners`), and FanDuel team totals. About 3 credits a cup, 9 in
all.

**Recommendation.** Credits are not the obstacle; the model is.
- **Champions League only, as a first step:**
  - about **86 credits a month** in season (683 a season) for live DraftKings odds;
  - plus **590** one-off for Pinnacle's 2025/26 closes (**1,160** for two seasons), to fit
    a cup blend and test a cross-league model against the close before anything trades.
  - Its teams are the best covered: 89% rated, 61% in our own six fits.
- **All three cups:** about 171 a month (1,540 a season) live, and 1,200 (one season) or
  2,470 (two) one-off for Pinnacle closes. The Conference League adds the teams we can
  rate least (56%).
- Earlier research found no model edge against Pinnacle in any domestic league, so
  expect the same here. Only Blend Lean and the 12% test could run, and Blend Lean needs
  option (a).
- Suggested order, each step a separate owner decision:
  1. the ≤10-credit live probe;
  2. option (a) for the Champions League, two seasons (1,160);
  3. the cross-league model, tested walk-forward against Pinnacle;
  4. live odds (`live=True`) only if step 3 earns it, or as a paper test the owner
     chooses.

### European cups: market probe, step 1 (12 Oct; owner-approved ≤10 credits)

`odds-check.yml` run 38198048534 (`task=markets`, `leagues=UCL,UEL,UECL`, `cap=10`,
03:00 UTC): **10 credits** (22,018 → 22,008). One match per cup (1 discovery credit, then
one odds call at up to ten books; the cap's even share left room for three markets in the
Champions League and two in the others, so totals were dropped first where short).

| Cup (match, kickoff) | DraftKings h2h | Pinnacle h2h / totals | Pinnacle team corners | FanDuel |
| --- | --- | --- | --- | --- |
| UCL (RC Lens v Sporting Lisbon, Tue 13 Oct, about 38 h out) | all three sides, 4.8% | 3.4% / 2.8% (line 3.0) | **listed, two-sided, 6.9%** (4.5 and 5.5 per team) | h2h 4.8%; team corners 7.1%; team totals listed; match total one-sided |
| UEL (Salzburg v AC Milan, Thu 15 Oct, about 3.5 days out) | all three sides, 5.6% | 5.4% / 4.1% (line 3.0) | not listed | h2h 7.8%; team totals listed (not asked); match total one-sided |
| UECL (Gent v AGF Aarhus, Thu 15 Oct, about 3.5 days out) | all three sides, 5.5% | 6.0% / 4.8% (line 2.5) | not listed | h2h 7.0% only |

- All 18 events of each cup's next round are listed.
- **DraftKings prices all three cups' match odds**, at margins close to its top-flight ones
  (4.8–5.6%, against 7.7–8.5% in League One and League Two).
- **Pinnacle's team corners showed only for the Champions League match**, the one closest
  to kickoff. The Europa and Conference League matches were about 3.5 days out, so corners may
  simply not be posted yet; League One and League Two (11 Oct) were checked 4–6 days out too.
  A re-check nearer kickoff would settle it (1–2 credits a league).
- DraftKings also lists player props in the Champions League (anytime scorer, shots,
  cards), and FanDuel lists team totals in the Champions and Europa League.
- Nothing switched on. Next steps stay separate owner decisions: Pinnacle Champions League
  closes for a backtest (590 credits a season), the cross-league model, then live odds.

**Re-check near kickoff (15 Oct; owner-approved, "Yes to the europa league and conference
league matches"):** `odds-check.yml` run 38747744922 (`task=markets`, `leagues=UEL,UECL`,
`cap=4`, 14:01 UTC, about 2 h 45 min before the first kickoffs): **4 credits** (20,619 →
20,615); one discovery call and one h2h call per cup, so the corner answer comes from the
discovery list (`books_markets.pinnacle`), not from prices.

| Cup (match, kickoff) | Pinnacle team corners | DraftKings h2h | Pinnacle h2h | FanDuel |
| --- | --- | --- | --- | --- |
| UEL (Salzburg v AC Milan, 16:45 UTC) | **listed** (`alternate_team_totals_corners`; not priced in this run) | 4.7% | 3.6% | h2h 8.0%; team corners and team totals listed |
| UECL (Gent v AGF Aarhus, 16:45 UTC) | **not listed** (Pinnacle lists only h2h, spreads and totals) | 4.4% | 4.4% | h2h 7.9% only |

- So Pinnacle posts Europa League team corners close to kickoff (absent 3.5 days out),
  as it did for the Champions League 38 h out; the Conference League match had none even
  under 3 hours out. DraftKings itself lists team corners in the Europa League match.
- Nothing switched on: the Europa and Conference League stay registered off. Team corners in
  any cup would also need a corner model for clubs across leagues (model (f) is per league).

### Champions League: historical closes download (13 Oct, owner-approved ≤1,200 credits)

Owner decision (13 Oct): switch the Champions League on with Blend Lean, and first buy two
seasons of Pinnacle's past Champions League closing prices (option (a) above, about 1,160
credits once). The lead set the cap at 1,200 credits, with the shared 3,000-credit reserve
kept. This step is only the download and its storage; the cross-league model and the
live switch-on come later.

What it buys (`cup_odds.py`, `soccer-stats backfill-cup-odds`):
- The Odds API's historical bulk odds for `soccer_uefa_champs_league` (league phase and
  knockouts; never the qualifying rounds), `markets=h2h`,
  `bookmakers=pinnacle,draftkings`, decimal prices.
- One snapshot per kickoff slot of ESPN's calendar (`cups.season_calendar` on
  `uefa.champions`), at the kickoff minus 5 minutes, floored to the API's 5-minute
  steps. One snapshot prices every match listed at that moment, so a slot costs about 10
  credits (one market; up to ten named books count as one region).
- Seasons 2024/25 and 2025/26 (the 36-team format): 57 + 59 = **116 snapshots, about
  1,160 credits** on the 12 Oct calendar (the dry run prints the exact plan).

Spend control:
- Every call goes through `player_goal_odds.CappedBudget`: it is skipped when its largest
  possible cost would pass the cap or leave under 3,000 credits on the shared key (the
  balance is read first from The Odds API's free sports list). The cost counted is
  `x-requests-last` on every call.
- The first call is budgeted at 20 credits (one region per book, the worst case). If it
  really costs 20, adding DraftKings doubled the price: DraftKings is dropped for the rest
  of the run and the log says so (two seasons would then cost about 1,170).
- If a slot's snapshot leaves any match without a Pinnacle price, the snapshot 5 minutes
  earlier (`previous_timestamp`) is asked once, but only after every slot's first snapshot
  and only while the cap allows (at most about 4 at a 1,200 cap).
- An HTTP error or a network failure stops all further calls.
- `--cap 0` (or no key) is a dry run: no Odds API call at all; it lists the snapshots, the
  matches in each and the cost estimate.
- The key travels only inside the request. It is redacted from every message, the report,
  the JSON, the cached bodies and the data-log rows.

Never paying twice:
- Every body is cached under `data/raw/cup_odds/soccer_uefa_champs_league_<time>.json`
  (kept in the Actions cache `cup-odds-history-*`, saved even when a later step fails;
  never delete it) and reused before any call.
- The durable copy is on data-log, because GitHub drops a cache unused for 7 days:
  - `odds_history/UCL_closes.jsonl`: one row per match and book (`cup`, `season`,
    `stage`, `date`, `kickoff` and `espn_id` from ESPN, ESPN's `home`/`away`, The Odds
    API's `odds_home`/`odds_away`, `event_id`, `commence_time`, `book`,
    `price_home`/`price_draw`/`price_away` in decimal, `snapshot` (the API's time),
    `requested`, `minutes_before` kickoff, the book's `last_update`, `kind` close or retry,
    how the names matched and whether home and away were swapped);
  - `odds_history/UCL_close_calls.jsonl`: one row per snapshot call (slot, requested,
    snapshot times, books, status, cost from `x-requests-last`, events, matches paired,
    rows per book).
- The job reads both first. A slot whose call is recorded with status 200, or whose
  matches all hold a Pinnacle row, is never asked again; a slot retried once is never
  retried again.
- Team names: The Odds API's names are paired with ESPN's within each slot (an alias
  table, accent-free names, shared words, close spellings; one team matching is enough
  when its partner kicks off at the same time). The log prints every pairing, the Odds
  API events near a slot that matched nothing, and the ESPN matches left without a
  Pinnacle close.

How to run (Actions, `backfill.yml`, on `claude/soccer-stats-scaffold`):
1. **Dry run** (no key, 0 credits): `cup_odds` ticked, `dry_run` ticked (the default),
   `max_credits` 0, `cup_seasons` 2024,2025, everything else as default. It prints the
   plan, `Cup odds: 0 snapshots, 0 credits, ...` and a `CUP_ODDS_JSON` line, and uploads
   the 7-day artifact `cup-odds`.
2. **Paid run** (lead dispatch only, after review): the same with `dry_run` unticked and
   `max_credits` 1200. From the default branch it appends the closes and the call record to
   data-log; from any other branch it only prints and uploads the artifact (the bodies stay
   in the cache, and a later default-branch run with `max_credits` 0 and `dry_run`
   unticked writes them to data-log for free).
3. Read the summary line `Cup odds: N snapshots, C credits, M matches with Pinnacle, D with
   DraftKings, credits left X` and `CUP_ODDS_JSON` (per season: matches on the calendar,
   snapshots held, cached, bought, planned and skipped, matches priced per book). A rerun
   costs nothing for slots already held.
