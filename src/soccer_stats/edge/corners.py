"""Corners bake-off, round 11 (docs/totals.md, "Corners bake-off"; pre-registered).

Five candidates give each match's home, away and total corner distributions:
(a) league averages (baseline), (b) team for/against averages shrunk toward the league,
(c) Dixon-Coles style corner ratings (the lab's HierPoisson on corner counts), (d) (c)
plus match context in an NB2 regression per side, (e) a direct total NB2 on (d)'s mean.
All are refitted every 28 days on the 730 days before the block, need 1,000 earlier
matches, and use earlier matches only. Development scores 2017/18-2023/24 after
dropping every match from 1 July 2024 (before the match model is fitted); the 2024/25
holdout is opened once per league for the finalists. 2025/26 is never loaded.
"""

from __future__ import annotations

import functools

import numpy as np
import pandas as pd

from soccer_stats.edge import totals
from soccer_stats.edge.totals import KMAX, convolve, corner_features, over_from_pmf, score_line
from soccer_stats.lab import metrics
from soccer_stats.lab.harness import Holdout
from soccer_stats.lab.harness import walk_forward as harness_walk_forward
from soccer_stats.lab.models import HierPoisson
from soccer_stats.models.player_counts import NBRegression, nb_pmf

LEAGUES = totals.LEAGUES
FIRST_DATA = 2014
LAST_DATA = 2024  # the 2024/25 season; 2025/26 is never loaded
DEV_START = pd.Timestamp("2017-07-01")
HOLDOUT_START = pd.Timestamp("2024-07-01")
HOLDOUT_END = pd.Timestamp("2025-07-01")
TOTAL_LINES = (8.5, 9.5, 10.5, 11.5)
TEAM_LINES = (3.5, 4.5, 5.5)
TRAIN_DAYS = 730
MIN_TRAIN = 1000
SHRINK = 10.0  # (b): pseudo-matches at the league mean
HALF_LIFE = 180.0  # (c)
PRIOR_SD = 0.2  # (c)
L2 = 1.0  # (d)
SLOPE_BAND = (0.80, 1.25)
CANDIDATES = ("a", "b", "c", "d", "e")
NAMES = {
    "a": "league average (baseline)",
    "b": "team averages, shrunk",
    "c": "corner ratings",
    "d": "ratings + match context",
    "e": "direct total",
}
GROUPS = {"total": ("b", "c", "d", "e"), "team": ("b", "c", "d")}
DEV_TESTS = sum(len(v) for v in GROUPS.values()) * len(LEAGUES)  # 7 x 6 = 42
HOLDOUT_TESTS = len(GROUPS) * len(LEAGUES)  # 12
CONTEXT = ["log_sup", "log_tot", "h_sf", "h_sa", "a_sf", "a_sa"]
N = KMAX + 1  # columns per distribution


def mom_alpha(y, m) -> float:
    """Moment-matched NB2 dispersion: sum((y - m)^2 - m) / sum(m^2), floored at 1e-4."""
    y, m = np.asarray(y, float), np.asarray(m, float)
    return float(max(((y - m) ** 2 - m).sum() / (m**2).sum(), 1e-4))


def _window(train: pd.DataFrame) -> pd.DataFrame:
    t = pd.to_datetime(train["time"])
    return train[t > t.max() - pd.Timedelta(days=TRAIN_DAYS)]


class Ratings:
    """(c)'s fit: HierPoisson on the corner counts, one shared home edge, no team terms
    beyond attack/defence. Unknown teams get 0 (the league mean)."""

    def fit(self, t: pd.DataFrame):
        m = HierPoisson(half_life=HALF_LIFE, prior_sd=PRIOR_SD, home_sd=0.0, lookback=TRAIN_DAYS)
        d = pd.DataFrame(
            {
                "time": t["time"],
                "home": t["home"],
                "away": t["away"],
                "home_goals": t["home_corners"],
                "away_goals": t["away_corners"],
            }
        )
        m.fit(d)
        self.m_ = m
        return self

    def rates(self, df: pd.DataFrame):
        m = self.m_
        h, a = df["home"].to_numpy(), df["away"].to_numpy()
        get = lambda d, ts: np.array([d.get(x, 0.0) for x in ts])  # noqa: E731
        lh = np.exp(m.mu_ + m.h_ + get(m.att_, h) + get(m.def_, a))
        la = np.exp(m.mu_ + get(m.att_, a) + get(m.def_, h))
        return lh, la


