"""Corners bake-off 2, round 13 (docs/totals.md, "Corners bake-off 2"; pre-registered).

Each team's corners (home and away, over 3.5 / 4.5 / 5.5) in six leagues. Candidates,
all on the round-11 walk-forward (refit every 28 days on the 730 days before the block,
1,000 earlier matches, earlier kickoffs only):

* (a) the league average NB2 per side: the baseline;
* (b) round 11's shrunk team averages (10 pseudo-matches): a reference, outside the family;
* (f) team averages with empirical-Bayes shrinkage estimated on each training window;
* (g) (f) plus Pinnacle's early 1X2 (log home/away ratio, the favourite's chance);
* (h) (f) plus 10-match style form: shots, shots on target, fouls and cards, for/against.

Development scores 2017/18-2023/24 after dropping every match from 1 July 2024. The test
is 2026/27 kickoffs from 1 July 2026 to 31 January 2027, locked until a run is given a
reason (opened once, not before 3 February 2027), for one finalist per league.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from soccer_stats.edge import corner_recal, corners
from soccer_stats.edge.corners import L2, LEAGUES, MIN_TRAIN, SHRINK, mom_alpha
from soccer_stats.edge.totals import FORM, KMAX, LEAGUE_DAYS, MIN_FORM, trailing_mean
from soccer_stats.lab.harness import Holdout
from soccer_stats.lab.harness import walk_forward as harness_walk_forward
from soccer_stats.models.player_counts import NBRegression, nb_pmf

DEV_START = pd.Timestamp("2017-07-01")
DEV_END = pd.Timestamp("2024-07-01")  # everything from here is dropped in development
TEST_START = pd.Timestamp("2026-07-01")
TEST_END = pd.Timestamp("2027-02-01")  # kickoffs before 1 February 2027
TEST_SEASON = 2026
EARLIEST_OPEN = pd.Timestamp("2027-02-03", tz="UTC")
MIN_JUDGED = 120  # fewer scored test matches: reported, not judged
MIN_REG = 200  # rows with every feature needed to fit (g) or (h)
CANDS = ("a", "b", "f", "g", "h")  # prediction order
FAMILY = ("f", "g", "h")
DEV_TESTS = len(FAMILY) * len(LEAGUES)  # 18: 99.722%
NAMES = {
    "a": "league average",
    "b": "team averages, 10 pseudo-matches (round 11, reference)",
    "f": "team averages, empirical-Bayes shrinkage",
    "g": "(f) + Pinnacle game state",
    "h": "(f) + style form",
}
N = KMAX + 1
STYLE = {"s": "shots", "t": "sot", "f": "fouls", "k": "cards"}  # code -> column stem
GAME = ["lr", "fav"]
GAME_PRICES = ["pinnacle_early_home", "pinnacle_early_draw", "pinnacle_early_away"]
H_FEATS = [f"h_{c}f" for c in STYLE] + [f"a_{c}a" for c in STYLE]  # home corners
A_FEATS = [f"a_{c}f" for c in STYLE] + [f"h_{c}a" for c in STYLE]  # away corners


# ---------- features (earlier kickoffs only) ----------


def style_features(df: pd.DataFrame) -> pd.DataFrame:
    """h_/a_{s,t,f,k}{f,a}: each side's last-FORM-match means for and against (any venue,
    across seasons, earlier matches only) as log ratios to the league's per-team mean over
    the LEAGUE_DAYS before, as totals.corner_features does for corners and shots."""
    m = df.sort_values("date", kind="stable")
    parts = []
    for side, other in (("home", "away"), ("away", "home")):
        d = {"row": m.index, "date": m["date"], "team": m[side], "side": side[0]}
        for c, stem in STYLE.items():
            d[f"{c}f"] = m[f"{side}_{stem}"]
            d[f"{c}a"] = m[f"{other}_{stem}"]
        parts.append(pd.DataFrame(d))
    long = pd.concat(parts).sort_values(["date", "row"], kind="stable").reset_index(drop=True)
    g = long.groupby("team", sort=False)
    stats = [f"{c}{w}" for c in STYLE for w in ("f", "a")]
    for c in stats:
        form = g[c].transform(lambda s: s.shift(1).rolling(FORM, min_periods=MIN_FORM).mean())
        league = trailing_mean(long["date"], long[c], LEAGUE_DAYS)
        long[f"x_{c}"] = np.log(np.clip(form, 0.1, None) / np.clip(league, 0.1, None))
    out = pd.DataFrame(index=df.index)
    for s in ("h", "a"):
        part = long[long["side"] == s].set_index("row")
        for c in stats:
            out[f"{s}_{c}"] = part[f"x_{c}"]
    return out


def game_features(df: pd.DataFrame) -> pd.DataFrame:
    """Pinnacle's early 1X2 without margin (Shin): log(P(home)/P(away)) and the
    favourite's chance. NaN where football-data has no Pinnacle early prices."""
    from soccer_stats.odds import devig_shin

    cols = GAME_PRICES
    out = pd.DataFrame(np.nan, index=df.index, columns=GAME)
    if not set(cols) <= set(df):
        return out
    px = df[cols].to_numpy(float)
    for i in np.flatnonzero(np.isfinite(px).all(axis=1) & (px > 1).all(axis=1)):
        p = devig_shin(px[i])
        out.iloc[i] = [np.log(p[0] / p[2]), max(p[0], p[2])]
    return out


