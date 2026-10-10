"""Goalscorer model improvements through the lab harness (docs/player_props.md §8).

Pre-registered before any run: candidates A–H, development 2023/24–2024/25 (starters,
lineup known), 7 comparisons with 99.29% (Bonferroni) ranges on the paired log-loss
gain, a tail-calibration rule, 2025/26 reported only as "seen", and a forward window
(2026/27 from 2026-10-10) that stays locked until it is opened once with a reason.

Every feature here uses only matches that kicked off strictly earlier, like
`player_goals.goal_features`. Candidates are fitted through `lab.harness.walk_forward`
(refit every 28 days on the 730 days before the block); H's settings are chosen per
season on the season before (nested), never on the season scored.
"""

from __future__ import annotations

import re

import numpy as np
import pandas as pd
from scipy.special import expit, logit
from scipy.stats import rankdata

from soccer_stats.factors import PRIOR_90S, before_kickoff
from soccer_stats.lab import harness
from soccer_stats.lab.harness import Holdout
from soccer_stats.lab.metrics import boot_range
from soccer_stats.models.player_goals import GOAL_FACTORS, GoalscorerModel, p_zero
from soccer_stats.player_goals import FORWARD_START

SEEN_START = pd.Timestamp("2025-07-01", tz="UTC")
DEV_SEASONS = ("2324", "2425")
TUNE_SEASON = "2223"
LEVEL = 1 - 0.05 / 7  # Bonferroni over 7 comparisons: 99.29%
REFIT = "28D"
LOOKBACK_DAYS = 730
MIN_PREV_APPS = 3
TAIL_BUCKETS = ((0.20, 0.30), (0.30, 1.0))
DEFAULT_SP_XG = 0.01  # set-piece xG per 90 before any history
POS_SHARE_PRIOR = 2.0  # team-xG units of shrinkage for the xG share

C_FEATS = ["log_sp_xg_rate", "pen_taker"]
D_FEATS = ["log_opp_xga", "log_opp_xga_pos"]
F_FEATS = ["log_xg_rate_short", "log_xg_rate_long"]
G_FEATS = [*GOAL_FACTORS, *C_FEATS, *D_FEATS, "log_xg_share", *F_FEATS]
CANDIDATES = {
    # name: (factors, lineup known, kind)
    "A": (list(GOAL_FACTORS), False, "glm"),
    "B": (list(GOAL_FACTORS), True, "glm"),
    "C": ([*GOAL_FACTORS, *C_FEATS], True, "glm"),
    "D": ([*GOAL_FACTORS, *D_FEATS], True, "glm"),
    "E": (["log_xg_share" if f == "log_xg_rate" else f for f in GOAL_FACTORS], True, "glm"),
    "F": ([*GOAL_FACTORS, *F_FEATS], True, "glm"),
    "G": (G_FEATS, True, "glm"),
    "H": (G_FEATS, True, "gbm"),
}
REFERENCE = {"B": "A", "C": "B", "D": "B", "E": "B", "F": "B", "G": "B", "H": "B"}
GBM_GRID = [
    {"num_leaves": nl, "min_child_samples": mc, "n_estimators": 200, "learning_rate": 0.03}
    for nl in (4, 8)
    for mc in (100, 400)
]


# ---------- features (earlier kickoffs only) ----------


def _ewsum(x: pd.Series, by: pd.Series, half_life: float) -> pd.Series:
    """Recency-weighted sum of each group's earlier rows (rows in time order)."""
    decay = 0.5 ** (1 / half_life)
    vals = np.nan_to_num(x.to_numpy(dtype=float))
    out = np.zeros(len(vals))
    for idx in by.groupby(by, sort=False).indices.values():
        s = 0.0
        for i in idx:
            out[i] = s
            s = decay * s + vals[i]
    return pd.Series(out, index=x.index)