class Bakeoff:
    """All five candidates fitted on the same window. predict() returns, per candidate in
    CANDIDATES order, [home pmf | away pmf | total pmf] (3 * N columns each); rows a
    candidate can't predict (missing context features for d/e) are NaN."""

    def fit(self, train: pd.DataFrame):
        t = _window(train).reset_index(drop=True)
        hc, ac = t["home_corners"].to_numpy(float), t["away_corners"].to_numpy(float)
        tot = hc + ac
        # (a) league averages
        self.a_ = {
            "mh": hc.mean(),
            "ma": ac.mean(),
            "mt": tot.mean(),
            "ah": mom_alpha(hc, np.full(len(t), hc.mean())),
            "aa": mom_alpha(ac, np.full(len(t), ac.mean())),
            "at": mom_alpha(tot, np.full(len(t), tot.mean())),
        }
        # (b) team averages as ratios to the league's home/away means, shrunk toward 1
        mh, ma = self.a_["mh"], self.a_["ma"]
        long = pd.concat(
            [
                pd.DataFrame({"team": t["home"], "f": hc / mh, "a": ac / ma}),
                pd.DataFrame({"team": t["away"], "f": ac / ma, "a": hc / mh}),
            ]
        )
        g = long.groupby("team")
        n = g.size()
        self.b_for_ = ((g["f"].sum() + SHRINK) / (n + SHRINK)).to_dict()
        self.b_against_ = ((g["a"].sum() + SHRINK) / (n + SHRINK)).to_dict()
        bh, ba = self._b_rates(t)
        self.b_alpha_ = (mom_alpha(hc, bh), mom_alpha(ac, ba))
        # (c) corner ratings
        self.r_ = Ratings().fit(t)
        ch, ca = self.r_.rates(t)
        self.c_alpha_ = (mom_alpha(hc, ch), mom_alpha(ac, ca))
        # (d) NB2 regression per side on log rating + context (rows with every feature)
        x = self._d_frame(t, ch, ca)
        ok = np.isfinite(x[["log_c_home", "log_c_away", *CONTEXT]]).all(axis=1).to_numpy()
        one = np.ones(ok.sum())
        self.dh_ = NBRegression(["log_c_home", *CONTEXT], L2).fit(x[ok], hc[ok], one)
        self.da_ = NBRegression(["log_c_away", *CONTEXT], L2).fit(x[ok], ac[ok], one)
        # (e) total dispersion moment-matched on the totals at (d)'s mean
        mt = self.dh_.rate(x[ok]) + self.da_.rate(x[ok])
        self.e_alpha_ = mom_alpha(tot[ok], mt)
        return self

    def _b_rates(self, df):
        f = lambda d, ts: np.array([d.get(x, 1.0) for x in ts])  # noqa: E731
        h, a = df["home"].to_numpy(), df["away"].to_numpy()
        lh = self.a_["mh"] * f(self.b_for_, h) * f(self.b_against_, a)
        la = self.a_["ma"] * f(self.b_for_, a) * f(self.b_against_, h)
        return lh, la

    @staticmethod
    def _d_frame(df, ch, ca):
        x = df[CONTEXT].copy() if set(CONTEXT) <= set(df) else pd.DataFrame(index=df.index)
        x["log_c_home"] = np.log(ch)
        x["log_c_away"] = np.log(ca)
        return x.reset_index(drop=True)

    def predict(self, test: pd.DataFrame) -> np.ndarray:
        k = len(test)
        a = self.a_
        out = []

        def sides(lh, la, alphas):
            ph, pa = nb_pmf(lh, alphas[0], KMAX), nb_pmf(la, alphas[1], KMAX)
            return np.hstack([ph, pa, convolve(ph, pa)])

        out.append(
            np.hstack(
                [
                    nb_pmf(np.full(k, a["mh"]), a["ah"], KMAX),
                    nb_pmf(np.full(k, a["ma"]), a["aa"], KMAX),
                    nb_pmf(np.full(k, a["mt"]), a["at"], KMAX),
                ]
            )
        )
        out.append(sides(*self._b_rates(test), self.b_alpha_))
        ch, ca = self.r_.rates(test)
        out.append(sides(ch, ca, self.c_alpha_))
        x = self._d_frame(test, ch, ca)
        ok = np.isfinite(x[["log_c_home", "log_c_away", *CONTEXT]]).all(axis=1).to_numpy()
        d = np.full((k, 3 * N), np.nan)
        e = np.full((k, 3 * N), np.nan)
        if ok.any():
            dh, da = self.dh_.rate(x[ok]), self.da_.rate(x[ok])
            d[ok] = sides(dh, da, (self.dh_.alpha_, self.da_.alpha_))
            e[ok] = d[ok]
            e[ok, 2 * N :] = nb_pmf(dh + da, self.e_alpha_, KMAX)
        out += [d, e]
        return np.hstack(out)


