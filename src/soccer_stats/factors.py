"""The factor list for player shot models, in one place so later player markets reuse it.

Each player's expected shots come from several independent drivers, not from his
average alone. Every feature for a match uses only matches that kicked off strictly
earlier (not even other games at the same time), so nothing known after the look leaks in.

Groups (FACTOR_GROUPS) are what the ablation removes one at a time:

* player:   his shots per 90 (recency-weighted, shrunk toward his position), position,
            penalty duty
* team:     team shots per match, team expected goals for this match (Dixon-Coles),
            share of the team's shots from absent team-mates
* opponent: shots the opponent concedes per match, overall and to his position
* match:    home or away, expected game state (win minus loss chance), days of rest

Minutes (chance of starting, minutes as a starter or substitute) and his on-target rate
are estimated here too, but used by the count model directly rather than as factors.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

HALF_LIFE = 10  # appearances (or team matches) for recency weighting
DECAY = 0.5 ** (1 / HALF_LIFE)
PRIOR_90S = 5.0  # shrink shots per 90 toward the position average by this many 90s
PRIOR_SHOTS = 10.0  # shrink on-target rate toward the position average by this many shots
PRIOR_STARTS = 2.0
PRIOR_START_MIN, PRIOR_SUB_MIN = 80.0, 20.0
# Used before any history exists (fixed, so no later data can leak in).
DEFAULT_POS_RATE = {"GK": 0.05, "DEF": 0.6, "MID": 1.2, "FWD": 2.4}
DEFAULT_SOT_RATE = 0.33
LEAGUE_TEAM_XG = 1.4  # a typical team's expected goals per match, for scaling
REGULAR_SHARE = 0.10  # absent players who usually take this share of team shots count

FACTOR_GROUPS: dict[str, list[str]] = {
    "player": ["log_rate", "pos_def", "pos_fwd", "pen_share"],
    "team": ["log_team_shots", "log_team_xg", "absent_share"],
    "opponent": ["log_opp_conceded", "log_opp_pos"],
    "match": ["home", "game_state", "log_rest"],
}
ALL_FACTORS = [f for fs in FACTOR_GROUPS.values() for f in fs]


def ewsum_prior(x: pd.Series, by: pd.Series) -> pd.Series:
    """Recency-weighted sum of each group's earlier rows: sum of DECAY^age * x.

    Rows must already be in time order. The current row is excluded.
    """
    vals = x.to_numpy(dtype=float)
    out = np.zeros(len(vals))
    for idx in by.groupby(by, sort=False).indices.values():
        s = 0.0
        for i in idx:
            out[i] = s
            s = DECAY * s + (0.0 if np.isnan(vals[i]) else vals[i])
    return pd.Series(out, index=x.index)


def before_kickoff(df: pd.DataFrame, keys: list[str], cols: list[str]) -> pd.DataFrame:
    """Cumulative sums of `cols` per `keys` over kickoffs strictly before each row's."""
    g = df.groupby([*keys, "kickoff"], sort=True)[cols].sum().reset_index()
    cum = g.groupby(keys)[cols].cumsum() - g[cols] if keys else g[cols].cumsum() - g[cols]
    g[cols] = cum
    return (
        df[[*keys, "kickoff"]].merge(g, on=[*keys, "kickoff"], how="left")[cols].set_axis(df.index)
    )


def team_table(app: pd.DataFrame) -> pd.DataFrame:
    """One row per team per match: shots for and against, with prior-only rolling means."""
    t = (
        app.groupby(["match_id", "team"], sort=False)
        .agg(
            kickoff=("kickoff", "first"),
            opponent=("opponent", "first"),
            team_shots=("team_shots", "first"),
            opp_shots=("opp_shots", "first"),
            penalties=("penalties", "sum"),
        )
        .reset_index()
        .sort_values(["kickoff", "match_id"])
        .reset_index(drop=True)
    )
    ones = pd.Series(1.0, index=t.index)
    w = ewsum_prior(ones, t["team"]).replace(0, np.nan)
    t["team_shots_pm"] = ewsum_prior(t["team_shots"], t["team"]) / w
    t["conceded_pm"] = ewsum_prior(t["opp_shots"], t["team"]) / w
    t["team_pens"] = ewsum_prior(t["penalties"], t["team"])
    t["prev_kickoff"] = t.groupby("team")["kickoff"].shift(1)
    lg = before_kickoff(t.assign(n=1.0), [], ["team_shots", "n"])
    t["league_shots"] = (lg["team_shots"] / lg["n"].replace(0, np.nan)).fillna(12.0)
    return t