def _rate(df, col, pos_rate, half_life):
    s, n = (
        _ewsum(df[col], df["player_id"], half_life),
        _ewsum(df["_90s"], df["player_id"], half_life),
    )
    return (s + pos_rate * PRIOR_90S) / (n + PRIOR_90S)


def extra_features(gf: pd.DataFrame) -> pd.DataFrame:
    """C, D, E and F features on `player_goals.goal_features` output."""
    df = gf.sort_values(["kickoff", "match_id", "team"]).reset_index(drop=True).copy()
    for col in ("sp_xg", "pen_xg", "penalties"):
        df[col] = df[col].fillna(0).astype(float) if col in df else 0.0
    df["_90s"] = df["minutes"] / 90
    df["_one"] = 1.0
    pos = before_kickoff(df, ["position"], ["xg", "sp_xg", "_90s"])
    pos_xg = (pos["xg"] / pos["_90s"].replace(0, np.nan)).fillna(0.1)
    pos_sp = (pos["sp_xg"] / pos["_90s"].replace(0, np.nan)).fillna(DEFAULT_SP_XG)

    # C: set-piece xG per 90, and whether he took his team's most recent penalty.
    df["log_sp_xg_rate"] = np.log(_rate(df, "sp_xg", pos_sp, 10).clip(lower=0.001))
    df["pen_taker"] = _last_penalty_taker(df)

    # D: the opponent's xG conceded, overall and to his position, before this match.
    tm = (
        df.groupby(["match_id", "team"], sort=False)
        .agg(kickoff=("kickoff", "first"), opponent=("opponent", "first"), xg_for=("xg", "sum"))
        .reset_index()
    )
    tm = tm.merge(
        tm[["match_id", "team", "xg_for"]].rename(columns={"team": "opponent", "xg_for": "xga"}),
        on=["match_id", "opponent"],
        how="left",
    ).sort_values(["kickoff", "match_id"])
    tm = tm.reset_index(drop=True)
    tm["_one"] = 1.0
    conc = before_kickoff(tm, ["team"], ["xga", "_one"])
    lg = before_kickoff(tm, [], ["xga", "_one"])
    lg_avg = (lg["xga"] / lg["_one"].replace(0, np.nan)).fillna(1.4)
    tm["xga_rel"] = ((conc["xga"] + 3 * lg_avg) / (conc["_one"] + 3)) / lg_avg
    look = tm.set_index(["match_id", "team"])["xga_rel"]
    okey = list(zip(df["match_id"], df["opponent"], strict=True))
    df["log_opp_xga"] = np.log(pd.Series(look.reindex(okey).to_numpy()).clip(0.3, 3)).fillna(0)
    df["log_opp_xga_pos"] = _opp_position_xg(df)

    # E: his share of his team's xG in the matches he played, shrunk to his position's.
    team_xg = df.groupby(["match_id", "team"])["xg"].transform("sum")
    df["_team_xg"] = team_xg
    sp = before_kickoff(df, ["position"], ["xg", "_team_xg"])
    pos_share = (sp["xg"] / sp["_team_xg"].replace(0, np.nan)).fillna(0.06)
    s_x = _ewsum(df["xg"], df["player_id"], 10)
    s_t = _ewsum(df["_team_xg"], df["player_id"], 10)
    share = (s_x + pos_share * POS_SHARE_PRIOR) / (s_t + POS_SHARE_PRIOR)
    df["log_xg_share"] = np.log(share.clip(0.001, 1))

    # F: xG per 90 at two more speeds.
    df["log_xg_rate_short"] = np.log(_rate(df, "xg", pos_xg, 4).clip(lower=0.002))
    df["log_xg_rate_long"] = np.log(_rate(df, "xg", pos_xg, 40).clip(lower=0.002))
    return df.drop(columns=["_90s", "_one", "_team_xg"])


