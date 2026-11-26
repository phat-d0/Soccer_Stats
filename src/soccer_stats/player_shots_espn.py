"""§17's shots model on ESPN player data, frozen by the pre-registration of 26 Nov 2026
(docs/player_props.md §17). Nothing here reads a price."""

from __future__ import annotations

# Every shots factor except pen_share (penalty xG, which ESPN doesn't carry).
SHOTS_ESPN_FACTORS = [
    "log_rate",
    "pos_def",
    "pos_fwd",
    "log_team_shots",
    "log_team_xg",
    "absent_share",
    "log_opp_conceded",
    "log_opp_pos",
    "home",
    "game_state",
    "log_rest",
]
SHOTS_ESPN_L2 = 2.0
SHOTS_ESPN_SOT = "thin"


def shots_espn_model():
    """§17's frozen shots model: B's shot NB2 (start/sub mixture, lineup known) on ESPN
    appearance features, on-target by thinning, fitted with FIT_OPTIONS."""
    from soccer_stats.models.player_counts import PlayerShotModel
    from soccer_stats.models.player_goals import FIT_OPTIONS

    return PlayerShotModel(
        SHOTS_ESPN_FACTORS, l2=SHOTS_ESPN_L2, sot_method=SHOTS_ESPN_SOT, options=FIT_OPTIONS
    )
