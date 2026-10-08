"""The leagues the match pipeline knows, and which of them run live.

One entry per league: its football-data code (the key everywhere: file names, trade ids,
the odds log, `<code>_dk.json`), display name, Understat league name (xG; None for a
league Understat doesn't cover, such as the Championship, which then runs the goals-only
model and takes its schedule from football-data), The Odds API sport key, and two
switches:

* `live`: publish builds the league's fixtures and fetches its DraftKings odds. Only the
  Premier League is live; the others are wired up but off until the owner turns them on
  (the Odds API key is shared with the baseball app, so each league costs credits).
* `odds_policy`: when its odds are refreshed. "always" is the Premier League's rule since
  launch (every hour, every 30 minutes within 2 hours of a kickoff, budget permitting).
  "matchday" fetches only when the league has a match within MATCHDAY_HOURS, and less often
  away from kickoff (odds_feed.policy_floor).

A league trades only with a learned minimum edge from its own backtest
(`backtest/<code>_dk.json` -> edge_threshold); without that file a non-E0 league opens no
paper trades (trades.paper_threshold).
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


LEAGUES: dict[str, League] = {
    lg.code: lg
    for lg in (
        League("E0", "Premier League", "EPL", "soccer_epl", live=True, odds_policy="always"),
        League("SP1", "La Liga", "La_Liga", "soccer_spain_la_liga"),
        League("D1", "Bundesliga", "Bundesliga", "soccer_germany_bundesliga"),
        League("I1", "Serie A", "Serie_A", "soccer_italy_serie_a"),
        League("F1", "Ligue 1", "Ligue_1", "soccer_france_ligue_one"),
        League("E1", "Championship", None, "soccer_efl_champ"),
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