def _last_penalty_taker(df: pd.DataFrame) -> pd.Series:
    """1 if the player took his team's most recent penalty before this kickoff."""
    pens = df.loc[df["penalties"] > 0, ["team", "kickoff", "player_id"]].sort_values("kickoff")
    pens = pens.drop_duplicates(["team", "kickoff"], keep="last").rename(
        columns={"player_id": "taker"}
    )
    left = df[["team", "kickoff", "player_id"]].reset_index().sort_values("kickoff")
    if pens.empty:
        return pd.Series(0.0, index=df.index)
    m = pd.merge_asof(
        left, pens, on="kickoff", by="team", allow_exact_matches=False, direction="backward"
    )
    out = (m["taker"] == m["player_id"]).astype(float)
    return pd.Series(out.to_numpy(), index=m["index"].to_numpy()).reindex(df.index).fillna(0)


def _opp_position_xg(df: pd.DataFrame) -> pd.Series:
    """log(opponent's share of xG conceded to this position / league share), prior-only."""
    by = df.groupby(["match_id", "team", "opponent", "kickoff", "position"])["xg"].sum()
    by = by.reset_index()
    by["_all"] = by["xg"]
    conc = before_kickoff(by, ["opponent", "position"], ["xg"])["xg"]
    tot = before_kickoff(by, ["opponent"], ["_all"])["_all"]
    lg_pos = before_kickoff(by, ["position"], ["xg"])["xg"]
    lg_tot = before_kickoff(by, [], ["_all"])["_all"]
    share = (conc + 1.0 * lg_pos / lg_tot.replace(0, np.nan)) / (tot + 1.0)
    lg_share = lg_pos / lg_tot.replace(0, np.nan)
    by["f"] = np.log((share / lg_share).clip(0.5, 2)).fillna(0)
    look = by.set_index(["match_id", "team", "position"])["f"]
    key = list(zip(df["match_id"], df["team"], df["position"], strict=True))
    return pd.Series(look.reindex(key).to_numpy(), index=df.index).fillna(0)


# ---------- candidates ----------


class Candidate:
    """A goalscorer candidate for lab.harness: fitted when its block arrives on the
    LOOKBACK_DAYS before the block's first kickoff; [P(no goal), P(goal)], NaN for
    players with fewer than MIN_PREV_APPS earlier appearances."""

    def __init__(self, factors, lineup_known=True, kind="glm", gbm=None):
        self.factors, self.lineup_known, self.kind, self.gbm = factors, lineup_known, kind, gbm
        self.train = None

    def fit(self, train):
        self.train = train

    def predict(self, test):
        lo = test["kickoff"].min()
        tr = self.train[self.train["kickoff"] >= lo - pd.Timedelta(days=LOOKBACK_DAYS)]
        p = np.full(len(test), np.nan)
        ok = (test["prev_apps"] >= MIN_PREV_APPS).to_numpy()
        if len(tr) < 200 or tr["goals"].sum() < 10 or not ok.any():
            return np.column_stack([1 - p, p])
        t = test[ok]
        if self.kind == "glm":
            p[ok] = GoalscorerModel(self.factors).fit(tr).prob_score(t, self.lineup_known)
        else:
            p[ok] = _gbm_prob(tr, t, self.factors, self.gbm or GBM_GRID[0], self.lineup_known)
        return np.column_stack([1 - p, p])


def _gbm_prob(train, test, factors, params, lineup_known):
    import lightgbm as lgb

    model = lgb.LGBMRegressor(objective="poisson", verbose=-1, random_state=0, **params)
    model.fit(
        train[factors].to_numpy(float),
        train["goals"].to_numpy(float),
        init_score=np.log(np.clip(train["minutes"].to_numpy(float) / 90, 1e-3, None)),
    )
    rate = np.exp(model.predict(test[factors].to_numpy(float), raw_score=True))  # per 90
    ps = test["started"].astype(float).to_numpy() if lineup_known else test["start_rate"].to_numpy()
    st = rate * test["start_minutes"].to_numpy() / 90
    sb = rate * test["sub_minutes"].to_numpy() / 90
    return ps * (1 - p_zero(st, 0.0)) + (1 - ps) * (1 - p_zero(sb, 0.0))


