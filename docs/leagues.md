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

## League One and League Two check (11 Oct)

Owner's request (11 Oct, lead session): more midweek fixtures. Among our six leagues only
the Championship plays midweek, so League One (E2) and League Two (E3) are candidates.
Owner-approved at **10 credits at most** for the paid part. Nothing goes live from this
check; switching either on is a separate owner decision.

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
