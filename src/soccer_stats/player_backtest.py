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

Stage 2 (with FanDuel prices) applies the player trade rule to historical player
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
    over_min,
    prob_over,
    season_averages,
)
from soccer_stats.player_data import match_in_fixture
from soccer_stats.player_odds import PLAYER_BOOKMAKER_NAME

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
    end = feats["kickoff"].max() + pd.Timedelta(refit_every)
    weeks = pd.date_range(start, end, freq=refit_every)
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


def score_by_season(preds: pd.DataFrame) -> dict:
    """score() per season: appearances and model vs baseline log loss for each count."""
    if preds.empty:
        return {}
    out = {}
    for season, g in preds.groupby(preds["season"].astype(str)):
        s = score(g)
        out[season] = {
            "appearances": s["appearances"],
            **{
                c: {
                    "model": round(s[c]["model"], 4),
                    "baseline": round(s[c]["baseline"], 4),
                    "beats_baseline": s[c]["beats_baseline"],
                }
                for c in COUNTS
            },
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


STRATEGIES = {
    # name: (snapshot, chance used, starters only)
    "raw_3h": ("look", "model", False),  # the original rule, for comparison
    "blend_3h": ("look", "blend", False),
    "blend_lineup": ("close", "blend", True),  # the strategy the app trades
}
MAIN_STRATEGY = "blend_lineup"
SWEEP = (0.02, 0.05, 0.08, 0.12, 0.20)


def _priced_lines(snap: pd.DataFrame, preds: pd.DataFrame, info: dict, key: str) -> pd.DataFrame:
    """One row per priced side with the model's chance, the outcome and the implied chance.

    Players without a prediction (didn't play: void; or too little history) are dropped.
    """
    if snap.empty or preds.empty:
        return pd.DataFrame()
    p = preds[["player_id", "season", "kickoff", "started", "position", "shots", "sot"]].copy()
    p["pmf_shots"], p["pmf_sot"] = preds["pmf_shots"], preds["pmf_sot"]
    p["mean_shots"], p["mean_sot"] = preds["mean_shots"], preds["mean_sot"]
    p["pkick"] = p.pop("kickoff")
    m = snap.merge(p, on=["player_id", "season"], how="inner")
    m = m[(m["pkick"] - m["kickoff"]).abs() <= pd.Timedelta(days=2)]
    m = m.drop_duplicates(["event_id", "player_id", "market", "line", "side"])
    info[f"no_prediction_{key}"] = int(len(snap) - len(m))
    if m.empty:
        return pd.DataFrame()
    count = m["market"].map(MARKET_COUNT)
    p_over = [
        float(np.asarray(pm_s if c == "shots" else pm_t)[over_min(ln) :].sum())
        for pm_s, pm_t, c, ln in zip(m["pmf_shots"], m["pmf_sot"], count, m["line"], strict=True)
    ]
    m["p_model"] = np.where(m["side"] == "over", p_over, 1 - np.asarray(p_over))
    m["actual"] = np.where(count == "shots", m["shots"], m["sot"]).astype(int)
    m["exp_count"] = np.where(count == "shots", m["mean_shots"], m["mean_sot"])
    hit = m["actual"] >= np.ceil(m["line"])
    m["won"] = np.where(m["side"] == "over", hit, ~hit).astype(int)
    # The bookmaker's margin-free chance when both sides are priced, else 1 / odds.
    pairs = {}
    for k, g in m.groupby(["event_id", "player_id", "market", "line"]):
        sides = dict(zip(g["side"], g["odds"], strict=False))
        pr = tr.devig_pair(sides.get("over"), sides.get("under"))
        if pr:
            pairs[k] = pr
    m["implied"] = [
        (pairs[k][0] if s == "over" else pairs[k][1]) if k in pairs else 1 / o
        for k, s, o in zip(
            zip(m["event_id"], m["player_id"], m["market"], m["line"], strict=True),
            m["side"],
            m["odds"],
            strict=True,
        )
    ]
    drop = ["pmf_shots", "pmf_sot", "mean_shots", "mean_sot", "pkick", "shots", "sot"]
    return m.drop(columns=drop).reset_index(drop=True)


def _diagnostics(lines: pd.DataFrame, info: dict) -> None:
    info["lines_with_prediction"] = len(lines)
    e = lines["p_model"] * lines["odds"] - 1
    info["edge_quantiles"] = {q: round(float(e.quantile(q)), 4) for q in (0.5, 0.9, 0.99, 1.0)}
    info["odds_range"] = [
        round(float(lines["odds"].min()), 2),
        round(float(lines["odds"].max()), 2),
    ]
    info["p_mean"] = round(float(lines["p_model"].mean()), 4)
    info["lines_seen"] = {str(k): int(v) for k, v in lines["line"].value_counts().head(12).items()}
    info["implied_from_price_only"] = int((lines["side"] == "over").sum())


def calibration_table(lines: pd.DataFrame) -> list[dict]:
    """Implied vs model vs blend vs actual win rate by implied-chance bucket (all lines)."""
    bins = [0, 0.05, 0.1, 0.2, 0.35, 0.5, 0.7, 1.0]
    g = lines.groupby(pd.cut(lines["implied"], bins), observed=True)
    out = []
    for b, x in g:
        out.append(
            {
                "bucket": f"{b.left:.0%}-{b.right:.0%}",
                "lines": len(x),
                "implied": round(float(x["implied"].mean()), 4),
                "model": round(float(x["p_model"].mean()), 4),
                "blend": round(float(x["p"].mean()), 4) if x["p"].notna().any() else None,
                "won": round(float(x["won"].mean()), 4),
            }
        )
    return out


def priced_trades(
    preds: pd.DataFrame,
    history: pd.DataFrame,
    apps: pd.DataFrame,
    threshold: float = tr.PAPER_EDGE,
    league: str = "E0",
    known: pd.DataFrame | None = None,
    cal_min_lines: int | None = None,
) -> tuple[pd.DataFrame, dict]:
    """Stage 2: the player trade rule on historical FanDuel player odds.

    `preds` are before-lineups walk-forward predictions and `known` the lineup-known
    ones (with pmf_shots / pmf_sot); `history` is player_odds.load_history() output.

    Three strategies (STRATEGIES): the raw model at the 3-hour look; the blended chance
    (player_calibration) at the 3-hour look; and the blended chance on confirmed
    starters at FanDuel's last price before kickoff, after lineups are out. The last
    one is the main result (its trades are returned); the others are summarised in
    info["strategies"] with a threshold sweep. 3-hour trades close at the last price
    before kickoff (CLV); lineup trades open there, so they have no CLV. Players who
    didn't play are void (the bookmaker refunds them).
    """
    from soccer_stats import player_calibration as cal

    info = {"priced_sides": len(history), "unmatched_names": 0, "no_prediction": 0}
    if history.empty or preds.empty:
        return pd.DataFrame(), info
    h = history.copy()
    h["season"] = [tr.season_label(k) for k in h["kickoff"]]
    h, info["unmatched_names"] = map_odds_players(h, apps)
    look, close = h[h["kind"] == "look"], h[h["kind"] == "close"]
    info["look_sides_matched"] = len(look)
    info["close_sides_matched"] = len(close)
    info["sides_by_name"] = h["side"].value_counts().to_dict() if not h.empty else {}

    lines = {"look": _priced_lines(look, preds, info, "look")}
    lines["close"] = _priced_lines(close, known if known is not None else preds, info, "close")
    info["no_prediction"] = info.get("no_prediction_look", 0)
    if lines["look"].empty:
        return pd.DataFrame(), info
    _diagnostics(lines["look"], info)
    info["calibration"] = {}
    for kind, df in lines.items():
        if df.empty:
            continue
        n_min = cal_min_lines or cal.MIN_LINES
        df["p"], fits = cal.walk_forward(df, min_lines=n_min)
        info["calibration"][kind] = {
            "fits": len(fits),
            "last": fits[-1] if fits else None,
            # what the live app uses: a fit on every settled line
            "coef": cal.fit(df["implied"], df["p_model"], df["won"], n_min),
            "table": calibration_table(df),
        }

    close_by = (
        {
            k: g["odds"].iloc[0]
            for k, g in lines["close"].groupby(["event_id", "player_id", "market", "line", "side"])
        }
        if not lines["close"].empty
        else {}
    )
    results, info["strategies"] = {}, {}
    for name, (kind, chance, starters) in STRATEGIES.items():
        df = lines[kind]
        if df.empty:
            continue
        df = df.assign(p=df["p_model"] if chance == "model" else df["p"]).dropna(subset=["p"])
        if starters:
            df = df[df["started"].astype(bool)]
        sweep = {}
        for th in SWEEP:
            t = _build_trades(tr.player_picks(df, th), th, league, kind, close_by)
            sweep[f"{th:.0%}"] = tr.summarize(t) if not t.empty else {"trades": 0}
        t = _build_trades(tr.player_picks(df, threshold), threshold, league, kind, close_by)
        results[name] = t
        info["strategies"][name] = {
            "trades": compact_trades(t),
            "snapshot": "3 hours before" if kind == "look" else "after lineups",
            "chance": chance,
            "starters_only": starters,
            "lines": len(df),
            "summary": tr.summarize(t) if not t.empty else {"trades": 0},
            "sweep": sweep,
        }
    info["main_strategy"] = MAIN_STRATEGY
    info["trade_fields"] = list(TRADE_FIELDS)
    info["_lines"] = lines  # for the CLI to save; not JSON
    return results.get(MAIN_STRATEGY, pd.DataFrame()), info


# Bet-by-bet trades of every strategy at the main threshold, one compact row each
# (strategies[name]["trades"], columns in info["trade_fields"]), oldest first, so the
# app can chart any strategy. About 100 bytes a bet instead of ~900 for a full trade.
TRADE_FIELDS = (
    "date",
    "home",
    "away",
    "player",
    "market",
    "line",
    "side",
    "odds",
    "p",
    "implied",
    "edge",
    "actual",
    "status",
    "profit",
    "clv",
)


def compact_trades(trades: pd.DataFrame) -> list[list]:
    """Trades -> rows in TRADE_FIELDS order (void bets kept: status "void", profit 0)."""
    if trades.empty:
        return []
    t = trades.sort_values(["kickoff", "home", "player", "market"])
    clv = t["clv_dk"] if "clv_dk" in t else pd.Series(None, index=t.index)
    out = []
    for r, c in zip(t.to_dict("records"), clv, strict=True):
        out.append(
            [
                pd.Timestamp(r["kickoff"]).strftime("%Y-%m-%d"),
                r["home"],
                r["away"],
                r["player"],
                "shots" if r["market"] == "player_shots" else "sot",
                float(r["line"]),
                r["side"],
                round(float(r["odds"]), 2),
                round(float(r["model_p"]), 3),
                round(float(r["implied"]), 3),
                round(float(r["edge"]), 3),
                int(r["actual"]) if pd.notna(r.get("actual")) else None,
                r["status"],
                round(float(r["profit"] or 0), 2),
                round(float(c), 3) if c is not None and pd.notna(c) else None,
            ]
        )
    return out


def _build_trades(picks: pd.DataFrame, threshold: float, league: str, kind: str, close_by: dict):
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
            model_p_base=r["p_model"],
            look="3h" if kind == "look" else "lineup",
            player={"player": r["player"], "player_id": r["player_id"], "team": r["team"]},
        )
        if kind == "look":
            c = close_by.get((r["event_id"], r["player_id"], r["market"], r["line"], r["side"]))
            t["close_odds"] = float(c) if c else None
            if c:
                t["clv_dk"] = t["odds"] / c - 1  # over-only: price change at the close
        t.update(tr.settle_player(t, r["actual"], r["started"]))
        t["position"] = r["position"]
        t["implied"] = r["implied"]
        t["bookmaker"] = PLAYER_BOOKMAKER_NAME
        t["settled_at"] = t["kickoff"]
        trades.append(t)
    return pd.DataFrame(trades)


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
