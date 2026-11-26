"""An xG fallback without Understat: the live match model with Understat xG, goals only and a
shots proxy, on the same rows.

Pre-registered in docs/lab.md "An xG fallback without Understat" (owner via the lead, 26 Nov),
committed before this code. Development only: 2019/20-2023/24 scored, 2024/25 locked
(lab.harness.Holdout), no key, 0 credits, no live change.

Forms (the only difference is the xG columns the live model is given and its weight):
* a: Understat xG (xg.with_xg, the xg-good files), weight 0.7; = b where a league has none;
* b: goals only, weight 0;
* c: proxy goals per team = alpha*HST + beta*(HS - HST), fitted per league on 2017/18-2018/19
  (least squares, no intercept), weight 0.7;
* c_sot (descriptive): alpha' * HST alone, weight 0.7.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

from soccer_stats.lab.harness import Holdout

LEAGUES = ("E0", "SP1", "D1", "I1", "F1", "E1", "E2", "E3")
XG_LEAGUES = ("E0", "SP1", "D1", "I1", "F1")
SIDES = ("home", "draw", "away")
FORMS = ("a", "b", "c", "c_sot")
JUDGED = ("a", "b", "c")
LOCK = pd.Timestamp("2024-07-01")
SCORED_FROM = pd.Timestamp("2019-07-01")
FIRST_YEAR = 2017  # training rows from 2017/18
LAST_YEAR = 2023
PROXY_TO = SCORED_FROM  # the proxy's coefficients use matches before this only
WEIGHT = 0.7  # publish.XG_WEIGHT (a test pins it)
N_BOOT = 2000
SEED = 0
KEYS = ["league", "season", "home", "away"]


# --- the shots proxy ---------------------------------------------------------------------


def fd_shots(code: str, years) -> pd.DataFrame:
    """football-data's shots (HS/AS) and shots on target (HST/AST) per match, keyed by season,
    home, away (data.COLUMNS is left alone)."""
    from soccer_stats.data import download, season_code

    parts = []
    for y in years:
        raw = pd.read_csv(download(code, y), encoding="latin-1", on_bad_lines="skip")
        raw = raw.dropna(subset=[c for c in ("HomeTeam", "AwayTeam", "FTHG") if c in raw])
        out = pd.DataFrame(
            {"season": season_code(y), "home": raw["HomeTeam"], "away": raw["AwayTeam"]}
        )
        for col, name in (("HS", "home_hs"), ("AS", "away_hs"), ("HST", "home_hst"),
                          ("AST", "away_hst")):  # fmt: skip
            out[name] = pd.to_numeric(raw[col], errors="coerce") if col in raw else np.nan
        parts.append(out)
    return pd.concat(parts, ignore_index=True).drop_duplicates(["season", "home", "away"])


def _team_rows(m: pd.DataFrame) -> pd.DataFrame:
    """One row per team and match: goals, shots, shots on target."""
    rows = []
    for side in ("home", "away"):
        rows.append(pd.DataFrame({"goals": m[f"{side}_goals"].astype(float),
                                  "hs": m[f"{side}_hs"].astype(float),
                                  "hst": m[f"{side}_hst"].astype(float)}))  # fmt: skip
    return pd.concat(rows, ignore_index=True)


def fit_proxy(m: pd.DataFrame) -> dict:
    """alpha, beta >= 0 (goals ~ HST + (HS - HST), no intercept, non-negative least squares)
    and alpha' (goals ~ HST) on the matches of `m` (already restricted to before PROXY_TO)."""
    t = _team_rows(m)
    both = t.dropna(subset=["goals", "hs", "hst"])
    both = both[both["hs"] >= both["hst"]]
    one = t.dropna(subset=["goals", "hst"])
    out: dict = {"rows_both": int(len(both)), "rows_sot": int(len(one))}
    if len(both) >= 50:
        from scipy.optimize import nnls

        x = np.column_stack([both["hst"], both["hs"] - both["hst"]])
        coef = nnls(x, both["goals"].to_numpy(float))[0]  # amended 26 Nov: weights >= 0
        out["alpha"], out["beta"] = float(coef[0]), float(coef[1])
    if len(one) >= 50:
        h = one["hst"].to_numpy(float)
        out["alpha_sot"] = float(h @ one["goals"].to_numpy(float) / (h @ h))
    return out