def frame(df: pd.DataFrame) -> pd.DataFrame:
    """The model frame: teams, counts, time, and the context features (earlier matches
    only, from totals.corner_features)."""
    f = corner_features(df)
    out = df[["date", "home", "away", "home_corners", "away_corners", "season_start", "match"]]
    out = pd.concat([out, f[CONTEXT]], axis=1)
    out = out.dropna(subset=["home_corners", "away_corners"]).copy()
    out["time"] = out["date"]
    return out


def predictions(data: pd.DataFrame, start, end, holdout: Holdout) -> dict[str, np.ndarray]:
    """Walk-forward distributions per candidate on rows start <= time < end, restricted
    to the rows every candidate predicts. Returns {"index", cand: (home, away, total)}."""
    p = harness_walk_forward(
        data,
        Bakeoff,
        {},
        start,
        end,
        holdout=holdout,
        refit="28D",
        min_train=MIN_TRAIN,
    )
    if p.empty:
        return {"index": pd.Index([])}
    arr = p.to_numpy()
    keep = np.isfinite(arr).all(axis=1)
    arr = arr[keep]
    out: dict = {"index": p.index[keep]}
    for i, c in enumerate(CANDIDATES):
        block = arr[:, i * 3 * N : (i + 1) * 3 * N]
        out[c] = (block[:, :N], block[:, N : 2 * N], block[:, 2 * N :])
    return out


# ---------- scoring ----------


def line_outcomes(data: pd.DataFrame, idx) -> dict[str, tuple[str, int, float, np.ndarray]]:
    """{line name: (group, part, line, y)}; part 0 home, 1 away, 2 total."""
    d = data.loc[idx]
    hc, ac = d["home_corners"].to_numpy(int), d["away_corners"].to_numpy(int)
    out = {}
    for line in TOTAL_LINES:
        out[f"total_{line}"] = ("total", 2, line, (hc + ac > line).astype(int))
    for side, part, c in (("home", 0, hc), ("away", 1, ac)):
        for line in TEAM_LINES:
            out[f"{side}_{line}"] = ("team", part, line, (c > line).astype(int))
    return out


def _brier(p, y) -> float:
    return float(((np.asarray(p) - np.asarray(y)) ** 2).mean())


def score(data: pd.DataFrame, pred: dict, candidates, level: float) -> dict:
    """Per candidate: each line (score_line vs (a), Brier), and per group the mean
    per-match log-loss gain over (a) with its range at `level` and the pass rule."""
    idx = pred["index"]
    groups = data.loc[idx, "match"].to_numpy()
    lines = line_outcomes(data, idx)
    count = data.loc[idx, ["home_corners", "away_corners"]].to_numpy(int)
    res: dict = {
        "rows": int(len(idx)),
        "seasons": sorted(int(s) for s in data.loc[idx, "season_start"].unique()),
        "mean_total": float(count.sum(axis=1).mean()) if len(idx) else None,
        "candidates": {},
    }
    if not len(idx):
        return res

    def p_over(c, part, line):
        return over_from_pmf(pred[c][part], line)

    def count_ll(c):
        k = np.clip(count, 0, KMAX)
        r = np.arange(len(k))
        tot = np.clip(count.sum(axis=1), 0, KMAX)
        return {
            "home": float(-np.log(pred[c][0][r, k[:, 0]]).mean()),
            "away": float(-np.log(pred[c][1][r, k[:, 1]]).mean()),
            "total": float(-np.log(pred[c][2][r, tot]).mean()),
        }

    for c in ("a", *candidates):
        r: dict = {"name": NAMES[c], "count_log_loss": count_ll(c), "lines": {}, "groups": {}}
        gains: dict[str, list] = {"total": [], "team": []}
        for name, (grp, part, line, y) in lines.items():
            if grp == "total" or c != "e":
                pm, pb = p_over(c, part, line), p_over("a", part, line)
                if c == "a":
                    s = {
                        "rows": int(len(y)),
                        "over_rate": float(y.mean()),
                        "predicted": float(pm.mean()),
                    }
                else:
                    s = score_line(pm, pb, y, groups, level)
                    two = lambda p: np.column_stack([p, 1 - p])  # noqa: E731
                    gains[grp].append(
                        metrics.log_loss_rows(two(pb), 1 - y)
                        - metrics.log_loss_rows(two(pm), 1 - y)
                    )
                s["brier"] = _brier(pm, y)
                s["baseline_brier"] = _brier(pb, y)
                r["lines"][name] = s
        if c != "a":
            for grp, members in GROUPS.items():
                if c not in members or not gains[grp]:
                    continue
                g = np.mean(gains[grp], axis=0)
                slopes = {k: v["slope"] for k, v in r["lines"].items() if lines[k][0] == grp}
                rng = metrics.boot_range(g, groups, n_boot=totals.N_BOOT, level=level)
                in_band = all(SLOPE_BAND[0] <= s <= SLOPE_BAND[1] for s in slopes.values())
                r["groups"][grp] = {
                    "gain": float(g.mean()),
                    "gain_range": rng,
                    "slopes": slopes,
                    "slopes_in_band": in_band,
                    "pass": bool(rng is not None and rng[0] > 0 and in_band),
                }
        res["candidates"][c] = r
    return res


