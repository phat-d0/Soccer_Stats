"""Favourites coming back from behind (owner, 24 Oct; docs/lab.md, pre-registered).

Phase 1, free data only: does the pre-match favourite (Pinnacle's closing 1X2) come back
from behind more often than a fair, time-scaled in-play model says?

* Event A, conceded first: the underdog scores the first goal (Understat goal minutes,
  E0, SP1, D1, I1, F1). Minute bands 0-30, 30-60, 60+.
* Event B, behind at half-time: football-data's HTHG/HTAG, all eight domestic leagues.
* Benchmark: independent Poisson means fitted to Pinnacle's margin-free closing 1X2 and
  over/under 2.5 (`team_totals.implied_means`), each side's remaining goals Poisson with
  mean lambda x minutes left / 90, conditioned on the score at the event.

Development 2017/18-2024/25; every match from 1 July 2025 is dropped before anything is
computed (`Holdout`). 18 tests on the win residual (realized - predicted), 99.722% ranges.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.special import expit
from scipy.stats import skellam

from soccer_stats.data import RAW_DIR, download, season_code
from soccer_stats.lab import metrics
from soccer_stats.lab.harness import Holdout
from soccer_stats.odds import devig_shin

LEAGUES_A = ("E0", "SP1", "D1", "I1", "F1")  # Understat goal minutes
LEAGUES_B = (*LEAGUES_A, "E1", "E2", "E3")  # half-time scores
YEARS = range(2017, 2025)  # 2017/18-2024/25
HOLDOUT_START = pd.Timestamp("2025-07-01")
BANDS = ((0.45, 0.55), (0.55, 0.65), (0.65, 0.75), (0.75, 1.01))
MINUTE_BANDS = ((0, 30), (30, 60), (60, 200))
TESTS = len(BANDS) * len(MINUTE_BANDS) + len(BANDS) + 2  # 18
MIN_BENCH_COVERAGE = 0.90
FULL_TIME = 90.0
PAUSE = 0.3


def band_name(lo: float, hi: float) -> str:
    return f"{lo:.0%}-{min(hi, 1.0):.0%}" if hi <= 1.0 else f"{lo:.0%}+"


def minute_name(lo: int, hi: int) -> str:
    return f"{lo}-{hi}" if hi < 200 else f"{lo}+"


# ---------- football-data ----------


def _num(raw: pd.DataFrame, col: str) -> pd.Series:
    if col not in raw:
        return pd.Series(np.nan, index=raw.index)
    return pd.to_numeric(raw[col], errors="coerce")


def parse_fd(raw: pd.DataFrame, league: str, year: int) -> pd.DataFrame:
    """One row per played match: teams, full- and half-time score, Pinnacle's close."""
    df = raw.dropna(subset=["HomeTeam", "AwayTeam", "FTHG", "FTAG"]).reset_index(drop=True)
    out = pd.DataFrame(
        {
            "league": league,
            "season_start": year,
            "date": pd.to_datetime(df["Date"], dayfirst=True, format="mixed"),
            "home": df["HomeTeam"],
            "away": df["AwayTeam"],
            "fthg": _num(df, "FTHG"),
            "ftag": _num(df, "FTAG"),
            "hthg": _num(df, "HTHG"),
            "htag": _num(df, "HTAG"),
            "ph": _num(df, "PSCH"),
            "pd": _num(df, "PSCD"),
            "pa": _num(df, "PSCA"),
            "pov": _num(df, "PC>2.5"),
            "pun": _num(df, "PC<2.5"),
        }
    )
    return out


def load_fd(league: str, years=YEARS, raw_dir: Path = RAW_DIR) -> pd.DataFrame:
    frames = []
    for y in years:
        raw = pd.read_csv(download(league, y, raw_dir), encoding="latin-1", on_bad_lines="skip")
        frames.append(parse_fd(raw, league, y))
    df = pd.concat(frames, ignore_index=True)
    return df[df["date"] < HOLDOUT_START].reset_index(drop=True)