def predict(feats, name, start, end, holdout=None, gbm=None) -> pd.Series:
    """One candidate's walk-forward P(goal) for rows with start <= kickoff < end."""
    factors, known, kind = CANDIDATES[name]
    pr = harness.walk_forward(
        feats,
        lambda **kw: Candidate(**kw),
        {"factors": factors, "lineup_known": known, "kind": kind, "gbm": gbm},
        start,
        end,
        holdout=holdout,
        time_col="kickoff",
        refit=REFIT,
        min_train=1,
    )
    return pr["p_1"] if not pr.empty else pd.Series(dtype=float)


def _season_bounds(feats, season):
    k = feats.loc[feats["season"].astype(str) == season, "kickoff"]
    return k.min(), k.max() + pd.Timedelta(days=1)


def predict_dev(feats, holdout=None, log=print) -> tuple[pd.DataFrame, dict]:
    """Every candidate on the development seasons; H tuned per season on the one before."""
    lo, _ = _season_bounds(feats, DEV_SEASONS[0])
    _, hi = _season_bounds(feats, DEV_SEASONS[-1])
    out = pd.DataFrame(index=feats.index[(feats["kickoff"] >= lo) & (feats["kickoff"] < hi)])
    for name, (_, _, kind) in CANDIDATES.items():
        if kind == "glm":
            out[name] = predict(feats, name, lo, hi, holdout)
            log(f"  {name}: {out[name].notna().sum()} predictions")
    chosen, parts = {}, []
    order = sorted(feats["season"].astype(str).unique())
    for season in DEV_SEASONS:
        if season not in order:
            continue
        i_s = order.index(season) if season in order else 0
        prev = order[i_s - 1] if i_s > 0 else None
        scores = {}
        plo, phi = _season_bounds(feats, prev) if prev else (None, None)
        for i, cfg in enumerate(GBM_GRID if prev else []):
            p = predict(feats[feats["kickoff"] < phi], "H", plo, phi, holdout, cfg)
            rows = feats.loc[p.index]
            keep = rows["started"].astype(bool) & p.notna()
            if keep.sum() > 100:
                y = (rows.loc[keep, "goals"] > 0).astype(int).to_numpy()
                scores[i] = float(_ll(p[keep].to_numpy(), y).mean())
        best = min(scores, key=scores.get) if scores else 0
        chosen[season] = {"config": GBM_GRID[best], "tune_log_loss": scores, "tuned_on": prev}
        slo, shi = _season_bounds(feats, season)
        parts.append(predict(feats[feats["kickoff"] < shi], "H", slo, shi, holdout, GBM_GRID[best]))
        log(f"  H {season}: config {best} (tuned on {prev})")
    out["H"] = pd.concat(parts) if parts else np.nan
    return out, chosen


# ---------- scoring ----------


def _ll(p, y):
    p = np.clip(np.asarray(p, float), 1e-6, 1 - 1e-6)
    y = np.asarray(y, float)
    return -(y * np.log(p) + (1 - y) * np.log(1 - p))


def _wilson(k, n, z=1.96):
    if n == 0:
        return (np.nan, np.nan)
    ph = k / n
    d = 1 + z * z / n
    c = (ph + z * z / (2 * n)) / d
    h = z * np.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n)) / d
    return (c - h, c + h)


