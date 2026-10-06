"""Walk-forward backtest for the player shot models.

Stage 1 (no odds, no credits): by match week from the start date, fit on appearances
before the week and predict the week's appearances. Score P(over 0.5 / 1.5 / 2.5) for
shots and shots on target against a baseline of the player's season-to-date average.
The model must beat the baseline before any credits are spent on player prices.

Two views: "lineup known" (we know who starts; an upper bound) and "before lineups"
(the chance of starting; what version 1 can trade). Bets are void if a player doesn't
play, so both are scored on players who played.

Also reported: an ablation (the full model beside the model with each factor group
removed; groups that don't lower out-of-sample log loss are dropped), the choice of
shots-on-target method, and a check that a team's expected player shots add up to its
expected team shots.

Stage 2 (with DraftKings prices) applies the player trade rule to historical player
odds and reports the match backtest's metrics split by market, line, position and
starter or substitute.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from soccer_stats import trades as tr
from soccer_stats.factors import ALL_FACTORS, FACTOR_GROUPS
from soccer_stats.models.player_counts import (
    PlayerShotModel,
    baseline_pmf,
    prob_over,
    season_averages,
)
from soccer_stats.player_data import match_in_fixture

LINES = (0.5, 1.5, 2.5)
TOLERANCE = 0.15  # expected player shots vs expected team shots
COUNTS = {"shots": "shots", "sot": "sot"}


def match_info(preds: pd.DataFrame, apps: pd.DataFrame) -> pd.DataFrame:
    """Team expected goals and game state per Understat match, from walk_forward output.

    `preds` (football-data team names) has home, away, season, exp_home, exp_away,
    p_home, p_away; joined to appearances on teams and season.
    """
    if preds.empty or apps.empty:
        return pd.DataFrame(columns=["match_id", "team", "team_xg", "game_state"])
    keys = apps.drop_duplicates("match_id")[["match_id", "season", "team", "opponent", "home"]]
    keys = keys.assign(
        h=np.where(keys["home"], keys["team"], keys["opponent"]),
        a=np.where(keys["home"], keys["opponent"], keys["team"]),
    )
    p = preds.assign(season=preds["season"].astype(str))
    m = keys.merge(
        p,
        left_on=["h", "a", "season"],
        right_on=["home", "away", "season"],
        how="inner",
        suffixes=("", "_fd"),
    )
    rows = []
    for r in m.itertuples(index=False):
        rows.append(
            {
                "match_id": r.match_id,
                "team": r.h,
                "team_xg": r.exp_home,
                "game_state": r.p_home - r.p_away,
            }
        )
        rows.append(
            {
                "match_id": r.match_id,
                "team": r.a,
                "team_xg": r.exp_away,
                "game_state": r.p_away - r.p_home,
            }
        )
    return pd.DataFrame(rows)


def walk_forward(
    feats: pd.DataFrame,
    start: str | pd.Timestamp,
    factors: list[str] | None = None,
    lineup_known: bool = False,
    sot_method: str = "thin",
    refit_every: str = "7D",
    lookback_days: int = 730,
    min_prev_apps: int = 3,
) -> pd.DataFrame:
    """Out-of-sample predictions for each appearance from `start` on.

    Each week's model is fitted only on appearances before the week. Players with fewer
    than `min_prev_apps` earlier appearances are left out (too little to price).
    """
    feats = season_averages(feats)
    start = pd.Timestamp(start, tz="UTC") if pd.Timestamp(start).tz is None else pd.Timestamp(start)
    if not lineup_known:  # absences aren't known before lineups in the backtest
        feats = feats.assign(absent_share=0.0)
    weeks = pd.date_range(start, feats["kickoff"].max() + pd.Timedelta(days=1), freq=refit_every)
    out = []
    for lo, hi in zip(weeks[:-1], weeks[1:], strict=True):
        test = feats[(feats["kickoff"] >= lo) & (feats["kickoff"] < hi)]
        test = test[test["prev_apps"] >= min_prev_apps]
        if test.empty:
            continue
        train = feats[
            (feats["kickoff"] < lo) & (feats["kickoff"] >= lo - pd.Timedelta(days=lookback_days))
        ]
        if len(train) < 200:
            continue
        model = PlayerShotModel(factors, sot_method=sot_method).fit(train)
        d = model.distributions(test, lineup_known)
        base = {c: baseline_pmf(test, c) for c in COUNTS}
        rec = test[
            [
                "match_id",
                "season",
                "kickoff",
                "team",
                "opponent",
                "player_id",
                "player",
                "position",
                "started",
                "minutes",
                "shots",
                "sot",
                "team_shots",
                "team_shots_exp",
                "season_avg_shots",
                "season_avg_sot",
            ]
        ].copy()
        for c in COUNTS:
            pmf, bpmf = d[c], base[c]
            y = np.minimum(test[c].to_numpy(), pmf.shape[1] - 1)
            rec[f"ll_{c}"] = -np.log(np.clip(pmf[np.arange(len(y)), y], 1e-12, None))
            rec[f"ll_{c}_base"] = -np.log(np.clip(bpmf[np.arange(len(y)), y], 1e-12, None))
            rec[f"mean_{c}"] = (pmf * np.arange(pmf.shape[1])).sum(axis=1)
            for line in LINES:
                rec[f"p_{c}_o{line}"] = prob_over(pmf, line)
                rec[f"b_{c}_o{line}"] = prob_over(bpmf, line)
            rec[f"pmf_{c}"] = list(pmf.round(5))
        out.append(rec)
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def _bin_ll(p: np.ndarray, y: np.ndarray) -> float:
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def score(preds: pd.DataFrame) -> dict:
    """Log loss of P(over line) for each count and line, model vs baseline."""
    if preds.empty:
        return {}
    out = {"appearances": len(preds)}
    for c in COUNTS:
        rows = []
        for line in LINES:
            y = (preds[c] > line).astype(float).to_numpy()
            rows.append(
                {
                    "line": line,
                    "model": _bin_ll(preds[f"p_{c}_o{line}"].to_numpy(), y),
                    "baseline": _bin_ll(preds[f"b_{c}_o{line}"].to_numpy(), y),
                    "rate": float(y.mean()),
                }
            )
        model = float(np.mean([r["model"] for r in rows]))
        base = float(np.mean([r["baseline"] for r in rows]))
        out[c] = {
            "lines": rows,
            "model": model,
            "baseline": base,
            "count_ll": float(preds[f"ll_{c}"].mean()),
            "count_ll_baseline": float(preds[f"ll_{c}_base"].mean()),
            "beats_baseline": model < base,
        }
    return out


def reconcile_team_totals(preds: pd.DataFrame) -> dict:
    """Do a team's expected player shots add up to its expected team shots?

    Compares, per team-match, the sum of players' expected shots (lineup known) with the
    team's expected shots from its own and its opponent's shot rates, and with the
    actual team total.
    """
    if preds.empty:
        return {}
    g = preds.groupby(["match_id", "team"]).agg(
        players=("mean_shots", "sum"),
        team_exp=("team_shots_exp", "first"),
        actual=("team_shots", "first"),
        n=("player_id", "size"),
    )
    g = g[g["n"] >= 10].dropna()
    if g.empty:
        return {}
    ratio = float(g["players"].sum() / g["team_exp"].sum())
    return {
        "team_matches": len(g),
        "ratio_to_expected": ratio,
        "ratio_to_actual": float(g["players"].sum() / g["actual"].sum()),
        "tolerance": TOLERANCE,
        "within_tolerance": abs(ratio - 1) <= TOLERANCE,
    }


def ablation(
    feats: pd.DataFrame, start, base_score: dict, lineup_known: bool = False, **kw
) -> dict:
    """The full model's log loss beside the model with each factor group removed.

    A group is kept only if removing it makes out-of-sample log loss worse.
    """
    rows, keep = [], []
    full = np.mean([base_score[c]["model"] for c in COUNTS])
    for group, cols in FACTOR_GROUPS.items():
        factors = [f for f in ALL_FACTORS if f not in cols]
        s = score(walk_forward(feats, start, factors, lineup_known, **kw))
        ll = np.mean([s[c]["model"] for c in COUNTS]) if s else float("nan")
        helps = bool(ll > full)  # removing it hurts -> it helps
        rows.append(
            {"without": group, "log_loss": float(ll), "change": float(ll - full), "kept": helps}
        )
        if helps:
            keep.append(group)
    return {"full": float(full), "groups": rows, "kept_groups": keep}


def kept_factors(groups: list[str]) -> list[str]:
    return [f for g in groups for f in FACTOR_GROUPS[g]]


# ---------- stage 2: priced backtest ----------


def best_player_picks(
    lines: pd.DataFrame, threshold: float = tr.PAPER_EDGE, cap_per_match: int = tr.MAX_PLAYER_TRADES
) -> pd.DataFrame:
    """Apply the player trade rule to priced lines (see trades.player_picks)."""
    return tr.player_picks(lines, threshold, cap_per_match)


MARKET_COUNT = {"player_shots": "shots", "player_shots_on_target": "sot"}


def map_odds_players(odds: pd.DataFrame, apps: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Add Understat player_id/team to odds rows by name, within the match's two teams.

    A name that matches in both teams, or in neither, is skipped and counted.
    """
    if odds.empty:
        return odds.assign(player_id=[], team=[]), 0
    roster = apps.drop_duplicates(["season", "team", "player_id"])
    out, skipped = [], set()
    for (home, away, season), g in odds.groupby(["home", "away", "season"]):
        rosters = {}
        for team in (home, away):
            cands = roster[(roster["team"] == team) & (roster["season"] == season)]
            rosters[team] = dict(zip(cands["player_id"], cands["player"], strict=True))
        found, missed = match_in_fixture(g["player"].unique(), rosters)
        skipped.update((home, away, season, n) for n in missed)
        for name, (pid, team) in found.items():
            out.append(g[g["player"] == name].assign(player_id=pid, team=team))
    df = pd.concat(out, ignore_index=True) if out else odds.iloc[:0].assign(player_id=[], team=[])
    return df, len(skipped)