def finalists(res: dict) -> dict[str, str]:
    """Per group, the candidate with the best development gain (passing or not)."""
    out = {}
    for grp in GROUPS:
        best = max(
            (
                (r["groups"][grp]["gain"], c)
                for c, r in res["candidates"].items()
                if grp in r.get("groups", {})
            ),
            default=None,
        )
        if best:
            out[grp] = best[1]
    return out


def parse_finalists(text: str) -> dict[str, str]:
    """'total=e,team=d' -> {'total': 'e', 'team': 'd'} (checked against GROUPS)."""
    out = {}
    for part in filter(None, (s.strip() for s in (text or "").split(","))):
        grp, _, c = part.partition("=")
        grp, c = grp.strip(), c.strip()
        if grp not in GROUPS or c not in GROUPS[grp]:
            raise SystemExit(f"Bad finalist {part!r}: use total=<b|c|d|e>,team=<b|c|d>")
        out[grp] = c
    if set(out) != set(GROUPS):
        raise SystemExit("Name one finalist per group: total=<x>,team=<y>")
    return out


def run(df: pd.DataFrame, level: float, open_reason: str | None = None, final=None) -> dict:
    """Development (open_reason None) or the holdout for `final` ({group: candidate})."""
    holdout = Holdout(HOLDOUT_START)
    if open_reason is None:
        df = df[df["date"] < HOLDOUT_START]  # again, in case the caller didn't
        data = frame(df.sort_values("date").reset_index(drop=True))
        pred = predictions(data, DEV_START, HOLDOUT_START, holdout)
        res = score(data, pred, ("b", "c", "d", "e"), level)
        res["finalists"] = finalists(res)
    else:
        df = df[df["date"] < HOLDOUT_END]
        data = frame(df.sort_values("date").reset_index(drop=True))
        holdout.unlock(open_reason)
        pred = predictions(data, HOLDOUT_START, HOLDOUT_END, holdout)
        cands = tuple(sorted(set(final.values())))
        res = score(data, pred, cands, level)
        for c, r in res["candidates"].items():  # only the finalist's own group counts
            r["groups"] = {g: v for g, v in r["groups"].items() if final.get(g) == c}
        res["finalists"] = final
        res["holdout_log"] = holdout.events
    res["level"] = level
    return res


# ---------- loading (network: GitHub Actions) ----------