def tail_rule(p, y) -> dict:
    """Pre-registered tail check, per bucket: predicted inside the observed rate's 95%
    range when that range's half-width is at most 3 points, else within 3 points."""
    out, ok_all = {}, True
    for lo, hi in TAIL_BUCKETS:
        m = (p >= lo) & (p < hi) if hi < 1 else p >= lo
        n = int(m.sum())
        if n == 0:
            out[f"{lo:.0%}+" if hi >= 1 else f"{lo:.0%}-{hi:.0%}"] = {"n": 0, "ok": True}
            continue
        pred, obs = float(p[m].mean()), float(y[m].mean())
        rlo, rhi = _wilson(int(y[m].sum()), n)
        ok = (rlo <= pred <= rhi) if (rhi - rlo) / 2 <= 0.03 else abs(pred - obs) <= 0.03
        ok_all &= bool(ok)
        out[f"{lo:.0%}+" if hi >= 1 else f"{lo:.0%}-{hi:.0%}"] = {
            "n": n,
            "predicted": round(pred, 4),
            "scored": round(obs, 4),
            "range95": [round(rlo, 4), round(rhi, 4)],
            "ok": bool(ok),
        }
    return {"buckets": out, "ok": ok_all}


def describe(p, y, groups, bench=None) -> dict:
    """Log loss, Brier with its Murphy decomposition, AUC, sharpness, tail check."""
    p, y = np.asarray(p, float), np.asarray(y, float)
    ll = _ll(p, y)
    bins = np.clip((p * 10).astype(int), 0, 9)
    base = y.mean()
    rel = res = 0.0
    for b in range(10):
        m = bins == b
        if m.any():
            rel += m.sum() * (p[m].mean() - y[m].mean()) ** 2
            res += m.sum() * (y[m].mean() - base) ** 2
    r = rankdata(p)
    n1, n0 = y.sum(), len(y) - y.sum()
    auc = (r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0) if n1 and n0 else np.nan
    out = {
        "rows": len(p),
        "matches": int(pd.Series(groups).nunique()),
        "scored": round(float(base), 4),
        "predicted": round(float(p.mean()), 4),
        "log_loss": round(float(ll.mean()), 5),
        "log_loss_range95": [round(v, 5) for v in boot_range(ll, groups)],
        "brier": round(float(((p - y) ** 2).mean()), 5),
        "reliability": round(rel / len(p), 6),
        "resolution": round(res / len(p), 6),
        "auc": round(float(auc), 4),
        "share_30plus": round(float((p >= 0.30).mean()), 4),
        "tail": tail_rule(p, y),
    }
    if bench is not None:
        out["log_loss_season_xg"] = round(float(_ll(bench, y).mean()), 5)
    return out


def compare(preds: pd.DataFrame, feats: pd.DataFrame, level: float = LEVEL) -> dict:
    """Every candidate on development starters, and the 7 pre-registered comparisons."""
    rows = feats.loc[preds.index]
    st = rows["started"].astype(bool) & rows["season"].astype(str).isin(DEV_SEASONS)
    cols = list(CANDIDATES)
    st &= preds[cols].notna().all(axis=1)
    y = (rows.loc[st, "goals"] > 0).astype(int).to_numpy()
    g = rows.loc[st, "match_id"].to_numpy()
    bench = 1 - np.exp(-rows.loc[st, "season_avg_xg"].clip(lower=1e-4).to_numpy())
    res = {"rows": int(st.sum()), "level": round(level, 4), "candidates": {}, "tests": {}}
    lls = {}
    for c in cols:
        p = preds.loc[st, c].to_numpy()
        lls[c] = _ll(p, y)
        res["candidates"][c] = describe(p, y, g, bench)
    for c, ref in REFERENCE.items():
        d = lls[ref] - lls[c]
        rng = boot_range(d, g, level=level)
        gain_ok = bool(rng and rng[0] > 0)
        tail_ok = res["candidates"][c]["tail"]["ok"]
        res["tests"][c] = {
            "vs": ref,
            "gain": round(float(d.mean()), 5),
            "range": [round(v, 5) for v in rng] if rng else None,
            "gain_passes": gain_ok,
            "tail_ok": tail_ok,
            "passes": gain_ok and tail_ok,
        }
    passing = [c for c in "CDEFGH" if res["tests"][c]["passes"]]
    res["chosen"] = min(passing, key=lambda c: res["candidates"][c]["log_loss"]) if passing else "B"
    return res


