# Team news from ESPN: confirmed lineups (all six leagues)

The owner approved this on 9 Oct: a free team-news feed for every league. It costs 0 Odds
API credits. It is data and display only, so nothing here changes how bets are judged.
The Premier League's FPL team news (`news` on each card, `fpl_news/` on data-log) is
unchanged; ESPN's feed sits beside it.

It is for two things:
- the 16 Nov check, which needs the time each confirmed lineup first appears in every
  league, to measure how fast DraftKings reacts compared with Pinnacle after a lineup
  surprise;
- the match sheets, which can show the lineups.

Code: `src/soccer_stats/espn_news.py`.

## Source

ESPN's free site API: `site.api.espn.com/apis/site/v2/sports/soccer/<slug>/scoreboard`
and `/summary?event=<id>`. It is unofficial and needs no key.

What the probes found (`odds-check.yml` `task=espn`, no key; runs 37964143972,
37964327038 and 37964999592, 9 Oct):
- **Slugs.** All six answer: E0 `eng.1`, SP1 `esp.1`, D1 `ger.1`, I1 `ita.1`, F1
  `fra.1`, E1 `eng.2`.
- **One date per call.** A date range (`dates=20261005-20261011`) returns 400 in every
  league, while a single date returns 200. The scoreboard therefore makes one call per
  UTC day.
- **Lineups.** `rosters[].roster[]` with `starter` true or false and
  `athlete.displayName`; empty before the lineup is out. The shot-count check had already
  read the same fields for the Premier League's finished matches.
- **Injuries: none.** No league's summary has an `injuries` key. The parser reads one
  defensively if ESPN adds it, but today the injury lists are always empty. The Premier
  League's injuries still come from FPL.
- **No update time.** ESPN gives no last-updated time for the lineup (`meta` holds only
  `gameState`). The time a lineup first appears is therefore our own fetch time.

## When it fetches (each publish run)

- **Scoreboard:** one call per UTC day from now to 36 hours ahead, per league, cached 6
  hours.
- **Summary:** only for matches kicking off within 36 hours.
  - More than 90 minutes out: refreshed every 3 hours.
  - Within 90 minutes: every run, until both teams list 11 starters (a confirmed XI).
    After that the body is final and never fetched again.
- **Limits:** at most 60 summaries a run across all leagues; bodies cached in
  `data/raw/espn/`.
- **Failures:** 15-second timeouts, two retries on errors and 5xx (none on 4xx). Any
  failure means no team news for that league this run; it never stops the build.
- **Team names:** matched with the shared map (`odds_feed.TEAM_NAMES` plus its
  unaccented and unique prefix/suffix match) and `espn_news.ESPN_NAMES` for the rest.
  Unmatched ESPN matches inside the window are printed in the publish log.
- **Log line:** "Team news (ESPN): N matches, C confirmed lineups, R requests (F failed);
  unmatched: …".

## In data.json

Each fixture card kicking off within 36 hours, and matched to an ESPN event, gains:

```json
"team_news": {
  "source": "ESPN",
  "event_id": "401879268",
  "fetched_at": "2026-10-17T13:52:00+00:00",
  "updated": null,
  "injuries": {"home": [], "away": []},
  "lineup": {
    "confirmed": true,
    "fetched_at": "2026-10-17T13:52:00+00:00",
    "first_confirmed_at": "2026-10-17T13:52:00+00:00",
    "home": {"starters": ["…11 names"], "subs": ["…"]},
    "away": {"starters": ["…11 names"], "subs": ["…"]}
  }
}
```

- `lineup.home` and `lineup.away` appear once ESPN lists a roster; before that the lineup
  has only `confirmed: false` and `fetched_at`.
- `first_confirmed_at` appears once both XIs are out.
- `injuries[side]` is a list of `{name, status, detail}`, empty today.
- Cards with no ESPN news have no `team_news` key.

## On data-log

The file is `team_news/<code>_<YYYY-MM>.jsonl`, written by `soccer-stats log-team-news`
from the built `data.json` (no API calls).
- One row per match and team, written only when the content changed: confirmed flag,
  starters, subs, injuries.
- Each row has: league, home, away, kickoff, event_id, team, side, confirmed, starters,
  subs, injuries, `espn_updated`, `fetched_at`, `first_confirmed_at` and `logged_at`.
- **`first_confirmed: true`** marks the first row with a confirmed XI for that match and
  team. Its `fetched_at` is the lineup announcement time (to within one publish run) that
  the 16 Nov check uses.
