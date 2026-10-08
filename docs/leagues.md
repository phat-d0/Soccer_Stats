# More leagues: what it would cost in Odds API credits

The match pipeline is ready for La Liga (SP1), Bundesliga (D1), Serie A (I1) and Ligue 1
(F1). They are wired in but switched off. Only the Premier League (E0) fetches live odds,
and its behaviour hasn't changed. This page shows what switching each league on would
cost, so the owner can decide. **No credits were spent to work this out.**

The Odds API key is shared with the baseball app. The balance was about 22,600 on 7 Oct.

## How a league is switched on

1. Set `live=True` for the league in `src/soccer_stats/leagues.py` (the `LEAGUES`
   registry) and merge.
2. Each publish run then builds that league's fixtures with its own match model, fetches
   its DraftKings odds by the refresh rule below, and logs them to
   `odds_log/<code>_<YYYY-MM>.jsonl`. The app gets every fixture tagged with its league.
3. **Paper trades for that league stay off** until it has its own learned minimum edge
   (`backtest/<code>_dk.json` → `edge_threshold.min_edge`). That needs a DraftKings
   backtest for the league, which needs historical DraftKings odds, which costs credits.
   That's a separate decision. With no level, the league shows fixtures and odds only.

## Refresh rule

One refresh is one call: h2h and totals from one bookmaker, which costs 2 credits.

| League | Rule |
| --- | --- |
| Premier League (live) | Unchanged: every hour, around the clock, and every 30 minutes within 2 hours of a kickoff (budget permitting). |
| SP1, D1, I1, F1 (when switched on) | Nothing unless the league has a match within 48 hours. Then every 3 hours, hourly from 6 hours before kickoff, and every 30 minutes in the last 2 hours. |

With more than one league live, the budget rule splits the remaining credits evenly
between them. When credits run low, every league slows down together.

## Estimated cost per month

`soccer-stats estimate-credits --month YYYY-MM` replays the scheduled publish runs over
that month's fixtures (from Understat) under the rule above. You can also run it with
*Backfill DraftKings odds* → `estimate_month`, which needs no key and spends nothing.

The figures are upper bounds: GitHub delays or skips some scheduled runs, so real use is
lower.

**October 2026** (most kickoff times fixed; `backfill.yml` run 37841834127):

| League | Matches | Calls | Credits / month |
| --- | --- | --- | --- |
| Premier League (live now) | 38 | 774 | **1,548** |
| La Liga | 36 | 310 | 620 |
| Bundesliga | 36 | 207 | 414 |
| Serie A | 43 | 311 | 622 |
| Ligue 1 | 36 | 224 | 448 |
| **All four new leagues** | | | **2,104** |
| **All five** | | | **3,652** |

I also ran November and December 2026 (runs 37841726640 and 37841730546). The four
leagues came out at only 126–156 credits a month. Their schedules for those months still
use placeholder kickoff times, so all of a round's matches look like one kickoff. Treat
October's figures as the realistic ones. My synthetic "typical month" calendar (four full
rounds, usual kickoff slots) gave 478–720 credits per league, in line with October.

## What that means for the balance

- **Premier League alone, as now:** about 1,500 a month, so roughly 15 months on 22,600
  credits before counting the baseball app.
- **All five leagues:** about 3,650 a month, so roughly 6 months.
- **One extra league:** about 400–620 a month. Serie A and La Liga cost the most, with
  more matches spread over Friday to Monday. The Bundesliga is cheapest, because most of
  its round kicks off at the same Saturday time.

Ways to spend less, if needed (none are built yet):
- refresh the new leagues only from 24 hours out instead of 48 (saves roughly a third);
- drop the totals market for them, which halves each call to 1 credit.

## Things to check on the first live run of a new league

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