def frame(df: pd.DataFrame) -> pd.DataFrame:
    """The model frame: teams, corner counts, time, game state and style form."""
    out = df[["date", "home", "away", "home_corners", "away_corners", "season_start", "match"]]
    out = pd.concat([out, game_features(df), style_features(df)], axis=1)
    out = out.dropna(subset=["home_corners", "away_corners"]).copy()
    out["time"] = out["date"]
    return out


# ---------- the candidates ----------


def eb_shrink(team: pd.Series, x: np.ndarray) -> float:
    """Empirical-Bayes pseudo-matches k = within-team variance / between-team variance
    (method of moments: variance of team means minus their average sampling variance,
    floored at 1e-4)."""
    d = pd.DataFrame({"team": team.to_numpy(), "x": np.asarray(x, float)})
    g = d.groupby("team")["x"]
    n, means = g.size(), g.mean()
    resid = d["x"] - g.transform("mean")
    dof = max(int(len(d) - len(n)), 1)
    within = float((resid**2).sum() / dof)
    between = max(float(means.var(ddof=1) - (within / n).mean()), 1e-4)
    return within / between


class Bakeoff2:
    """All candidates on the same window. predict() returns, per candidate in CANDS order,
    [home pmf | away pmf] (2 * N columns each)."""

    def fit(self, train: pd.DataFrame):
        t = corners._window(train).reset_index(drop=True)
        hc, ac = t["home_corners"].to_numpy(float), t["away_corners"].to_numpy(float)
        mh, ma = hc.mean(), ac.mean()
        self.mean_ = (mh, ma)
        self.a_alpha_ = (mom_alpha(hc, np.full(len(t), mh)), mom_alpha(ac, np.full(len(t), ma)))
        teams = pd.concat([t["home"], t["away"]], ignore_index=True)
        f = np.r_[hc / mh, ac / ma]  # corners for, as a ratio to the venue's mean
        a = np.r_[ac / ma, hc / mh]  # corners against
        self.k_ = (eb_shrink(teams, f), eb_shrink(teams, a))
        g = pd.DataFrame({"team": teams, "f": f, "a": a}).groupby("team")
        n = g.size()
        self.tables_ = {}
        for name, (kf, ka) in (("b", (SHRINK, SHRINK)), ("f", self.k_)):
            self.tables_[name] = (
                ((g["f"].sum() + kf) / (n + kf)).to_dict(),
                ((g["a"].sum() + ka) / (n + ka)).to_dict(),
            )
        self.alpha_ = {}
        for name in ("b", "f"):
            lh, la = self._rates(t, name)
            self.alpha_[name] = (mom_alpha(hc, lh), mom_alpha(ac, la))
        fh, fa = self._rates(t, "f")
        x = self._x(t, fh, fa)
        self.reg_ = {}
        for name, hf, af in (("g", GAME, GAME), ("h", H_FEATS, A_FEATS)):
            pair = []
            for y, base, feats in ((hc, "log_fh", hf), (ac, "log_fa", af)):
                ok = np.isfinite(x[[base, *feats]]).all(axis=1).to_numpy()
                if ok.sum() < MIN_REG:
                    pair = None  # too few rows with features: (f)'s chances
                    break
                m = NBRegression([base, *feats], L2).fit(x[ok], y[ok], np.ones(ok.sum()))
                pair.append(m)
            self.reg_[name] = tuple(pair) if pair else None
        return self

    def _rates(self, df, name):
        tf, ta = self.tables_[name]
        get = lambda d, ts: np.array([d.get(x, 1.0) for x in ts])  # noqa: E731
        h, a = df["home"].to_numpy(), df["away"].to_numpy()
        mh, ma = self.mean_
        return mh * get(tf, h) * get(ta, a), ma * get(tf, a) * get(ta, h)

    @staticmethod
    def _x(df, fh, fa):
        cols = [c for c in GAME + H_FEATS + A_FEATS if c in df]
        x = df[cols].reset_index(drop=True).copy()
        x["log_fh"], x["log_fa"] = np.log(fh), np.log(fa)
        return x

    def predict(self, test: pd.DataFrame) -> np.ndarray:
        k = len(test)
        mh, ma = self.mean_

        def sides(lh, la, alphas):
            return np.hstack([nb_pmf(lh, alphas[0], KMAX), nb_pmf(la, alphas[1], KMAX)])

        out = [sides(np.full(k, mh), np.full(k, ma), self.a_alpha_)]
        for name in ("b", "f"):
            out.append(sides(*self._rates(test, name), self.alpha_[name]))
        f_block = out[-1]
        fh, fa = self._rates(test, "f")
        x = self._x(test, fh, fa)
        for name, hf, af in (("g", GAME, GAME), ("h", H_FEATS, A_FEATS)):
            block = f_block.copy()  # no features: (f)'s chances
            if self.reg_[name] is None:
                out.append(block)
                continue
            mh_, ma_ = self.reg_[name]
            okh = np.isfinite(x[["log_fh", *hf]]).all(axis=1).to_numpy()
            oka = np.isfinite(x[["log_fa", *af]]).all(axis=1).to_numpy()
            ok = okh & oka
            if ok.any():
                block[ok] = sides(mh_.rate(x[ok]), ma_.rate(x[ok]), (mh_.alpha_, ma_.alpha_))
            out.append(block)
        return np.hstack(out)