def priced_trades(
    preds: pd.DataFrame,
    history: pd.DataFrame,
    apps: pd.DataFrame,
    threshold: float = tr.PAPER_EDGE,
    league: str = "E0",
) -> tuple[pd.DataFrame, dict]:
    """Stage 2: the player trade rule on historical DraftKings player odds.

    `preds` are before-lineups walk-forward predictions (with pmf_shots / pmf_sot);
    `history` is player_odds.load_history() output. Trades open at the look (3 hours
    before kickoff), close at the last price before kickoff, and settle on Understat's
    counts. Priced players who didn't play are void (the bookmaker refunds them).
    """
    info = {"priced_sides": len(history), "unmatched_names": 0, "no_prediction": 0}
    if history.empty or preds.empty:
        return pd.DataFrame(), info
    h = history.copy()
    h["season"] = [tr.season_label(k) for k in h["kickoff"]]
    h, info["unmatched_names"] = map_odds_players(h, apps)
    look, close = h[h["kind"] == "look"], h[h["kind"] == "close"]
    p = preds.copy()
    rows = []
    for r in look.itertuples(index=False):
        m = p[
            (p["player_id"] == r.player_id)
            & (p["season"] == r.season)
            & ((p["kickoff"] - r.kickoff).abs() <= pd.Timedelta(days=2))
        ]
        if m.empty:
            info["no_prediction"] += 1  # didn't play (void) or too little history
            continue
        m = m.iloc[0]
        pmf = np.asarray(m[f"pmf_{MARKET_COUNT[r.market]}"])
        p_over = float(pmf[int(np.floor(r.line)) + 1 :].sum())
        rows.append(
            {
                **r._asdict(),
                "p": p_over if r.side == "over" else 1 - p_over,
                "actual": int(m[MARKET_COUNT[r.market]]),
                "started": bool(m["started"]),
                "position": m["position"],
            }
        )
    lines = pd.DataFrame(rows)
    picks = tr.player_picks(lines, threshold)
    trades = []
    for r in picks.to_dict("records"):
        t = tr.new_trade(
            {
                "market": r["market"],
                "odds": r["odds"],
                "model_p": r["p"],
                "edge": r["edge"],
                "line": r["line"],
                "side": r["side"],
            },
            source="backtest",
            league=league,
            home=r["home"],
            away=r["away"],
            kickoff=r["kickoff"],
            opened_at=r["snapshot_ts"],
            odds_fetched_at=str(r["snapshot_ts"]),
            threshold=threshold,
            look=f"{3:g}h",
            player={"player": r["player"], "player_id": r["player_id"], "team": r["team"]},
        )
        c = close[
            (close["player_id"] == r["player_id"])
            & (close["market"] == r["market"])
            & (close["line"] == r["line"])
            & ((close["kickoff"] - r["kickoff"]).abs() <= pd.Timedelta(days=2))
        ]
        over = c.loc[c["side"] == "over", "odds"]
        under = c.loc[c["side"] == "under", "odds"]
        pair = tr.devig_pair(
            over.iloc[0] if len(over) else None, under.iloc[0] if len(under) else None
        )
        same = c.loc[c["side"] == r["side"], "odds"]
        t["close_odds"] = float(same.iloc[0]) if len(same) else None
        if pair:
            t["clv_dk"] = t["odds"] * (pair[0] if r["side"] == "over" else pair[1]) - 1
        t.update(tr.settle_player(t, r["actual"], r["started"]))
        t["position"] = r["position"]
        t["settled_at"] = t["kickoff"]
        trades.append(t)
    return pd.DataFrame(trades), info