def build_features(app: pd.DataFrame, match_info: pd.DataFrame | None = None) -> pd.DataFrame:
    """Prior-only features for every appearance in `app` (from player_data).

    `match_info` optionally has match_id, team, team_xg (expected goals for this match)
    and game_state (win minus loss chance) from the match model; missing values are
    neutral.
    """
    df = app.sort_values(["kickoff", "match_id", "team"]).reset_index(drop=True).copy()
    pid = df["player_id"]
    df["_90s"] = df["minutes"] / 90
    df["_one"] = 1.0

    # Player shots per 90 and on-target rate, shrunk toward position averages.
    pos = before_kickoff(df, ["position"], ["shots", "sot", "_90s"])
    default = df["position"].map(DEFAULT_POS_RATE).fillna(1.0)
    pos_rate = (pos["shots"] / pos["_90s"].replace(0, np.nan)).fillna(default)
    pos_sot = (pos["sot"] / pos["shots"].replace(0, np.nan)).fillna(DEFAULT_SOT_RATE)
    s_sh, s_sot = ewsum_prior(df["shots"], pid), ewsum_prior(df["sot"], pid)
    s_90, s_n = ewsum_prior(df["_90s"], pid), ewsum_prior(df["_one"], pid)
    df["rate"] = (s_sh + pos_rate * PRIOR_90S) / (s_90 + PRIOR_90S)
    df["sot_rate"] = (s_sot + pos_sot * PRIOR_SHOTS) / (s_sh + PRIOR_SHOTS)
    df["log_rate"] = np.log(df["rate"].clip(lower=0.02))
    df["pos_def"] = (df["position"] == "DEF").astype(float)
    df["pos_fwd"] = (df["position"] == "FWD").astype(float)
    df["prev_apps"] = df.groupby("player_id").cumcount()

    # Minutes: start rate (among his appearances), minutes as starter / substitute.
    started = df["started"].astype(float)
    df["start_rate"] = (
        (ewsum_prior(started, pid) + 0.5 * PRIOR_STARTS) / (s_n + PRIOR_STARTS)
    ).clip(0.02, 0.98)
    df["start_minutes"] = _shrunk_mean(df["minutes"].where(df["started"]), pid, PRIOR_START_MIN)
    df["sub_minutes"] = _shrunk_mean(df["minutes"].where(~df["started"]), pid, PRIOR_SUB_MIN)

    # Team, opponent and match context.
    t = team_table(df).set_index(["match_id", "team"])
    key = list(zip(df["match_id"], df["team"], strict=True))
    okey = list(zip(df["match_id"], df["opponent"], strict=True))
    lg = t["league_shots"].reindex(key).to_numpy()
    df["team_shots_pm"] = t["team_shots_pm"].reindex(key).to_numpy()
    df["team_shots_exp"] = df["team_shots_pm"].fillna(pd.Series(lg, index=df.index)) * (
        t["conceded_pm"].reindex(okey).to_numpy() / lg
    ).clip(0.3, 3)
    df["log_team_shots"] = np.log((df["team_shots_pm"] / lg).clip(0.3, 3)).fillna(0)
    df["log_opp_conceded"] = np.log((t["conceded_pm"].reindex(okey).to_numpy() / lg).clip(0.3, 3))
    df["log_opp_conceded"] = df["log_opp_conceded"].fillna(0)
    prev = pd.to_datetime(t["prev_kickoff"].reindex(key).to_numpy(), utc=True)
    rest = (df["kickoff"] - prev).dt.total_seconds() / 86400
    df["log_rest"] = np.log(rest.fillna(7).clip(2, 14) / 7)
    df["home"] = df["home"].astype(float)

    # Penalty duty: his recency-weighted penalties over his team's.
    team_pens = pd.Series(t["team_pens"].reindex(key).to_numpy(), index=df.index)
    df["pen_share"] = (ewsum_prior(df["penalties"], pid) / team_pens.replace(0, np.nan)).fillna(0)
    df["pen_share"] = df["pen_share"].clip(0, 1)

    df["log_opp_pos"] = _opp_position_factor(df)
    df["absent_share"] = absent_share(df)

    if match_info is not None and not match_info.empty:
        mi = match_info.drop_duplicates(["match_id", "team"]).set_index(["match_id", "team"])
        df["team_xg"] = mi["team_xg"].reindex(key).to_numpy()
        df["game_state"] = mi["game_state"].reindex(key).to_numpy()
    for col in ("team_xg", "game_state"):
        if col not in df:
            df[col] = np.nan
    lg_xg = LEAGUE_TEAM_XG
    df["log_team_xg"] = np.log((df["team_xg"] / lg_xg).clip(0.3, 3)).fillna(0)
    df["game_state"] = df["game_state"].fillna(0)
    return df.drop(columns=["_90s", "_one"])