def score_window(preds: pd.DataFrame, feats: pd.DataFrame, names, ref="B", level=0.95):
    """Starters in a later window (seen 2025/26, or the forward check): each candidate's
    description and its paired gain over `ref` at a 95% range."""
    rows = feats.loc[preds.index]
    st = rows["started"].astype(bool) & preds[list(names)].notna().all(axis=1)
    y = (rows.loc[st, "goals"] > 0).astype(int).to_numpy()
    g = rows.loc[st, "match_id"].to_numpy()
    bench = 1 - np.exp(-rows.loc[st, "season_avg_xg"].clip(lower=1e-4).to_numpy())
    out = {"rows": int(st.sum()), "candidates": {}, "vs_ref": {}}
    for c in names:
        out["candidates"][c] = describe(preds.loc[st, c].to_numpy(), y, g, bench)
        if c != ref and ref in names:
            d = _ll(preds.loc[st, ref], y) - _ll(preds.loc[st, c], y)
            rng = boot_range(d, g, level=level)
            out["vs_ref"][c] = {"gain": round(float(d.mean()), 5), "range95": rng}
    return out


def forward_holdout(reason: str | None = None) -> Holdout:
    """The pre-registered forward window (2026/27 from 10 Oct), locked unless a reason
    is given."""
    h = Holdout(FORWARD_START)
    if reason:
        h.unlock(reason)
    return h


# ---------- the pilot, descriptively ----------

PRICE_LINE = re.compile(r"^\s*(\S+)\s+(.+?)\s+yes ([0-9.]+) no (\S+)\s*$")
MATCH_LINE = re.compile(r"pilot (.+) v (.+): (\d+) prices")


def parse_pilot_log(text: str) -> pd.DataFrame:
    """The round-5 pilot's printed prices (job log of odds-check run 37574303057) back
    into rows: match, book, player, yes."""
    rows, match = [], None
    for raw in text.splitlines():
        line = re.sub(r"^\S+Z ", "", raw)  # drop the log's timestamp
        m = MATCH_LINE.search(line)
        if m:
            match = (m.group(1).strip(), m.group(2).strip())
            continue
        p = PRICE_LINE.match(line)
        if p and match:
            rows.append(
                {
                    "home": match[0],
                    "away": match[1],
                    "book": p.group(1),
                    "player": p.group(2).strip(),
                    "yes": float(p.group(3)),
                }
            )
    return pd.DataFrame(rows, columns=["home", "away", "book", "player", "yes"])


# ---------- round 8: the same model in five leagues (docs/player_props.md §10) ----------

LEAGUE_SEASONS = ("2324", "2425", "2526")
NEW_LEAGUES = ("SP1", "D1", "I1", "F1")
FORWARD_LEAGUES = ("E0", *NEW_LEAGUES)
LEVEL_LEAGUE = 1 - 0.05 / 8  # 4 leagues x 2 benchmarks: 99.375%
FORWARD_MIN_MATCHES = 150
BENCHMARKS = {"season_goals": "bench_goals", "season_xg": "bench_xg"}