def player_report(preds: pd.DataFrame) -> dict:
    """Per-player backtest results for the app: expected vs actual shots, by name.

    `preds` are walk-forward predictions (one row per appearance, made before the
    match). Returns a summary row per player and his match-by-match record.
    """
    if preds.empty:
        return {"players": [], "apps": {}}
    p = preds.sort_values("kickoff")
    y1 = (p["shots"] > 0.5).astype(float)
    p = p.assign(
        bin_ll=-(
            y1 * np.log(p["p_shots_o0.5"].clip(1e-6, 1 - 1e-6))
            + (1 - y1) * np.log((1 - p["p_shots_o0.5"]).clip(1e-6, 1 - 1e-6))
        ),
        bin_ll_base=-(
            y1 * np.log(p["b_shots_o0.5"].clip(1e-6, 1 - 1e-6))
            + (1 - y1) * np.log((1 - p["b_shots_o0.5"]).clip(1e-6, 1 - 1e-6))
        ),
    )
    g = p.groupby("player_id")
    summary = pd.DataFrame(
        {
            "player": g["player"].last(),
            "team": g["team"].last(),
            "position": g["position"].last(),
            "apps": g.size(),
            "starts": g["started"].sum(),
            "minutes": g["minutes"].sum(),
            "exp_shots": g["mean_shots"].sum(),
            "shots": g["shots"].sum(),
            "exp_sot": g["mean_sot"].sum(),
            "sot": g["sot"].sum(),
            "ll": g["ll_shots"].mean(),
            "ll_base": g["ll_shots_base"].mean(),
            "last": g["kickoff"].max(),
        }
    ).reset_index()
    summary["beats_baseline"] = summary["ll"] < summary["ll_base"]
    summary["last"] = summary["last"].dt.strftime("%Y-%m-%d")
    cols = [
        "kickoff",
        "opponent",
        "started",
        "minutes",
        "mean_shots",
        "shots",
        "mean_sot",
        "sot",
        "p_shots_o0.5",
        "p_shots_o1.5",
    ]
    apps = {}
    for pid, sub in p.groupby("player_id"):
        apps[pid] = [
            [
                k.strftime("%Y-%m-%d"),
                opp,
                bool(st),
                int(mi),
                round(float(es), 2),
                int(sh),
                round(float(et), 2),
                int(so),
                round(float(p1), 3),
                round(float(p2), 3),
            ]
            for k, opp, st, mi, es, sh, et, so, p1, p2 in sub[cols].itertuples(index=False)
        ]
    return {
        "fields": [
            "date",
            "opponent",
            "started",
            "minutes",
            "exp_shots",
            "shots",
            "exp_sot",
            "sot",
            "p_1plus",
            "p_2plus",
        ],
        "players": summary.round(3).sort_values("exp_shots", ascending=False).to_dict("records"),
        "apps": apps,
    }