def _shrunk_mean(x: pd.Series, by: pd.Series, prior: float, k: float = 3.0) -> pd.Series:
    """Mean of each group's earlier non-missing values, shrunk toward `prior` by k values."""
    v = x.fillna(0)
    n = x.notna().astype(float)
    s = v.groupby(by).cumsum() - v
    c = n.groupby(by).cumsum() - n
    return (s + prior * k) / (c + k)


def _opp_position_factor(df: pd.DataFrame) -> pd.Series:
    """log(opponent's share of shots conceded to this position / league share), prior-only."""
    by = (
        df.groupby(["match_id", "team", "opponent", "kickoff", "position"])["shots"]
        .sum()
        .reset_index()
    )
    by["_all"] = by["shots"]
    # What each opponent conceded, by position and in total, before this kickoff.
    conc = before_kickoff(by, ["opponent", "position"], ["shots"])["shots"]
    tot = before_kickoff(by, ["opponent"], ["_all"])["_all"]
    lg_pos = before_kickoff(by, ["position"], ["shots"])["shots"]
    lg_tot = before_kickoff(by, [], ["_all"])["_all"]
    share = conc / tot.replace(0, np.nan)
    lg_share = lg_pos / lg_tot.replace(0, np.nan)
    by["f"] = np.log((share / lg_share).clip(0.5, 2)).fillna(0)
    look = by.set_index(["match_id", "team", "position"])["f"]
    key = list(zip(df["match_id"], df["team"], df["position"], strict=True))
    return pd.Series(look.reindex(key).to_numpy(), index=df.index).fillna(0)


def absent_share(df: pd.DataFrame) -> pd.Series:
    """Per appearance: share of the team's recent shots taken by regulars not playing.

    Uses who actually played (the 'lineup known' view). For 'before lineups', callers
    replace it with expected absences (FPL news live; zero in the backtest).
    """
    out = pd.Series(0.0, index=df.index)
    for _team, g in df.groupby("team", sort=False):
        order = g.drop_duplicates("match_id").sort_values("kickoff")["match_id"]
        pivot = g.pivot_table(index="match_id", columns="player_id", values="shots", aggfunc="sum")
        pivot = pivot.reindex(order)
        played = pivot.notna().to_numpy()
        x = pivot.fillna(0).to_numpy()
        hist = np.zeros_like(x)  # prior-only recency-weighted shots per player
        s = np.zeros(x.shape[1])
        for i in range(len(x)):
            hist[i] = s
            s = DECAY * s + x[i]
        tot = hist.sum(axis=1, keepdims=True)
        share = np.divide(hist, tot, out=np.zeros_like(hist), where=tot > 0)
        missing = (share * (~played) * (share >= REGULAR_SHARE)).sum(axis=1)
        out.loc[g.index] = g["match_id"].map(dict(zip(order, missing, strict=True))).to_numpy()
    return out
