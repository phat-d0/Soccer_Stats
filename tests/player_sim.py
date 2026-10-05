"""Synthetic player appearances with known shot rates, in player_data's schema."""

import numpy as np
import pandas as pd

POS_RATE = {"GK": 0.02, "DEF": 0.5, "MID": 1.2, "FWD": 2.4}
SQUAD = ["GK"] * 2 + ["DEF"] * 6 + ["MID"] * 6 + ["FWD"] * 4
STARTERS = {"GK": 1, "DEF": 4, "MID": 4, "FWD": 2}


def _letters(s):
    """Digits -> letters, so names survive normalisation (real names have no digits)."""
    return s.translate(str.maketrans("0123456789", "abcdefghij")).capitalize()


def simulate_players(n_teams=10, seasons=2, seed=0, alpha=0.3):
    rng = np.random.default_rng(seed)
    teams = [f"T{i:02d}" for i in range(n_teams)]
    attack = dict(zip(teams, rng.normal(0, 0.25, n_teams), strict=True))
    defence = dict(zip(teams, rng.normal(0, 0.2, n_teams), strict=True))
    players = []
    for t in teams:
        for j, pos in enumerate(SQUAD):
            players.append(
                {
                    "team": t,
                    "player_id": f"{t}_{j}",
                    "player": f"Player {_letters(t)} {_letters(str(j))}",
                    "position": pos,
                    "rate": POS_RATE[pos] * rng.lognormal(0, 0.45),
                    "sot_p": float(np.clip(rng.normal(0.35, 0.08), 0.1, 0.7)),
                    "quality": rng.normal(0, 1),  # who tends to start
                }
            )
    pl = pd.DataFrame(players)
    rows, mid = [], 0
    day = pd.Timestamp("2023-08-12 14:00", tz="UTC")
    for s in range(seasons):
        fixtures = [(h, a) for h in teams for a in teams if h != a]
        rng.shuffle(fixtures)
        for k, (h, a) in enumerate(fixtures):
            mid += 1
            kickoff = day + pd.Timedelta(days=365 * s + (k // (n_teams // 2)) * 4)
            season = f"{23 + s:02d}{24 + s:02d}"
            match_rows = []
            for team, opp, home in ((h, a, True), (a, h, False)):
                squad = pl[pl["team"] == team]
                mult = np.exp(attack[team] + defence[opp] + (0.1 if home else -0.1))
                for pos, n in STARTERS.items():
                    grp = squad[squad["position"] == pos]
                    score = grp["quality"] + rng.normal(0, 1, len(grp))
                    order = grp.assign(score=score).sort_values("score", ascending=False)
                    for i, p in enumerate(order.itertuples()):
                        if i < n:
                            minutes, started = int(rng.choice([90, 90, 90, 75, 65])), True
                        elif i < n + 1 and pos != "GK" and rng.random() < 0.6:
                            minutes, started = int(rng.integers(10, 35)), False
                        else:
                            continue
                        m = p.rate * mult * minutes / 90
                        lam = rng.gamma(1 / alpha, m * alpha) if m > 0 else 0
                        shots = rng.poisson(lam)
                        sot = rng.binomial(shots, p.sot_p)
                        match_rows.append(
                            {
                                "match_id": str(mid),
                                "season": season,
                                "kickoff": kickoff,
                                "team": team,
                                "opponent": opp,
                                "home": home,
                                "player_id": p.player_id,
                                "player": p.player,
                                "position": pos,
                                "minutes": minutes,
                                "started": started,
                                "shots": int(shots),
                                "sot": int(sot),
                                "penalties": 0,
                            }
                        )
            df = pd.DataFrame(match_rows)
            tot = df.groupby("team")["shots"].sum()
            df["team_shots"] = df["team"].map(tot)
            df["opp_shots"] = df["opponent"].map(tot)
            rows.append(df)
    return pd.concat(rows, ignore_index=True), pl