def proxy(m: pd.DataFrame, coef: dict, sot_only: bool = False) -> pd.DataFrame:
    """home_xg/away_xg = the proxy (NaN without shots on target: the model uses goals)."""
    out = m.copy()
    for side in ("home", "away"):
        hst, hs = out[f"{side}_hst"].astype(float), out[f"{side}_hs"].astype(float)
        sot = coef.get("alpha_sot", np.nan) * hst
        if sot_only or "alpha" not in coef:
            v = sot
        else:
            full = coef["alpha"] * hst + coef["beta"] * (hs - hst)
            ok = hs.notna() & hst.notna() & (hs >= hst)
            v = full.where(ok, sot)
        out[f"{side}_xg"] = v
    return out


# --- one league ------------------------------------------------------------------------


def describe_train(train: pd.DataFrame) -> dict:
    """What a failed fit was given: rows, dates, teams, and the goal and xG columns' gaps and
    ranges (diagnostics only; nothing is changed)."""
    out = {"rows": int(len(train)), "teams": int(len(set(train["home"]) | set(train["away"])))}
    if len(train):
        out["dates"] = [str(pd.Timestamp(train["date"].min()).date()),
                        str(pd.Timestamp(train["date"].max()).date())]  # fmt: skip
    for c in ("home_goals", "away_goals", "home_xg", "away_xg"):
        if c in train:
            v = pd.to_numeric(train[c], errors="coerce").to_numpy(float)
            fin = v[np.isfinite(v)]
            out[c] = {"nan": int(np.isnan(v).sum()), "inf": int(np.isinf(v).sum()),
                      "min": float(fin.min()) if len(fin) else None,
                      "max": float(fin.max()) if len(fin) else None}  # fmt: skip
    dup = train.duplicated(["date", "home", "away"]).sum() if len(train) else 0
    out["duplicates"] = int(dup)
    return out


def checked_factory(code: str, form: str, weight: float):
    """dashboard.match_model, unchanged, whose fit prints what it was given if it fails."""
    from soccer_stats import dashboard

    def make():
        model = dashboard.match_model(code, xg_weight=weight)
        fit = model.fit

        def logged(train, as_of=None):
            try:
                return fit(train, as_of=as_of)
            except RuntimeError as exc:
                info = {"league": code, "form": form, "weight": weight, "as_of": str(as_of),
                        "error": str(exc), **describe_train(train)}  # fmt: skip
                print(f"XG_FALLBACK_FIT_FAILED {json.dumps(info, default=str)}", flush=True)
                raise

        model.fit = logged
        return model

    return make


def _model_cols(preds: pd.DataFrame, form: str) -> pd.DataFrame:
    keep = ["season", "date", "home", "away", "home_goals", "away_goals"]
    out = preds[keep + [f"p_{s}" for s in SIDES]].copy()
    out["season"] = out["season"].astype(str)
    return out.rename(columns={f"p_{s}": f"p_{form}_{s}" for s in SIDES})


