"""§17's shots model on ESPN features is frozen: its settings are pinned here."""

from soccer_stats import player_shots_espn as ep
from soccer_stats.factors import ALL_FACTORS
from soccer_stats.models.player_goals import FIT_OPTIONS


def test_shots_espn_model_settings_are_frozen():
    assert ep.SHOTS_ESPN_FACTORS == [f for f in ALL_FACTORS if f != "pen_share"]
    m = ep.shots_espn_model()
    assert m.factors == ep.SHOTS_ESPN_FACTORS and m.l2 == 2.0 and m.sot_method == "thin"
    assert m.shots.options == FIT_OPTIONS and m.sot.options == FIT_OPTIONS
