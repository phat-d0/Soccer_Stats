"""Does model (f)'s side predict Pinnacle's look → close move on team corners?
(corners team, 8 Nov 2026; pre-registered in docs/totals.md "Corner price moves").

Prices only: the live corner log's quotes (`odds_log/<code>_corners_*.jsonl`) and the
model's chance logged beside each quote (`p_model_over`, computed when the quote was
logged, so no look-ahead). No corner count, result or trade outcome is read.

Per book, event, team and half line: the newest two-sided "look" quote and the newest
two-sided "close" quote (kickoffs before `before`). r = model − Pinnacle's Shin-fair over
at the look; m = fair over at the close − at the look. The primary measure is the mean of
sign(r) × m on Pinnacle's lines (the move toward the model's side, in chance points), with
a 95% range resampling matches (2,000 draws, seed 0); the signal passes when the range is
above 0. Descriptive: the slope of m on r, the shares moving toward and away (|m| > 0.005),
the main line per team (fair over nearest 0.5), the lines where the 12% rule would bet at
the look, per league, and DraftKings' lines.
"""

from __future__ import annotations

import glob
import json
from pathlib import Path

import numpy as np
import pandas as pd

BEFORE = "2026-11-08T00:00:00Z"
DRAWS, SEED = 2000, 0
MOVED = 0.005
EDGE = 0.12
KEY = ["book", "league", "event_id", "team", "line"]


def load(log_dir: Path) -> pd.DataFrame:
    """Every team-corner quote row in `log_dir/odds_log` (rows without `book` are
    Pinnacle's)."""
    rows = []
    for f in sorted(glob.glob(str(Path(log_dir) / "odds_log" / "*_corners_2*.jsonl"))):
        with open(f) as fh:
            rows += [json.loads(x) for x in fh if x.strip()]
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    if "book" not in df:
        df["book"] = None
    df["book"] = df["book"].fillna("pinnacle")
    if "market" in df:
        df = df[df["market"].fillna("team_corners") == "team_corners"]
    return df


def pairs(q: pd.DataFrame, before: str = BEFORE) -> pd.DataFrame:
    """One row per book, event, team and half line with a two-sided look and close."""
    q = q[
        q["over"].notna()
        & q["under"].notna()
        & q["fair_over"].notna()
        & q["p_model_over"].notna()
        & ((q["line"] % 1) == 0.5)
        & (pd.to_datetime(q["kickoff"], utc=True) < pd.Timestamp(before))
    ].sort_values("fetched_at")
    look = q[q["snapshot"] == "look"].groupby(KEY).last()
    close = q[q["snapshot"] == "close"].groupby(KEY).last()
    j = look.join(close[["fair_over", "over", "under"]], rsuffix="_close", how="inner")
    j = j.reset_index()
    j["r"] = j["p_model_over"] - j["fair_over"]
    j["m"] = j["fair_over_close"] - j["fair_over"]
    j["toward"] = np.sign(j["r"]) * j["m"]
    edge_over = j["p_model_over"] * j["over"] - 1
    edge_under = (1 - j["p_model_over"]) * j["under"] - 1
    j["rule_bets"] = (edge_over >= EDGE) | (edge_under >= EDGE)
    j["dist"] = (j["fair_over"] - 0.5).abs()
    j["main"] = j["dist"] == j.groupby(["book", "event_id", "team"])["dist"].transform("min")
    return j


def _boot(x: pd.DataFrame, stat, draws: int = DRAWS, seed: int = SEED):
    groups = [g for _, g in x.groupby("event_id")]
    if len(groups) < 2:
        return None
    rng = np.random.default_rng(seed)
    vals = []
    for _ in range(draws):
        idx = rng.integers(0, len(groups), len(groups))
        vals.append(stat(pd.concat([groups[i] for i in idx])))
    lo, hi = np.nanpercentile(vals, [2.5, 97.5])
    return [float(lo), float(hi)]


def _slope(x: pd.DataFrame) -> float:
    v = x["r"].var()
    return float(np.cov(x["r"], x["m"])[0, 1] / v) if len(x) > 2 and v > 0 else np.nan


def block(x: pd.DataFrame, draws: int = DRAWS) -> dict:
    """The measures on one set of lines."""
    if x.empty:
        return {"lines": 0}
    mean = lambda d: float(d["toward"].mean())  # noqa: E731
    return {
        "lines": int(len(x)),
        "matches": int(x["event_id"].nunique()),
        "toward_mean": mean(x),
        "toward_ci95": _boot(x, mean, draws),
        "slope": _slope(x),
        "slope_ci95": _boot(x, _slope, draws),
        "moved_toward": float((x["toward"] > MOVED).mean()),
        "moved_away": float((x["toward"] < -MOVED).mean()),
        "abs_r_mean": float(x["r"].abs().mean()),
        "abs_m_mean": float(x["m"].abs().mean()),
    }


def analyse(log_dir: Path, before: str = BEFORE, draws: int = DRAWS) -> dict:
    j = pairs(load(log_dir), before)
    pin = j[j["book"] == "pinnacle"]
    primary = block(pin, draws)
    ci = primary.get("toward_ci95")
    verdict = "pass" if ci and ci[0] > 0 else "fail" if ci else "no data"
    return {
        "before": before,
        "primary": primary,
        "verdict": verdict,
        "main_line": block(pin[pin["main"]], draws),
        "rule_bets": block(pin[pin["rule_bets"]], draws),
        "by_league": {lg: block(g, draws) for lg, g in pin.groupby("league")},
        "draftkings": block(j[j["book"] == "draftkings"], draws),
    }


def report(res: dict) -> str:
    def line(name, b):
        if not b.get("lines"):
            return f"{name}: no lines"
        ci, sc = b["toward_ci95"] or [np.nan, np.nan], b["slope_ci95"] or [np.nan, np.nan]
        return (
            f"{name}: {b['lines']} lines, {b['matches']} matches; toward the model "
            f"{b['toward_mean'] * 100:+.2f} pts ({ci[0] * 100:+.2f} to {ci[1] * 100:+.2f}); "
            f"slope {b['slope']:+.3f} ({sc[0]:+.3f} to {sc[1]:+.3f}); moved toward "
            f"{b['moved_toward']:.0%}, away {b['moved_away']:.0%}; |r| {b['abs_r_mean'] * 100:.1f}"
            f" pts, |m| {b['abs_m_mean'] * 100:.1f} pts"
        )

    out = [
        f"== Corner price moves (kickoffs before {res['before']}) ==",
        f"VERDICT {res['verdict']}",
    ]
    out.append(line("Pinnacle, all half lines (primary)", res["primary"]))
    out.append(line("Pinnacle, main line per team", res["main_line"]))
    out.append(line("Pinnacle, lines the 12% rule bets", res["rule_bets"]))
    out += [line(f"  {lg}", b) for lg, b in res["by_league"].items()]
    out.append(line("DraftKings, all half lines", res["draftkings"]))
    return "\n".join(out)


def main(argv: list[str] | None = None) -> None:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m soccer_stats.edge.corner_moves")
    ap.add_argument("--log-dir", required=True, help="folder holding odds_log/")
    ap.add_argument("--before", default=BEFORE)
    ap.add_argument("--json")
    args = ap.parse_args(argv)
    res = analyse(Path(args.log_dir), args.before)
    print(report(res))
    text = json.dumps(res, default=float)
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(text)
    print("CORNER_MOVES_JSON " + text)


if __name__ == "__main__":
    main()