def _fair(prices: np.ndarray) -> np.ndarray:
    """Shin-fair chances; a book with no margin left to remove is just normalised."""
    try:
        return devig_shin(prices)
    except ValueError:
        pi = 1 / prices
        return pi / pi.sum()


def _ok(*cols) -> np.ndarray:
    return np.all([np.isfinite(c) & (c > 1) for c in cols], axis=0)


def favourites(df: pd.DataFrame) -> pd.DataFrame:
    """Shin-fair closing chances, the favourite's side and chance, its strength band, and
    the fair over-2.5 chance. Matches without Pinnacle's close or with a favourite under
    45% are dropped (the caller counts them)."""
    df = df.copy()
    has = _ok(df["ph"], df["pd"], df["pa"])
    fair = np.full((len(df), 3), np.nan)
    for i in np.flatnonzero(has):
        fair[i] = _fair(df.loc[i, ["ph", "pd", "pa"]].to_numpy(float))
    df[["fh", "fd", "fa"]] = fair
    ou = _ok(df["pov"], df["pun"])
    fo = np.full(len(df), np.nan)
    for i in np.flatnonzero(ou):
        fo[i] = _fair(df.loc[i, ["pov", "pun"]].to_numpy(float))[0]
    df["f_over"] = fo
    df["fav_home"] = df["fh"] >= df["fa"]
    df["p_fav"] = np.where(df["fav_home"], df["fh"], df["fa"])
    df = df[has & (df["p_fav"] >= BANDS[0][0])].copy()
    df["band"] = pd.cut(
        df["p_fav"], [b[0] for b in BANDS] + [BANDS[-1][1]], right=False, labels=False
    ).astype(int)
    fav_ft = np.where(df["fav_home"], df["fthg"], df["ftag"])
    dog_ft = np.where(df["fav_home"], df["ftag"], df["fthg"])
    df["result"] = np.select([fav_ft > dog_ft, fav_ft == dog_ft], ["W", "D"], "L")
    return df.reset_index(drop=True)


# ---------- Understat goal minutes ----------


def goal_events(data: dict) -> list[tuple[int, str, bool]]:
    """(minute, list side 'h'/'a', own goal) for every goal in Understat's shot data."""
    out = []
    for side in ("h", "a"):
        for s in (data.get("shots") or {}).get(side, []) or []:
            r = s.get("result")
            if r in ("Goal", "OwnGoal"):
                out.append((int(float(s.get("minute") or 0)), side, r == "OwnGoal"))
    return sorted(out)


def credit(events, own_goal_flips: bool) -> list[tuple[int, str]]:
    """(minute, scoring side) under one own-goal convention: listed under the side that
    scored it (no flip) or under the player's own side (flip)."""
    other = {"h": "a", "a": "h"}
    return [(m, other[s] if (og and own_goal_flips) else s) for m, s, og in events]


def understat_goals(league: str, years=YEARS, raw_dir: Path = RAW_DIR, fetch: bool = False):
    """One row per Understat match: date, teams (football-data names) and its goal events.
    With `fetch`, downloads missing match files (free, paced); returns (rows, missing)."""
    from soccer_stats.player_data import fetch_match, match_path, season_matches

    rows, missing = [], 0
    for y in years:
        for m in season_matches(league, y, raw_dir).query("played").to_dict("records"):
            path = match_path(m["match_id"], raw_dir)
            if not path.exists():
                if not fetch:
                    missing += 1
                    continue
                try:
                    fetch_match(m["match_id"], raw_dir)
                    time.sleep(PAUSE)
                except Exception:  # noqa: BLE001 - a failed page is counted, not fatal
                    missing += 1
                    continue
            try:
                data = json.loads(path.read_text())
            except ValueError:
                missing += 1
                continue
            rows.append(
                {
                    "league": league,
                    "date": pd.Timestamp(m["kickoff"]).tz_convert(None).normalize(),
                    "home": m["home"],
                    "away": m["away"],
                    "events": goal_events(data),
                }
            )
    return pd.DataFrame(rows), missing


