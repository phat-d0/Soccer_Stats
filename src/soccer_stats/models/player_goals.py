"""Anytime goalscorer: P(a player scores at least once | he plays).

Goals per match are a count with mean minutes / 90 x exp(intercept + beta . factors),
fitted as a regularised negative binomial (it tends to Poisson when goals show no extra
spread) on the factors in GOAL_FACTORS, all built from earlier matches only
(player_goals.goal_features). As for shots, bets on players who don't play are void,
so the chance is a mix of "starts" and "comes on", weighted by his chance of starting
given he plays (0 or 1 when the lineup is known).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from soccer_stats.models.player_counts import NBRegression

GOAL_FACTORS = [
    "log_xg_rate",  # his shrunk xG per 90 (penalties included)
    "log_finish",  # his goals / xG, heavily shrunk toward 1
    "log_rate",  # his shrunk shots per 90
    "pen_share",
    "pos_def",
    "pos_fwd",
    "log_team_xg",  # the match model's expected goals for his team
    "log_opp_conceded",
    "home",
    "game_state",
]


def p_zero(mean: np.ndarray, alpha: float) -> np.ndarray:
    """NB2 P(0) at `mean` (Poisson as alpha -> 0)."""
    m = np.asarray(mean, dtype=float)
    if alpha < 1e-6:
        return np.exp(-m)
    r = 1.0 / alpha
    return (r / (r + m)) ** r


class GoalscorerModel:
    def __init__(self, factors: list[str] | None = None, l2: float = 2.0):
        self.factors = list(GOAL_FACTORS if factors is None else factors)
        self.goals = NBRegression(self.factors, l2)

    def fit(self, train: pd.DataFrame) -> GoalscorerModel:
        self.goals.fit(train, train["goals"].to_numpy(), train["minutes"].to_numpy() / 90)
        return self

    def means(self, df: pd.DataFrame, lineup_known: bool = False) -> pd.DataFrame:
        rate = self.goals.rate(df)
        p_start = (
            df["started"].astype(float).to_numpy() if lineup_known else df["start_rate"].to_numpy()
        )
        return pd.DataFrame(
            {
                "p_start": p_start,
                "goals_start": rate * df["start_minutes"].to_numpy() / 90,
                "goals_sub": rate * df["sub_minutes"].to_numpy() / 90,
            },
            index=df.index,
        )

    def prob_score(self, df: pd.DataFrame, lineup_known: bool = False) -> np.ndarray:
        """P(scores >= 1 | plays)."""
        m = self.means(df, lineup_known)
        a = self.goals.alpha_
        ps = m["p_start"].to_numpy()
        return ps * (1 - p_zero(m["goals_start"], a)) + (1 - ps) * (1 - p_zero(m["goals_sub"], a))
