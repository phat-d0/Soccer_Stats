# More leagues: live odds and what they cost in Odds API credits

The owner turned on live odds for every league on 8 Oct 2026: La Liga (SP1), Bundesliga
(D1), Serie A (I1), Ligue 1 (F1) and the EFL Championship (E1) now fetch DraftKings odds
beside the Premier League (E0), whose behaviour hasn't changed. Historical DraftKings odds
for the new leagues are not approved (cap 0), so they have no DraftKings backtest. This
page shows what each league costs. **No credits were spent to work this out.**

The Odds API key is shared with the baseball app. The balance was about 22,600 on 7 Oct.

## How a league is switched on (or off)

1. Set `live=True` (or `False`) for the league in `src/soccer_stats/leagues.py` (the
   `LEAGUES` registry) and merge. All six are `True` now.
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

One refresh is one call: h2h and totals from one bookmaker, which costs 2 credits.

| League | Rule |
| --- | --- |
| Premier League (live) | Unchanged: every hour, around the clock, and every 30 minutes within 2 hours of a kickoff (budget permitting). |
| SP1, D1, I1, F1, E1 | Nothing unless the league has a match within 48 hours. Then every 3 hours, hourly from 6 hours before kickoff, and every 30 minutes in the last 2 hours. |

### Who gets the budget when credits run low

- **The Premier League budgets as if it were alone** (share 1). With a healthy balance
  that changes nothing: at about 22,600 credits even a six-way split leaves ~1,900 calls
  per league for the month, far more than the hourly floor needs, so every league sits at
  its floor. It matters only when credits run low: at 3,000 credits an even six-way split
  would stretch E0 from hourly to about every 2 hours. Priority keeps it hourly.
- **The other leagues split the balance six ways and keep 3,000 credits in reserve**
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
lower.

**October 2026** (most kickoff times fixed; `backfill.yml` runs 37841834127 and 37843392959):

| League | Matches | Calls | Credits / month |
| --- | --- | --- | --- |
| Premier League | 38 | 774 | **1,548** |
| La Liga | 36 | 310 | 620 |
| Bundesliga | 36 | 207 | 414 |
| Serie A | 43 | 311 | 622 |
| Ligue 1 | 36 | 224 | 448 |
| Championship (October 2025 calendar*) | 53 | 198 | 396 |
| **All five new leagues** | | | **2,500** |
| **All six** | | | **4,048** |

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
- **All six leagues (live from 8 Oct):** about 4,050 a month, so roughly 5½ months. The
  new leagues stop at 3,000 credits, so E0 then runs on alone.
- **One extra league:** about 400–620 a month. Serie A and La Liga cost the most, with
  more matches spread over Friday to Monday. The Bundesliga is cheapest, because most of
  its round kicks off at the same Saturday time.

Ways to spend less, if needed (none are built yet):
- refresh the new leagues only from 24 hours out instead of 48 (saves roughly a third);
- drop the totals market for them, which halves each call to 1 credit.

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