def attach_goals(fav: pd.DataFrame, us: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Join Understat's goals to football-data matches (same teams, dates within a day),
    pick the own-goal convention that rebuilds more final scores, drop matches whose
    rebuilt score differs. Returns (matches with `goals`, counts)."""
    if us.empty:
        return fav.iloc[0:0].assign(goals=None), {"joined": 0}
    m = fav.merge(us, on=["league", "home", "away"], suffixes=("", "_us"))
    m = m[(m["date"] - m["date_us"]).abs() <= pd.Timedelta(days=1)]
    m = m.drop_duplicates(["league", "date", "home", "away"]).reset_index(drop=True)

    def rebuilt(flip):
        g = m["events"].map(lambda ev: credit(ev, flip))
        h = g.map(lambda x: sum(s == "h" for _, s in x))
        a = g.map(lambda x: sum(s == "a" for _, s in x))
        return g, (h == m["fthg"]) & (a == m["ftag"])

    g0, ok0 = rebuilt(False)
    g1, ok1 = rebuilt(True)
    flip = bool(ok1.sum() > ok0.sum())
    g, ok = (g1, ok1) if flip else (g0, ok0)
    counts = {
        "joined": int(len(m)),
        "own_goal_listed_under_scorers_side": not flip,
        "score_rebuilt": int(ok.sum()),
        "score_mismatch_dropped": int((~ok).sum()),
    }
    m = m.assign(goals=g)[ok].drop(columns=["events", "date_us"]).reset_index(drop=True)
    return m, counts


# ---------- events ----------


def event_a(m: pd.DataFrame) -> pd.DataFrame:
    """Matches where the underdog scored first: minute and minute band."""
    rows = []
    for i, r in m.iterrows():
        if not r["goals"]:
            continue
        first = r["goals"][0]
        if len(r["goals"]) > 1 and r["goals"][1][0] == first[0] and r["goals"][1][1] != first[1]:
            continue  # both sides in the same minute: order unknown
        fav_side = "h" if r["fav_home"] else "a"
        if first[1] != fav_side:
            rows.append((i, first[0]))
    out = m.loc[[i for i, _ in rows]].copy()
    out["minute"] = [mm for _, mm in rows]
    out["deficit"] = 1
    out["left"] = (FULL_TIME - out["minute"].clip(0, FULL_TIME)) / FULL_TIME
    out["mband"] = pd.cut(
        out["minute"],
        [b[0] for b in MINUTE_BANDS] + [MINUTE_BANDS[-1][1]],
        right=False,
        labels=False,
    )
    return out.reset_index(drop=True)


def event_b(fav: pd.DataFrame) -> pd.DataFrame:
    """Matches where the favourite trailed at half-time, with the deficit."""
    f = fav.dropna(subset=["hthg", "htag"]).copy()
    fh = np.where(f["fav_home"], f["hthg"], f["htag"])
    dh = np.where(f["fav_home"], f["htag"], f["hthg"])
    f["deficit"] = dh - fh
    out = f[f["deficit"] > 0].copy()
    out["left"] = 0.5
    out["minute"] = 45
    return out.reset_index(drop=True)


# ---------- the fair time-scaled model ----------


def pre_match_means(row) -> tuple[float, float] | None:
    """(favourite, underdog) Poisson means from Pinnacle's fair 1X2 and over/under 2.5."""
    from soccer_stats.team_totals import implied_means

    if not np.isfinite(row["f_over"]):
        return None
    lh, la = implied_means(
        {"home": row["fh"], "draw": row["fd"], "away": row["fa"]},
        {"over25": row["f_over"], "under25": 1 - row["f_over"]},
    )
    return (lh, la) if row["fav_home"] else (la, lh)


def in_play(lf: float, lu: float, deficit: int, left: float) -> tuple[float, float]:
    """P(favourite wins), P(draw) when it trails by `deficit` with `left` of the match to go."""
    if left <= 0:
        return 0.0, 0.0
    mu1, mu2 = lf * left, lu * left
    win = float(skellam.sf(deficit, mu1, mu2))  # diff > deficit
    draw = float(skellam.pmf(deficit, mu1, mu2))
    return win, draw


def add_predictions(ev: pd.DataFrame) -> pd.DataFrame:
    ev = ev.copy()
    pw, pdr = np.full(len(ev), np.nan), np.full(len(ev), np.nan)
    for i, r in enumerate(ev.itertuples(index=False)):
        means = pre_match_means(r._asdict())
        if means is None:
            continue
        pw[i], pdr[i] = in_play(*means, int(r.deficit), float(r.left))
    ev["p_win"], ev["p_draw"] = pw, pdr
    ev["y_win"] = (ev["result"] == "W").astype(int)
    ev["y_draw"] = (ev["result"] == "D").astype(int)
    return ev


def bench_seasons(ev: pd.DataFrame) -> pd.DataFrame:
    """Keep league-seasons where the benchmark covers >= 90% of the event's matches."""
    cov = ev.groupby(["league", "season_start"])["p_win"].apply(lambda s: s.notna().mean())
    keep = cov[cov >= MIN_BENCH_COVERAGE].index
    idx = ev.set_index(["league", "season_start"]).index.isin(keep)
    return ev[idx & ev["p_win"].notna().to_numpy()]


# ---------- scoring ----------


def rates(x: pd.DataFrame, level: float = 0.95) -> dict:
    """The favourite's realized win/draw/loss/avoided-defeat rates with match ranges."""
    out = {"matches": int(len(x))}
    for name, y in (
        ("win", x["result"] == "W"),
        ("draw", x["result"] == "D"),
        ("loss", x["result"] == "L"),
        ("avoided_defeat", x["result"] != "L"),
    ):
        v = y.to_numpy(float)
        out[name] = float(v.mean()) if len(v) else None
        out[f"{name}_range"] = metrics.boot_range(v, level=level) if len(v) > 1 else None
    return out


def residuals(x: pd.DataFrame, level: float) -> dict:
    """Realized minus predicted (win, draw, avoided defeat), with match-resampled ranges."""
    out = {"benchmarked": int(len(x))}
    if len(x) < 2:
        return out
    for name, y, p in (
        ("win", x["y_win"], x["p_win"]),
        ("draw", x["y_draw"], x["p_draw"]),
        ("avoided_defeat", x["y_win"] + x["y_draw"], x["p_win"] + x["p_draw"]),
    ):
        r = (y - p).to_numpy(float)
        out[f"{name}_predicted"] = float(p.mean())
        out[f"{name}_realized"] = float(y.mean())
        out[f"{name}_residual"] = float(r.mean())
        out[f"{name}_residual_range"] = metrics.boot_range(r, level=level)
    return out


def calibration(x: pd.DataFrame) -> dict | None:
    """Logistic intercept and slope of the win on logit(predicted win); 1 = well scaled."""
    p = np.clip(x["p_win"].to_numpy(float), 1e-6, 1 - 1e-6)
    y = x["y_win"].to_numpy(float)
    if len(y) < 50 or y.min() == y.max():
        return None
    X = np.column_stack([np.ones(len(p)), np.log(p / (1 - p))])
    w = np.array([0.0, 1.0])
    for _ in range(50):
        mu = expit(X @ w)
        h = (X * (mu * (1 - mu))[:, None]).T @ X + 1e-9 * np.eye(2)
        step = np.linalg.solve(h, X.T @ (y - mu))
        w += step
        if np.abs(step).max() < 1e-10:
            break
    return {"intercept": float(w[0]), "slope": float(w[1]), "rows": int(len(y))}


def cell(ev: pd.DataFrame, bench: pd.DataFrame, level: float) -> dict:
    out = rates(ev)
    res = residuals(bench, level)
    out.update(res)
    rng = res.get("win_residual_range")
    out["test"] = (
        "above model" if rng and rng[0] > 0 else "below model" if rng and rng[1] < 0 else "no gap"
    )
    return out


def analyse(a: pd.DataFrame, b: pd.DataFrame, level: float) -> dict:
    """Every cell of the family, the calibration, minute and deficit splits, per league."""
    ab, bb = bench_seasons(a), bench_seasons(b)
    res: dict = {"level": level, "tests": TESTS, "event_a": {}, "event_b": {}}
    for i, (lo, hi) in enumerate(BANDS):
        name = band_name(lo, hi)
        for j, (mlo, mhi) in enumerate(MINUTE_BANDS):
            key = f"{name} | {minute_name(mlo, mhi)}"
            sel = lambda d, i=i, j=j: d[(d["band"] == i) & (d["mband"] == j)]  # noqa: E731
            res["event_a"][key] = cell(sel(a), sel(ab), level)
        res["event_b"][name] = cell(b[b["band"] == i], bb[bb["band"] == i], level)
    res["event_a_pooled"] = cell(a, ab, level)
    res["event_b_pooled"] = cell(b, bb, level)
    res["calibration"] = {"event_a": calibration(ab), "event_b": calibration(bb)}
    res["minutes_of_first_underdog_goal"] = {
        minute_name(lo, hi): int(((a["minute"] >= lo) & (a["minute"] < hi)).sum())
        for lo, hi in MINUTE_BANDS
    }
    res["event_b_by_deficit"] = {
        "1": cell(b[b["deficit"] == 1], bb[bb["deficit"] == 1], 0.95),
        "2+": cell(b[b["deficit"] >= 2], bb[bb["deficit"] >= 2], 0.95),
    }
    res["event_a_by_league"] = {
        lg: cell(a[a["league"] == lg], ab[ab["league"] == lg], 0.95)
        for lg in sorted(a["league"].unique())
    }
    res["event_b_by_league"] = {
        lg: cell(b[b["league"] == lg], bb[bb["league"] == lg], 0.95)
        for lg in sorted(b["league"].unique())
    }
    tested = [
        *res["event_a"].values(),
        *res["event_b"].values(),
        res["event_a_pooled"],
        res["event_b_pooled"],
    ]
    res["above_model"] = [
        k
        for k, v in [*res["event_a"].items(), *res["event_b"].items()]
        if v["test"] == "above model"
    ] + [k for k in ("event_a_pooled", "event_b_pooled") if res[k]["test"] == "above model"]
    res["below_model"] = sum(v["test"] == "below model" for v in tested)
    res["verdict"] = (
        "gap found"
        if res["above_model"]
        else "below the model"
        if res["below_model"]
        else "no gap shown"
    )
    return res


# ---------- coverage (counts only, no outcomes) ----------


def coverage(fd: pd.DataFrame, us: pd.DataFrame | None, missing: int) -> dict:
    """Per season: matches, Pinnacle 1X2 and over/under close, half-time scores, Understat
    matches on file. Reads no result."""
    out = {}
    for y, g in fd.groupby("season_start"):
        row = {
            "matches": int(len(g)),
            "pinnacle_1x2": int(_ok(g["ph"], g["pd"], g["pa"]).sum()),
            "pinnacle_ou25": int(_ok(g["pov"], g["pun"]).sum()),
            "half_time": int(g[["hthg", "htag"]].notna().all(axis=1).sum()),
        }
        if us is not None and len(us):
            lo, hi = pd.Timestamp(f"{y}-07-01"), pd.Timestamp(f"{y + 1}-07-01")
            row["understat_files"] = int(((us["date"] >= lo) & (us["date"] < hi)).sum())
        out[season_code(int(y))] = row
    if us is not None:
        out["understat_missing"] = int(missing)
    return out


# ---------- runs ----------


def build(leagues_b=LEAGUES_B, leagues_a=LEAGUES_A, raw_dir: Path = RAW_DIR):
    """Event tables from the cached files (no downloads), plus the data counts."""
    holdout = Holdout(HOLDOUT_START)
    a_parts, b_parts, counts = [], [], {}
    for lg in leagues_b:
        fd = load_fd(lg, raw_dir=raw_dir)
        holdout.check(fd["date"])
        fav = favourites(fd)
        c = {"matches": int(len(fd)), "with_favourite_45": int(len(fav))}
        b_parts.append(event_b(fav))
        if lg in leagues_a:
            us, missing = understat_goals(lg, raw_dir=raw_dir)
            m, jc = attach_goals(fav, us)
            c.update(jc, understat_missing=missing)
            a_parts.append(event_a(m))
        counts[lg] = c
    a = add_predictions(pd.concat(a_parts, ignore_index=True)) if a_parts else pd.DataFrame()
    b = add_predictions(pd.concat(b_parts, ignore_index=True))
    return a, b, counts


def report(res: dict, counts: dict) -> str:
    def rng(v, f="{:+.1%}"):
        return f"{f.format(v[0])}..{f.format(v[1])}" if v else "-"

    def line(k, c):
        if "win_residual" not in c:
            return f"  {k:<22} n={c['matches']:5d} win {c['win'] or 0:.1%} (no benchmark)"
        return (
            f"  {k:<22} n={c['matches']:5d} (bench {c['benchmarked']:5d}) "
            f"win {c['win']:.1%} draw {c['draw']:.1%} avoid {c['avoided_defeat']:.1%} | "
            f"model win {c['win_predicted']:.1%}: residual {c['win_residual']:+.1%} "
            f"({rng(c['win_residual_range'])}) avoid resid {c['avoided_defeat_residual']:+.1%} "
            f"-> {c['test']}"
        )

    out = [f"== Favourites coming back (level {res['level']:.3%}, {res['tests']} tests) =="]
    out += [f"  data {lg}: {c}" for lg, c in counts.items()]
    out.append("Event A, the underdog scores first (E0, SP1, D1, I1, F1):")
    out += [line(k, c) for k, c in res["event_a"].items()]
    out.append(line("pooled", res["event_a_pooled"]))
    out.append(f"  first underdog goal minutes: {res['minutes_of_first_underdog_goal']}")
    out.append("Event B, behind at half-time (eight leagues):")
    out += [line(k, c) for k, c in res["event_b"].items()]
    out.append(line("pooled", res["event_b_pooled"]))
    out.append("Event B by deficit (95%):")
    out += [line(k, c) for k, c in res["event_b_by_deficit"].items()]
    out.append(f"Calibration (win on logit model win): {res['calibration']}")
    out.append("By league (95%), event A:")
    out += [line(k, c) for k, c in res["event_a_by_league"].items()]
    out.append("By league (95%), event B:")
    out += [line(k, c) for k, c in res["event_b_by_league"].items()]
    out.append(f"VERDICT: {res['verdict']}; above the model in: {res['above_model'] or 'none'}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> None:
    import argparse

    from soccer_stats.edge.stats import bonferroni_level
    from soccer_stats.lab.run import _jsonable

    ap = argparse.ArgumentParser(prog="python -m soccer_stats.lab.comebacks")
    ap.add_argument("--fetch", choices=LEAGUES_A, help="download one league's Understat files")
    ap.add_argument("--json")
    args = ap.parse_args(argv)
    if args.fetch:
        fd = load_fd(args.fetch)
        us, missing = understat_goals(args.fetch, fetch=True)
        cov = coverage(fd, us, missing)
        print(f"{args.fetch}: counts only (no outcomes read)")
        for k, v in cov.items():
            print(f"  {k}: {v}")
        print("COVERAGE_JSON " + json.dumps(cov))
        return
    a, b, counts = build()
    res = analyse(a, b, bonferroni_level(TESTS))
    print(report(res, counts))
    text = json.dumps(_jsonable({"counts": counts, **res}), default=str)
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(text)
    print("COMEBACKS_JSON " + text)


if __name__ == "__main__":
    main()