def load(league: str, cut: pd.Timestamp, last: int = LAST_DATA) -> pd.DataFrame:
    """football-data corners/shots/results from 2014/15 to the season starting in `last`
    (default 2024/25) with the match model's expected goals; every match from `cut` on is
    dropped before the match model is fitted."""
    from soccer_stats import backtest
    from soccer_stats.data import download, load_matches, season_code
    from soccer_stats.edge import books
    from soccer_stats.models import DixonColes
    from soccer_stats.xg import LEAGUES as XG_LEAGUES
    from soccer_stats.xg import with_xg

    years = range(FIRST_DATA, last + 1)
    frames = []
    for y in years:
        raw = pd.read_csv(download(league, y), encoding="latin-1", on_bad_lines="skip")
        px = books.book_prices(raw, season=season_code(y))
        played = raw.dropna(subset=["HomeTeam", "AwayTeam", "FTHG", "FTAG"]).reset_index(drop=True)
        for col, src in (("home_corners", "HC"), ("away_corners", "AC")):
            px[col] = pd.to_numeric(played[src], errors="coerce") if src in played else np.nan
        frames.append(px)
    px = pd.concat(frames, ignore_index=True)
    px = px[px["date"] < cut]
    matches = load_matches([league], years)
    matches = matches[matches["date"] < cut].reset_index(drop=True)
    xg = league in XG_LEAGUES
    if xg:
        matches, err = with_xg(matches)
        if err:
            raise SystemExit(err)
    dc = backtest.walk_forward(
        matches,
        start=totals.DC_START,
        model_factory=functools.partial(DixonColes, xg_weight=0.7 if xg else 0.0),
    )
    cols = [
        "date",
        "home",
        "away",
        "season",
        "home_shots",
        "away_shots",
        "home_corners",
        "away_corners",
    ]
    df = px[cols].merge(
        dc[["date", "home", "away", "exp_home", "exp_away"]],
        on=["date", "home", "away"],
        how="left",
    )
    df["season_start"] = 2000 + df["season"].astype(str).str[:2].astype(int)
    df["match"] = df["date"].dt.strftime("%Y-%m-%d") + " " + df["home"] + " v " + df["away"]
    print(
        f"{league}: {len(px)} football-data matches before {cut.date()}, "
        f"{int(df['exp_home'].notna().sum())} with match-model expected goals, "
        f"{int(df[['home_corners', 'away_corners']].notna().all(axis=1).sum())} with corners"
    )
    return df


# ---------- report ----------


def _rng(v, f="{:+.4f}"):
    return f"{f.format(v[0])}..{f.format(v[1])}" if v else "–"


def report(league: str, res: dict) -> str:
    out = [
        f"== {league}: corners bake-off, {'holdout' if 'holdout_log' in res else 'development'}"
        f" (gain ranges {res['level']:.3%}, slope band {SLOPE_BAND[0]}-{SLOPE_BAND[1]}) ==",
        f"Scored matches: {res['rows']}, seasons {res.get('seasons')}, mean total "
        + (f"{res['mean_total']:.2f}" if res.get("mean_total") is not None else "–"),
    ]
    for line in res.get("holdout_log", []):
        out.append(line)
    for c, r in res.get("candidates", {}).items():
        ll = r["count_log_loss"]
        out.append(
            f"({c}) {r['name']}: count log loss home {ll['home']:.4f}, away {ll['away']:.4f},"
            f" total {ll['total']:.4f}"
        )
        for name, s in r["lines"].items():
            extra = (
                f", gain {s['gain']:+.4f} ({_rng(s['gain_range'])}), slope range "
                f"{_rng(s['slope_range'], '{:.2f}')}, obs-pred {s['obs_minus_pred']:+.3f}"
                if "gain" in s
                else ""
            )
            slope = f" slope {s['slope']:.2f}," if "slope" in s else ""
            out.append(
                f"    {name:>10}: over {s['over_rate']:.3f} vs predicted {s['predicted']:.3f},"
                f"{slope} Brier {s['brier']:.4f} (baseline {s['baseline_brier']:.4f}){extra}"
            )
        for grp, gr in r["groups"].items():
            out.append(
                f"  -> {grp} group: gain {gr['gain']:+.4f} ({_rng(gr['gain_range'])}), slopes "
                f"in band {gr['slopes_in_band']}: PASS {gr['pass']}"
            )
    out.append(f"Finalists: {res.get('finalists')}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> None:
    import argparse
    import json
    from pathlib import Path

    from soccer_stats.edge.stats import bonferroni_level
    from soccer_stats.lab.run import _jsonable

    ap = argparse.ArgumentParser(prog="python -m soccer_stats.edge.corners")
    ap.add_argument("--league", default="E0", choices=LEAGUES)
    ap.add_argument("--open-holdout", action="store_true")
    ap.add_argument("--reason", default="")
    ap.add_argument("--finalists", default="")
    ap.add_argument("--json")
    args = ap.parse_args(argv)
    if args.open_holdout:
        if not args.reason.strip():
            raise SystemExit("Opening the holdout needs --reason")
        final = parse_finalists(args.finalists)
        df = load(args.league, HOLDOUT_END)
        res = run(df, bonferroni_level(HOLDOUT_TESTS), args.reason, final)
    else:
        df = load(args.league, HOLDOUT_START)
        res = run(df, bonferroni_level(DEV_TESTS))
    print(report(args.league, res))
    text = json.dumps(_jsonable({"league": args.league, **res}), default=str)
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(text)
    print("CORNERS_JSON " + text)


if __name__ == "__main__":
    main()
