"""The leagues the match pipeline knows, and which of them run live.

One entry per league: its football-data code (the key everywhere: file names, trade ids,
the odds log, `<code>_dk.json`), display name, Understat league name (xG; None for a
league Understat doesn't cover, such as the Championship, which then runs the goals-only
model and takes its schedule from football-data; League One and League Two likewise),
The Odds API sport key, and two
switches:

* `live`: publish builds the league's fixtures and fetches its DraftKings odds. Eight
  are live: the Premier League, La Liga, Bundesliga, Serie A, Ligue 1 and the
  Championship (owner, 8 Oct 2026), plus League One and League Two (E2, E3; owner, 13 Oct
  2026, after the 11 Oct check: DraftKings h2h only, no team corners or team totals
  there), and the Champions League (UCL; owner, 13 Oct 2026: fixtures and results from
  ESPN, model chances from the cross-league fit in cup_model.py). The Europa League and
  Conference League (UEL, UECL; cost estimate, 12 Oct 2026) are registered off: priced
  and probed only, never fetched or built. The Odds API
  key is shared with the baseball app, so each league costs credits; the Premier League
  budgets first, and the others pause below odds_feed.MATCHDAY_RESERVE_CREDITS
  (docs/leagues.md).
* `odds_policy`: when its odds are refreshed. "always" is the Premier League's rule since
  launch (every hour, every 30 minutes within 2 hours of a kickoff, budget permitting).
  "matchday" fetches only when the league has a match within MATCHDAY_HOURS, and less often
  away from kickoff (odds_feed.policy_floor).

Live paper trades follow trades.PAPER_RULE. Under "fixed_raw" (owner's live test, 10 Oct
2026) every live league trades at 12% on the model's own chance, and Blend Lean trades
wherever backtest/match_blends.json has the league's fit. Under "learned", a
league trades only with a learned minimum edge: its own backtest
(`backtest/<code>_dk.json` -> edge_threshold), else the research lab's
`lab/min_edge.json`. Without one, or with a null level (every non-E0 league today), a
non-E0 league opens no paper trades (trades.paper_threshold).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class League:
    code: str  # football-data division code
    name: str
    understat: str | None  # Understat league name (xg.LEAGUES); None = no xG
    odds_sport: str  # The Odds API sport key
    live: bool = False
    odds_policy: str = "matchday"  # "always" or "matchday"
    cup: bool = False  # a UEFA cup: no football-data calendar (cups.py reads ESPN's)


LEAGUES: dict[str, League] = {
    lg.code: lg
    for lg in (
        League("E0", "Premier League", "EPL", "soccer_epl", live=True, odds_policy="always"),
        League("SP1", "La Liga", "La_Liga", "soccer_spain_la_liga", live=True),
        League("D1", "Bundesliga", "Bundesliga", "soccer_germany_bundesliga", live=True),
        League("I1", "Serie A", "Serie_A", "soccer_italy_serie_a", live=True),
        League("F1", "Ligue 1", "Ligue_1", "soccer_france_ligue_one", live=True),
        League("E1", "Championship", None, "soccer_efl_champ", live=True),
        # League One and League Two: live from 13 Oct (owner; docs/leagues.md "League One
        # and League Two live"). Goals-only like the Championship (no Understat), schedule
        # from football-data, matchday odds policy. DraftKings h2h; team corners at Pinnacle
        # only from 24 Oct (corners_live.PINNACLE_ONLY); no team totals
        # (odds_feed.TEAM_TOTAL_LEAGUES) there.
        League("E2", "League One", None, "soccer_england_league1", live=True),
        League("E3", "League Two", None, "soccer_england_league2", live=True),
        # The Champions League: live from 13 Oct (owner; docs/leagues.md "Champions League
        # live"). football-data has no cup files: fixtures and results come from ESPN's
        # uefa.champions scoreboard, model chances from the cross-league fit (cup_model.py),
        # DraftKings h2h on the main key (league phase and knockouts), matchday odds policy.
        # No team corners or team totals.
        League("UCL", "Champions League", None, "soccer_uefa_champs_league", live=True, cup=True),
        # The Europa League and Conference League: off, a cost estimate only (owner's
        # request, 12 Oct; docs/leagues.md "European cups"); estimate-credits leaves the
        # cups to estimate-cups. A league that isn't live is never fetched, built, logged
        # or listed in data.json (publish.league_list).
        League("UEL", "Europa League", None, "soccer_uefa_europa_league", cup=True),
        League("UECL", "Conference League", None, "soccer_uefa_europa_conference_league", cup=True),
    )
}
PRIMARY = "E0"  # the league whose fields fill data.json's top level (params, teams, ...)


def get(code: str) -> League:
    """The registry entry for a football-data code (KeyError if unknown)."""
    return LEAGUES[code]


def live_codes() -> list[str]:
    """Codes of the leagues switched on, the primary league first."""
    codes = [c for c, lg in LEAGUES.items() if lg.live]
    return sorted(codes, key=lambda c: c != PRIMARY)


def name(code: str) -> str:
    lg = LEAGUES.get(code)
    return lg.name if lg else code
