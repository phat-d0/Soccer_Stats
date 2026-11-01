"""The priced goalscorer read gives identical bets from identical inputs (lead, 1 Nov 2026).

Two development runs on the same prices and rows gave 2,113 and 2,115 bets: scipy's default
L-BFGS-B stop let last-bit float differences between machines move the match model's fits
(and so B's chances) by up to ~1e-5. Reordering rows changes float sums the same way, so the
tests below fit on shuffled copies of the same data.
"""

import numpy as np
import pandas as pd

from soccer_stats import player_goal_priced as gp
from soccer_stats.models import DixonColes
from soccer_stats.models.player_goals import FIT_OPTIONS, GOAL_FACTORS, GoalscorerModel

TEAMS = [f"T{i}" for i in range(20)]


def matches(n=760, seed=0):
    rng = np.random.default_rng(seed)
    d0 = pd.Timestamp("2023-08-01")
    rows = []
    for k in range(n):
        h, a = rng.choice(20, 2, replace=False)
        rows.append({"date": d0 + pd.Timedelta(days=k // 2), "home": TEAMS[h],
                     "away": TEAMS[a], "home_goals": rng.poisson(1.5),
                     "away_goals": rng.poisson(1.2), "home_xg": rng.gamma(3, 0.5),
                     "away_xg": rng.gamma(3, 0.4)})  # fmt: skip
    return pd.DataFrame(rows)


def team_xg(m, options):
    dc = DixonColes(xg_weight=0.7, options=options).fit(m)
    return {(h, a): dc.expected_goals(h, a) for h in TEAMS for a in TEAMS if h != a}


def test_match_model_fits_agree_across_row_orders_with_tight_options():
    m = matches()
    a = team_xg(m, FIT_OPTIONS)
    b = team_xg(m.sample(frac=1, random_state=5).reset_index(drop=True), FIT_OPTIONS)
    diff = max(abs(a[k][i] - b[k][i]) for k in a for i in (0, 1))
    assert diff < 1e-6


def players(xg, seed=1, n=20000):
    rng = np.random.default_rng(seed)
    pairs = list(xg)
    pick = rng.integers(0, len(pairs), n)
    df = pd.DataFrame({f: rng.normal(size=n) for f in GOAL_FACTORS})
    df["log_team_xg"] = np.log([xg[pairs[i]][0] for i in pick])
    df["minutes"] = rng.integers(10, 91, n)
    eta = -2.3 + 0.5 * df["log_xg_rate"] + 0.4 * df["log_team_xg"]
    df["goals"] = rng.poisson(np.exp(eta) * df["minutes"] / 90)
    df["match_id"] = [f"m{i // 10}" for i in range(n)]
    return df.assign(started=True, start_minutes=85, sub_minutes=20, start_rate=1.0)


def bets_from(m, shuffle_seed=None):
    """One 'run': the match model, then B, then the rule's bets on fixed prices."""
    xg = team_xg(m, FIT_OPTIONS)
    df = players(xg)
    train = df if shuffle_seed is None else df.sample(frac=1, random_state=shuffle_seed)
    p = GoalscorerModel().fit(train).prob_score(df, lineup_known=True)
    # Prices that put many rows right around the 20% threshold.
    rng = np.random.default_rng(9)
    odds = np.round((1 + gp.T_JUDGED) / p * rng.uniform(0.98, 1.02, len(p)), 2)
    j = df.assign(p_B=p, entry_odds=odds)
    return p, gp.bets_at(j, gp.T_JUDGED)


def test_two_runs_on_the_same_inputs_give_identical_bets():
    m = matches()
    p1, b1 = bets_from(m)
    p2, b2 = bets_from(m.sample(frac=1, random_state=3).reset_index(drop=True), shuffle_seed=7)
    assert np.abs(p1 - p2).max() < 1e-7
    assert b1.sum() > 1000
    assert (b1.to_numpy() == b2.to_numpy()).all()


def test_rounding_keeps_last_digit_noise_off_the_threshold():
    # 0.25 × 4.0 − 1 is exactly 0: unrounded, the two chances would fall either side of a
    # zero threshold; rounded to 6 decimals they agree.
    j = pd.DataFrame({"p_B": [0.25 + 1e-11, 0.25 - 1e-11], "entry_odds": [4.0, 4.0]})
    assert ((j["p_B"] * j["entry_odds"] - 1) >= 0).tolist() == [True, False]
    assert gp.bets_at(j, 0.0).tolist() == [True, True]
    assert gp.t_volume(j.assign(match_id="m"), candidates=(0.0,))["by_t"][0]["bets"] == 2