def scored_rows(feats, league, start, end, holdout=None, seasons=None) -> pd.DataFrame:
    """One row per appearance with A's and B's walk-forward chances, the outcome and the
    two season-to-date benchmarks (P(>=1) = 1 - exp(-mean))."""
    pa = predict(feats, "A", start, end, holdout)
    pb_ = predict(feats, "B", start, end, holdout)
    idx = pa.dropna().index.intersection(pb_.dropna().index)
    r = feats.loc[idx]
    out = pd.DataFrame(
        {
            "league": league,
            "season": r["season"].astype(str).to_numpy(),
            "match_id": (league + "|" + r["match_id"].astype(str)).to_numpy(),
            "kickoff": r["kickoff"].to_numpy(),
            "started": r["started"].astype(bool).to_numpy(),
            "scored": (r["goals"] > 0).astype(int).to_numpy(),
            "p_A": pa.loc[idx].to_numpy(),
            "p_B": pb_.loc[idx].to_numpy(),
            "bench_goals": 1 - np.exp(-r["season_avg_goals"].clip(lower=1e-4).to_numpy()),
            "bench_xg": 1 - np.exp(-r["season_avg_xg"].clip(lower=1e-4).to_numpy()),
        }
    )
    if seasons is not None:
        out = out[out["season"].isin(seasons)]
    return out.reset_index(drop=True)


def _gains(rows, col, level):
    y, g = rows["scored"].to_numpy(), rows["match_id"].to_numpy()
    ll_m = _ll(rows[col], y)
    res = {}
    for name, b in BENCHMARKS.items():
        d = _ll(rows[b], y) - ll_m
        rng = boot_range(d, g, level=level)
        res[name] = {
            "gain": round(float(d.mean()), 5),
            "range": [round(v, 5) for v in rng] if rng else None,
            "beats": bool(rng and rng[0] > 0),
        }
    return res


def bench_tests(rows: pd.DataFrame, level: float) -> dict:
    """The pre-registered comparisons on one league (or the pool).

    Gate: A on every appearance before lineups beats both benchmarks (range above 0).
    Betting view: B on starters with the lineup known, against the same benchmarks
    (they are per appearance, so they lean low on starters), plus B vs A.
    """
    if rows.empty:
        return {"rows": 0}
    y = rows["scored"].to_numpy()
    allv = {
        "rows": len(rows),
        "matches": int(rows["match_id"].nunique()),
        "model": describe(rows["p_A"].to_numpy(), y, rows["match_id"].to_numpy()),
        "vs": _gains(rows, "p_A", level),
    }
    allv["beats_both"] = all(v["beats"] for v in allv["vs"].values())
    st = rows[rows["started"]]
    starters = _starters_view(st, "p_B", level, ref="p_A")
    starters["vs_A"] = starters.pop("vs_ref")
    return {"level": round(level, 5), "all_before_lineups": allv, "starters_lineup_known": starters}


def _starters_view(st: pd.DataFrame, col: str, level: float, ref: str) -> dict:
    """One model on starters: description, gains over both benchmarks, and the paired
    log-loss gain over `ref` (ref minus model)."""
    ys, g = st["scored"].to_numpy(), st["match_id"].to_numpy()
    out = {
        "rows": len(st),
        "model": describe(st[col].to_numpy(), ys, g),
        "vs": _gains(st, col, level),
    }
    out["beats_both"] = all(v["beats"] for v in out["vs"].values())
    d = _ll(st[ref], ys) - _ll(st[col], ys)
    rng = boot_range(d, g, level=level)
    out["vs_ref"] = {"gain": round(float(d.mean()), 5), "range": rng}
    return out


def history_report(rows_by_league: dict[str, pd.DataFrame]) -> dict:
    """Per league at 99.375% and pooled over the four new leagues at 95%."""
    out = {"leagues": {}}
    for lg, r in rows_by_league.items():
        out["leagues"][lg] = bench_tests(r, LEVEL_LEAGUE)
    new = [r for lg, r in rows_by_league.items() if lg in NEW_LEAGUES]
    if new:
        out["pooled_new_leagues"] = bench_tests(pd.concat(new, ignore_index=True), 0.95)
    out["answer"] = {
        lg: out["leagues"][lg]["all_before_lineups"].get("beats_both")
        for lg in rows_by_league
        if out["leagues"][lg].get("all_before_lineups")
    }
    return out


