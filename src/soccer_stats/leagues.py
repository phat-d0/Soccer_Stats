"""The leagues the match pipeline knows, and which of them run live.

One entry per league: its football-data code (the key everywhere: file names, trade ids,
the odds log, `<code>_dk.json`), display name, Understat league name (xG; None for a
league Understat doesn't cover, such as the Championship, which then runs the goals-only
model and takes its schedule from football-data), The Odds API sport key, and two
switches:

* `live`: publish builds the league's fixtures and fetches its DraftKings odds. All six
  are live (owner, 8 Oct 2026). The Odds API key is shared with the baseball app, so each
  league costs credits; the Premier League budgets first, and the others pause below
  odds_feed.MATCHDAY_RESERVE_CREDITS (docs/leagues.md).
* `odds_policy`: when its odds are refreshed. "always" is the Premier League's rule since
  launch (every hour, every 30 minutes within 2 hours of a kickoff, budget permitting).
  "matchday" fetches only when the league has a match within MATCHDAY_HOURS, and less often
  away from kickoff (odds_feed.policy_floor).

Live paper trades follow trades.PAPER_RULE. Under "fixed_raw" (owner's live test, 10 Oct
2026) every live league trades at 12% on the model's own chance. Under "learned", a
league trades only with a learned minimum edge: its own backtest
(`backtest/<code>_dk.json` -> edge_threshold), else the research lab's
`lab/min_edge.json`. Without one, or with a null level (all five non-E0 leagues today), a
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


LEAGUES: dict[str, League] = {
    lg.code: lg
    for lg in (
        League("E0", "Premier League", "EPL", "soccer_epl", live=True, odds_policy="always"),
        League("SP1", "La Liga", "La_Liga", "soccer_spain_la_liga", live=True),
        League("D1", "Bundesliga", "Bundesliga", "soccer_germany_bundesliga", live=True),
        League("I1", "Serie A", "Serie_A", "soccer_italy_serie_a", live=True),
        League("F1", "Ligue 1", "Ligue_1", "soccer_france_ligue_one", live=True),
        League("E1", "Championship", None, "soccer_efl_champ", live=True),
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