def run_league(code: str, out_dir: Path, holdout: Holdout | None = None) -> dict:
    from soccer_stats import backtest
    from soccer_stats import lean_backtest as lb
    from soccer_stats import match_calibration as mc
    from soccer_stats.data import load_matches, pinnacle_prices
    from soccer_stats.xg import with_xg

    holdout = holdout or Holdout(LOCK)
    years = range(FIRST_YEAR, LAST_YEAR + 1)
    matches = load_matches([code], years)
    matches = matches[pd.to_datetime(matches["date"]) < LOCK].copy()
    holdout.check(matches["date"])
    matches["season"] = matches["season"].astype(str)
    shots = fd_shots(code, years)
    base = matches.drop(columns=["home_xg", "away_xg"], errors="ignore").merge(
        shots, on=["season", "home", "away"], how="left")  # fmt: skip
    with_a, err = with_xg(matches)
    has_xg = bool(with_a["home_xg"].notna().any())
    coef = fit_proxy(base[pd.to_datetime(base["date"]) < PROXY_TO])
    frames = {
        "a": (with_a, WEIGHT if has_xg else 0.0),
        "b": (base.drop(columns=["home_xg", "away_xg"], errors="ignore"), 0.0),
        "c": (proxy(base, coef), WEIGHT),
        "c_sot": (proxy(base, coef, sot_only=True), WEIGHT),
    }
    pins = pinnacle_prices(code, range(SCORED_FROM.year, LAST_YEAR + 1))
    pins = pins[pd.to_datetime(pins["date"]) < LOCK]
    holdout.check(pins["date"])
    included = {c["season"] for c in lb.coverage(pins) if c["included"]}
    model_parts, blend_parts, bet_parts = [], [], []
    cache: dict = {}
    for form, (m, w) in frames.items():
        if form == "a" and not has_xg:
            preds = None  # (a) is the goals-only model: reuse (b)'s
        else:
            factory = checked_factory(code, form, w)
            preds = backtest.walk_forward(m, start=str(SCORED_FROM.date()), model_factory=factory)
            holdout.check(preds["date"])
            cache[form] = preds
    cache.setdefault("a", cache["b"])
    for form in FORMS:
        preds = cache[form]
        model_parts.append(_model_cols(preds, form).set_index(["season", "home", "away"]))
        merged = lb.with_pinnacle(preds, pins)
        train = mc.training_rows(merged, "h2h")
        scored, _ = lb.walk_forward(train, lb.target_rows(merged))
        if len(scored):
            scored = scored[scored["season"].astype(str).isin(included)]
        sb = scored[["season", "home", "away", *[f"pb_{s}" for s in SIDES]]].copy()
        sb["season"] = sb["season"].astype(str)
        blend_parts.append(sb.rename(columns={f"pb_{s}": f"pb_{form}_{s}" for s in SIDES})
                           .set_index(["season", "home", "away"]))  # fmt: skip
        b = lb.bets(scored, code)
        bet_parts.append(b.assign(form=form))
    model = model_parts[0]
    for part in model_parts[1:]:
        model = model.join(part[[c for c in part.columns if c.startswith("p_")]], how="outer")
    blend = pd.concat(blend_parts, axis=1)
    model = model.reset_index().assign(league=code)
    blend = blend.reset_index().assign(league=code)
    bets = pd.concat(bet_parts, ignore_index=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    model.to_csv(out_dir / f"xgfb_{code}_model.csv.gz", index=False)
    blend.to_csv(out_dir / f"xgfb_{code}_blend.csv.gz", index=False)
    bets.to_csv(out_dir / f"xgfb_{code}_bets.csv.gz", index=False)
    shot_cov = {c: float(base.loc[pd.to_datetime(base["date"]) >= SCORED_FROM, c].notna().mean())
                for c in ("home_hs", "home_hst")}  # fmt: skip
    xg_cov = (
        with_a.assign(season=with_a["season"].astype(str))
        .groupby("season")["home_xg"].apply(lambda s: round(float(s.notna().mean()), 3))
        .to_dict()
    )  # fmt: skip
    info = {"league": code, "has_xg": has_xg, "xg_error": err, "proxy": coef,
            "shots_coverage_scored": shot_cov, "xg_coverage": xg_cov,
            "seasons_blend": sorted(included), "model_rows": int(len(model)),
            "blend_rows": int(len(blend)), "bets": {f: int((bets["form"] == f).sum())
                                                    for f in FORMS}}  # fmt: skip
    (out_dir / f"xgfb_{code}.json").write_text(json.dumps(info, default=str))
    return info


# --- pooling ---------------------------------------------------------------------------


def outcome(df: pd.DataFrame) -> np.ndarray:
    hg, ag = df["home_goals"].astype(int), df["away_goals"].astype(int)
    return np.where(hg > ag, 0, np.where(hg == ag, 1, 2))


def log_loss(df: pd.DataFrame, prefix: str, form: str, y: np.ndarray) -> np.ndarray:
    p = df[[f"{prefix}_{form}_{s}" for s in SIDES]].to_numpy(float)
    return -np.log(np.clip(p[np.arange(len(y)), y], 1e-15, None))


def _draws(n: int, n_boot: int, seed: int = SEED) -> np.ndarray:
    return np.random.default_rng(seed).integers(0, n, (n_boot, n))


def diff_block(ll: dict, a: str, b: str, d: np.ndarray) -> dict:
    v = ll[a] - ll[b]
    means = v[d].mean(axis=1)
    return {"diff": float(v.mean()), "ci95": [float(np.quantile(means, 0.025)),
                                             float(np.quantile(means, 0.975))]}  # fmt: skip


def ll_report(df: pd.DataFrame, prefix: str, forms, n_boot: int = N_BOOT) -> dict:
    """Mean log loss per form and the paired differences, on rows every form scores."""
    cols = [f"{prefix}_{f}_{s}" for f in forms for s in SIDES]
    df = df.dropna(subset=cols)
    if len(df) < 2:
        return {"matches": int(len(df))}
    y = outcome(df)
    ll = {f: log_loss(df, prefix, f, y) for f in forms}
    d = _draws(len(df), n_boot)
    out = {"matches": int(len(df)), "log_loss": {f: float(v.mean()) for f, v in ll.items()}}
    pairs = [("b", "a"), ("c", "a"), ("c", "b"), ("c_sot", "b"), ("c_sot", "a")]
    out["diffs"] = {f"{x}-{z}": diff_block(ll, x, z, d) for x, z in pairs
                    if x in ll and z in ll}  # fmt: skip
    if "a" in ll and "c" in ll:
        gain_xg, gain_c = (ll["b"] - ll["a"])[d].mean(axis=1), (ll["b"] - ll["c"])[d].mean(axis=1)
        out["recovery"] = {
            "r": float((ll["b"] - ll["c"]).mean() / (ll["b"] - ll["a"]).mean())
            if (ll["b"] - ll["a"]).mean() != 0 else None,
            "ci95": [float(np.quantile(gain_c / gain_xg, 0.025)),
                     float(np.quantile(gain_c / gain_xg, 0.975))],
            "xg_gain_range_above_0": bool(np.quantile(gain_xg, 0.025) > 0),
        }  # fmt: skip
    return out


def bet_metrics(b: pd.DataFrame, n_boot: int = N_BOOT) -> dict:
    if b.empty:
        return {"bets": 0}
    clv = pd.to_numeric(b["clv"], errors="coerce").dropna().to_numpy(float)
    prof = b["profit"].to_numpy(float)

    def ci(v):
        if len(v) < 2:
            return None
        means = v[_draws(len(v), n_boot)].mean(axis=1)
        return [float(np.quantile(means, 0.025)), float(np.quantile(means, 0.975))]

    return {"bets": int(len(b)), "won": int(b["won"].astype(bool).sum()),
            "avg_odds": float(b["odds"].mean()), "clv": float(clv.mean()) if len(clv) else None,
            "clv_ci95": ci(clv), "roi": float(prof.mean()), "roi_ci95": ci(prof)}  # fmt: skip


def same_picks(bets: pd.DataFrame, form: str) -> dict:
    """Of (a)'s picks, the share where `form` picked the same side on the same match."""
    k = ["league", "season", "home", "away"]
    a = bets[bets["form"] == "a"].set_index(k)["market"]
    f = bets[bets["form"] == form].set_index(k)["market"]
    both = a.index.intersection(f.index)
    return {"a_bets": int(len(a)), "form_bets": int(len(f)), "same_match": int(len(both)),
            "same_side": int((a.loc[both] == f.loc[both]).sum()) if len(both) else 0}  # fmt: skip


def lean_report(bets: pd.DataFrame, n_boot: int = N_BOOT) -> dict:
    return {f: {**bet_metrics(bets[bets["form"] == f], n_boot),
                **({"vs_a": same_picks(bets, f)} if f != "a" else {})}
            for f in FORMS}  # fmt: skip


def proposal(model_xg: dict, blend_xg: dict, blend_go: dict) -> dict:
    cb = (blend_xg.get("diffs") or {}).get("c-b", {}).get("ci95")
    rec = model_xg.get("recovery") or {}
    r_ok = bool(rec.get("xg_gain_range_above_0") and rec.get("r") is not None
                and rec["r"] >= 0.5)  # fmt: skip
    xg_ok = bool(cb and cb[1] < 0) and r_ok
    go = (blend_go.get("diffs") or {}).get("c-b", {}).get("ci95")
    return {
        "understat_leagues": {"blend_c_minus_b_below_0": bool(cb and cb[1] < 0),
                              "recovery_at_least_half": r_ok, "propose": xg_ok},
        "goals_only_leagues": {"blend_c_minus_b_below_0": bool(go and go[1] < 0),
                               "propose": bool(go and go[1] < 0)},
    }  # fmt: skip


def pool(in_dir: Path, n_boot: int = N_BOOT) -> dict:
    rd = Path(in_dir)
    model = pd.concat([pd.read_csv(f) for f in sorted(rd.rglob("xgfb_*_model.csv.gz"))])
    blend = pd.concat([pd.read_csv(f) for f in sorted(rd.rglob("xgfb_*_blend.csv.gz"))])
    bets = pd.concat([pd.read_csv(f) for f in sorted(rd.rglob("xgfb_*_bets.csv.gz"))])
    infos = {json.loads(f.read_text())["league"]: json.loads(f.read_text())
             for f in sorted(rd.rglob("xgfb_*.json"))}  # fmt: skip
    model["season"] = model["season"].astype(str)
    blend["season"] = blend["season"].astype(str)
    model = model[pd.to_datetime(model["date"]) >= SCORED_FROM]
    blend = blend.merge(model[[*KEYS, "home_goals", "away_goals"]], on=KEYS, how="left")
    xg, go = model[model["league"].isin(XG_LEAGUES)], model[~model["league"].isin(XG_LEAGUES)]
    bxg, bgo = blend[blend["league"].isin(XG_LEAGUES)], blend[~blend["league"].isin(XG_LEAGUES)]
    res = {
        "kind": "xg_fallback", "development": True, "locked_from": str(LOCK.date()),
        "proxy": {k: v.get("proxy") for k, v in infos.items()}, "leagues_info": infos,
        "model": {"understat_leagues": ll_report(xg, "p", FORMS, n_boot),
                  "goals_only_leagues": ll_report(go, "p", ("b", "c", "c_sot"), n_boot),
                  "by_league": {lg: ll_report(g, "p", FORMS if lg in XG_LEAGUES
                                              else ("b", "c", "c_sot"), n_boot)
                                for lg, g in model.groupby("league")},
                  "by_season_understat": {s: ll_report(g, "p", FORMS, n_boot)
                                          for s, g in xg.groupby("season")}},
        "blend": {"understat_leagues": ll_report(bxg, "pb", FORMS, n_boot),
                  "goals_only_leagues": ll_report(bgo, "pb", ("b", "c", "c_sot"), n_boot),
                  "by_league": {lg: ll_report(g, "pb", FORMS if lg in XG_LEAGUES
                                              else ("b", "c", "c_sot"), n_boot)
                                for lg, g in blend.groupby("league")}},
        "lean": {"understat_leagues": lean_report(bets[bets["league"].isin(XG_LEAGUES)], n_boot),
                 "goals_only_leagues": lean_report(bets[~bets["league"].isin(XG_LEAGUES)], n_boot),
                 "by_league": {lg: {f: bet_metrics(g[g["form"] == f], n_boot) for f in FORMS}
                               for lg, g in bets.groupby("league")}},
    }  # fmt: skip
    res["proposal"] = proposal(res["model"]["understat_leagues"],
                               res["blend"]["understat_leagues"],
                               res["blend"]["goals_only_leagues"])  # fmt: skip
    run = os.environ.get("GITHUB_RUN_ID")
    res["runs"] = {"xg_fallback": run} if run else {}
    return res


def _d(r: dict, k: str) -> str:
    x = (r.get("diffs") or {}).get(k)
    if not x:
        return f"{k} –"
    return f"{k} {x['diff']:+.5f} ({x['ci95'][0]:+.5f} to {x['ci95'][1]:+.5f})"


def summary_lines(res: dict) -> list[str]:
    out = []
    for part in ("model", "blend"):
        for grp in ("understat_leagues", "goals_only_leagues"):
            r = res[part][grp]
            ll = ", ".join(f"{k} {v:.5f}" for k, v in (r.get("log_loss") or {}).items())
            diffs = [k for k in ("b-a", "c-a", "c-b", "c_sot-b") if k in (r.get("diffs") or {})]
            out.append(f"{part} {grp}: {r.get('matches')} matches; {ll}; "
                       + "; ".join(_d(r, k) for k in diffs))  # fmt: skip
            if r.get("recovery"):
                out.append(f"  recovery {r['recovery']}")
    for grp in ("understat_leagues", "goals_only_leagues"):
        for f, m in res["lean"][grp].items():
            out.append(f"lean {grp} {f}: {m.get('bets')} bets, CLV {m.get('clv')} "
                       f"{m.get('clv_ci95')}, ROI {m.get('roi')} {m.get('roi_ci95')}"
                       + (f", vs a {m['vs_a']}" if "vs_a" in m else ""))  # fmt: skip
    for lg, r in res["model"]["by_league"].items():
        out.append(f"  model {lg}: {r.get('matches')} matches; "
                   + "; ".join(_d(r, k) for k in ("b-a", "c-a", "c-b")
                               if k in (r.get("diffs") or {})))  # fmt: skip
    out.append(f"proxy coefficients: {res['proxy']}")
    out.append(f"proposal: {res['proposal']}")
    return out


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--league", default="", help="one league into --out")
    p.add_argument("--pool", default="", help="pool the files under this folder")
    p.add_argument("--out", default="out", help="output folder")
    p.add_argument("--boot", type=int, default=N_BOOT, help="bootstrap draws")
    a = p.parse_args(argv)
    if a.league:
        print(f"XG_FALLBACK_LEAGUE {json.dumps(run_league(a.league, Path(a.out)), default=str)}")
        return
    res = pool(Path(a.pool or a.out), a.boot)
    print("\n".join(summary_lines(res)))
    print(f"XG_FALLBACK_JSON {json.dumps(res, default=str, separators=(',', ':'))}")
    Path(a.out).mkdir(parents=True, exist_ok=True)
    (Path(a.out) / "xg_fallback.json").write_text(json.dumps(res, default=str))


if __name__ == "__main__":
    main()