def forward_report(rows: pd.DataFrame) -> dict:
    """The widened forward check (§10b): pooled gate on B's starters plus per league."""
    matches = int(rows["match_id"].nunique()) if len(rows) else 0
    out = {"matches": matches, "enough": matches >= FORWARD_MIN_MATCHES}
    if rows.empty:
        return out
    pooled = bench_tests(rows, 0.95)
    st = pooled["starters_lineup_known"]
    out["pooled"] = pooled
    out["gate"] = {
        "beats_both": st["beats_both"],
        "tail_ok": st["model"]["tail"]["ok"],
        "passes": bool(out["enough"] and st["beats_both"] and st["model"]["tail"]["ok"]),
    }
    out["leagues"] = {lg: bench_tests(r, 0.95) for lg, r in rows.groupby("league")}
    if "p_Bcal" in rows:  # §12: B-cal beside B, same gate; B stays the primary
        out["b_cal"] = _bcal_views(rows, 0.95)
        bc = out["b_cal"]["pooled"]
        out["b_cal"]["gate"] = {
            "beats_both": bc["beats_both"],
            "tail_ok": bc["model"]["tail"]["ok"],
            "passes": bool(out["enough"] and bc["beats_both"] and bc["model"]["tail"]["ok"]),
        }
    return out


# ---------- B-cal: B recalibrated per league (docs/player_props.md §12) ----------

CAL_MIN_ROWS = 2000


def add_bcal(rows: pd.DataFrame, history: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """`rows` with p_Bcal: for each league and season S, logit(p) = a + b*logit(p_B), fitted
    on that league's starters in `history` from seasons before S, applied to all of S.
    Fewer than CAL_MIN_ROWS earlier starters: p_Bcal = p_B. No earlier season: NaN."""
    from soccer_stats.player_lab import _fit_logit

    out = rows.copy()
    out["p_Bcal"] = np.nan
    coefs: dict = {}
    hist = history[history["started"].astype(bool)]
    for (lg, season), idx in out.groupby(["league", "season"]).groups.items():
        tr = hist[(hist["league"] == lg) & (hist["season"].astype(str) < str(season))]
        key = f"{lg} {season}"
        if tr.empty:
            coefs[key] = None
            continue
        p = out.loc[idx, "p_B"].clip(1e-6, 1 - 1e-6)
        if len(tr) < CAL_MIN_ROWS:
            out.loc[idx, "p_Bcal"] = p
            coefs[key] = {"rows": len(tr), "a": 0.0, "b": 1.0, "floor": True}
            continue
        a, b = _fit_logit(logit(tr["p_B"].clip(1e-6, 1 - 1e-6)), tr["scored"].to_numpy(float))
        out.loc[idx, "p_Bcal"] = expit(a + b * logit(p))
        seasons = sorted(tr["season"].astype(str).unique())
        coefs[key] = {"rows": len(tr), "a": round(a, 4), "b": round(b, 4), "fit_on": seasons}
    return out, coefs


def _bcal_views(rows: pd.DataFrame, level: float) -> dict:
    """B and B-cal on starters with B-cal defined, pooled and per league."""
    st = rows[rows["started"] & rows["p_Bcal"].notna()]

    def view(r):
        if r.empty:
            return {"rows": 0}
        bc = _starters_view(r, "p_Bcal", level, ref="p_B")
        bc["vs_B"] = bc.pop("vs_ref")
        y, g = r["scored"].to_numpy(), r["match_id"].to_numpy()
        bc["b"] = {"model": describe(r["p_B"].to_numpy(), y, g), "vs": _gains(r, "p_B", level)}
        return bc

    return {"pooled": view(st), "leagues": {lg: view(r) for lg, r in st.groupby("league")}}


def bcal_development(rows: pd.DataFrame) -> dict:
    """§12 development display: B vs B-cal on the seen seasons, descriptive (95%)."""
    rows, coefs = add_bcal(rows, rows)
    return {"coefficients": coefs, **_bcal_views(rows, 0.95)}