def predictions(data: pd.DataFrame, start, end, holdout: Holdout) -> dict:
    """Walk-forward distributions per candidate on rows start <= time < end:
    {"index", cand: (home pmf, away pmf)}."""
    p = harness_walk_forward(
        data, Bakeoff2, {}, start, end, holdout=holdout, refit="28D", min_train=MIN_TRAIN
    )
    if p.empty:
        return {"index": pd.Index([])}
    arr = p.to_numpy()
    keep = np.isfinite(arr).all(axis=1)
    arr = arr[keep]
    out: dict = {"index": p.index[keep]}
    for i, c in enumerate(CANDS):
        block = arr[:, i * 2 * N : (i + 1) * 2 * N]
        out[c] = (block[:, :N], block[:, N:])
    return out


def coverage(data: pd.DataFrame, idx) -> dict:
    """Share of scored matches with each candidate's own features (else (f)'s chances)."""
    d = data.loc[idx]
    return {
        "g": float(np.isfinite(d[GAME]).all(axis=1).mean()) if len(d) else None,
        "h": float(np.isfinite(d[H_FEATS + A_FEATS]).all(axis=1).mean()) if len(d) else None,
    }


def score(data: pd.DataFrame, pred: dict, cands, level: float) -> dict:
    """Each candidate on the six team lines vs (a), through corner_recal.score (per-line
    gain and slope, per-match mean gain with its range, the pass rule)."""
    out = {}
    for c in cands:
        rows = corner_recal.long_rows(data, pred, c)
        r = corner_recal.score(rows, "p_raw", level)
        r["name"] = NAMES[c]
        r["family"] = c in FAMILY
        if not r["family"]:
            r["pass"] = False
        out[c] = r
    return out


def finalist(res: dict) -> str | None:
    """The passing family candidate with the largest gain (ties: f, then g, then h)."""
    passing = [
        (r["gain"], -FAMILY.index(c), c) for c, r in res.items() if c in FAMILY and r["pass"]
    ]
    return max(passing)[2] if passing else None


def run(df: pd.DataFrame, level: float, open_reason: str | None = None, final=None) -> dict:
    """Development (open_reason None; every match from 1 July 2024 dropped first) or the
    2026/27 test for `final`, opened once with a logged reason."""
    holdout = Holdout(TEST_START)
    if open_reason is None:
        df = df[df["date"] < DEV_END]
        data = frame(df.sort_values("date").reset_index(drop=True))
        pred = predictions(data, DEV_START, DEV_END, holdout)
        res: dict = {"stage": "development", "candidates": score(data, pred, CANDS[1:], level)}
        res["finalist"] = finalist(res["candidates"])
    else:
        if final not in FAMILY:
            raise SystemExit(f"--finalist must be one of {FAMILY} (from development)")
        if pd.Timestamp.now(tz="UTC") < EARLIEST_OPEN:
            raise SystemExit(f"2026/27 opens no earlier than {EARLIEST_OPEN.date()}")
        df = df[df["date"] < TEST_END]
        data = frame(df.sort_values("date").reset_index(drop=True))
        holdout.unlock(open_reason)
        pred = predictions(data, TEST_START, TEST_END, holdout)
        res = {"stage": "test", "candidates": score(data, pred, ("b", final), level)}
        res["finalist"] = final
        res["judged"] = len(pred["index"]) >= MIN_JUDGED
        if not res["judged"]:
            res["candidates"][final]["pass"] = False
        res["holdout_log"] = holdout.events
    idx = pred["index"]
    res.update(
        level=level,
        matches=int(len(idx)),
        seasons=sorted(int(s) for s in data.loc[idx, "season_start"].unique()),
        coverage=coverage(data, idx),
    )
    return res


