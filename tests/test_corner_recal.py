"""Team-corner recalibration (edge/corner_recal.py), no network."""

import numpy as np
import pandas as pd

from soccer_stats.edge import corner_recal


def test_coverage_counts_played_matches_with_both_counts():
    raw = pd.DataFrame(
        {
            "HomeTeam": ["A", "B", "C", "D"],
            "AwayTeam": ["E", "F", "G", "H"],
            "FTHG": [1, 0, 2, np.nan],  # the last one isn't played
            "FTAG": [0, 0, 1, np.nan],
            "HC": [5, np.nan, 7, np.nan],
            "AC": [3, 4, 2, np.nan],
        }
    )
    assert corner_recal.coverage_counts(raw) == {"played": 3, "with_corners": 2, "share": 0.6667}
    assert corner_recal.coverage_counts(raw.drop(columns="HC"))["with_corners"] == 0
