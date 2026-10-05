import numpy as np
import pandas as pd
import pytest


def simulate_league(n_teams=12, seasons=3, home_adv=0.25, intercept=0.1, seed=0):
    """Double round-robin seasons with known Poisson team strengths and fair-ish odds."""
    rng = np.random.default_rng(seed)
    teams = [f"T{i:02d}" for i in range(n_teams)]
    att = rng.normal(0, 0.3, n_teams)
    att -= att.mean()
    dfn = rng.normal(0, 0.3, n_teams)
    dfn -= dfn.mean()
    rows = []
    day = pd.Timestamp("2020-08-01")
    for s in range(seasons):
        fixtures = [(h, a) for h in range(n_teams) for a in range(n_teams) if h != a]
        rng.shuffle(fixtures)
        for k, (h, a) in enumerate(fixtures):
            lam = np.exp(intercept + home_adv + att[h] + dfn[a])
            mu = np.exp(intercept + att[a] + dfn[h])
            rows.append(
                {
                    "date": day + pd.Timedelta(days=365 * s + k // (n_teams // 2) * 3),
                    "home": teams[h],
                    "away": teams[a],
                    "home_goals": rng.poisson(lam),
                    "away_goals": rng.poisson(mu),
                    "lam": lam,
                    "mu": mu,
                }
            )
    df = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    truth = {
        "attack": dict(zip(teams, att, strict=True)),
        "defence": dict(zip(teams, dfn, strict=True)),
        "home_adv": home_adv,
        "intercept": intercept,
    }
    return df, truth


@pytest.fixture(scope="session")
def league():
    return simulate_league()