# ---------- loading (network: GitHub Actions) ----------

EXTRA = {
    "home_fouls": "HF",
    "away_fouls": "AF",
    "home_yellow": "HY",
    "away_yellow": "AY",
    "home_red": "HR",
    "away_red": "AR",
}


def load(league: str, cut: pd.Timestamp, last: int) -> pd.DataFrame:
    """corners.load plus shots on target, fouls, cards and Pinnacle's early 1X2 from the
    same football-data files (every match from `cut` on dropped)."""
    from soccer_stats.data import download, season_code
    from soccer_stats.edge import books

    df = corners.load(league, cut, last=last)
    frames = []
    for y in range(corners.FIRST_DATA, last + 1):
        raw = pd.read_csv(download(league, y), encoding="latin-1", on_bad_lines="skip")
        px = books.book_prices(raw, season=season_code(y))
        played = raw.dropna(subset=["HomeTeam", "AwayTeam", "FTHG", "FTAG"]).reset_index(drop=True)
        for col, src in EXTRA.items():
            px[col] = pd.to_numeric(played[src], errors="coerce") if src in played else np.nan
        frames.append(px)
    px = pd.concat(frames, ignore_index=True)
    px = px[px["date"] < cut]
    px["home_cards"] = px["home_yellow"] + 2 * px["home_red"]
    px["away_cards"] = px["away_yellow"] + 2 * px["away_red"]
    keep = ["date", "home", "away", "home_sot", "away_sot", "home_fouls", "away_fouls"]
    keep += ["home_cards", "away_cards", *GAME_PRICES]
    return df.merge(
        px[keep].drop_duplicates(["date", "home", "away"]), on=["date", "home", "away"], how="left"
    )


# ---------- report ----------


def _rng(v, f="{:+.4f}"):
    return f"{f.format(v[0])}..{f.format(v[1])}" if v else "–"


def report(league: str, res: dict) -> str:
    out = [
        f"== {league}: corners bake-off 2, {res['stage']} (level {res['level']:.3%}), "
        f"{res['matches']} matches, seasons {res['seasons']}; feature coverage "
        f"g {res['coverage']['g']}, h {res['coverage']['h']} =="
    ]
    out += res.get("holdout_log", [])
    for c, r in res["candidates"].items():
        tag = "" if r["family"] else " (reference, outside the family)"
        out.append(
            f"  ({c}) {r['name']}{tag}: gain {r['gain']:+.4f} ({_rng(r['gain_range'])}), "
            f"slopes {min(r['slopes'].values()):.2f}-{max(r['slopes'].values()):.2f}, "
            f"in band {r['slopes_in_band']}: PASS {r['pass']}"
        )
        for name, s in r["lines"].items():
            out.append(
                f"      {name:>9}: over {s['over_rate']:.3f} vs predicted {s['predicted']:.3f}, "
                f"slope {s['slope']:.2f} ({_rng(s['slope_range'], '{:.2f}')}), "
                f"gain {s['gain']:+.4f}"
            )
    if res["stage"] == "development":
        out.append(f"Finalist: {res['finalist'] or 'none (nothing passes; no 2026/27 test)'}")
    else:
        out.append(f"Judged: {res['judged']} (at least {MIN_JUDGED} matches)")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> None:
    import argparse
    import json
    from pathlib import Path

    from soccer_stats.edge.stats import bonferroni_level
    from soccer_stats.lab.run import _jsonable

    ap = argparse.ArgumentParser(prog="python -m soccer_stats.edge.corners2")
    ap.add_argument("--league", default="E0", choices=LEAGUES)
    ap.add_argument("--reason", default="")
    ap.add_argument("--finalist", default="")
    ap.add_argument("--tests", type=int, default=len(LEAGUES), help="test: leagues judged")
    ap.add_argument("--json")
    args = ap.parse_args(argv)
    reason = args.reason.strip() or None
    if reason:
        level = bonferroni_level(args.tests)
        df = load(args.league, TEST_END, last=TEST_SEASON)
    else:
        level = bonferroni_level(DEV_TESTS)
        df = load(args.league, DEV_END, last=corners.LAST_DATA - 1)
    res = run(df, level, reason, args.finalist.strip() or None)
    print(report(args.league, res))
    text = json.dumps(_jsonable(res), default=str)
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(text)
    print("CORNERS2_JSON " + text)


if __name__ == "__main__":
    main()
