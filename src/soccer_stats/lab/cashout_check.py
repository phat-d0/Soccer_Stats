"""Checks on the cash-out test's hold line (lead, 28 Oct): why holding a 4.0+ underdog
returned -24.6% per $1 in run 40706645115. Read-only and free (no key): football-data and
the cached snapshots only. It never changes the frozen analysis and never reads design H's
outcomes (H's matches are counted, never joined to a result).

1. football-data alone on every frozen match (joined by league, season, teams and kickoff):
   hold ROI by band, league, season and price source with each source's margin; mean of
   won x odds against mean 1/odds; average odds on winners and losers.
2. The result join of `cashout_test.build` (teams only): pairs that meet more than once at
   the same ground in a season, and positions whose result differs from the kickoff join.
3. Analysed positions against unmatched matches (league, season, price, band, hold ROI).
4. The matches the sample left out because B or C had bought them (spent): their half-time
   state and hold ROI, and the hold ROI with them put back. H's are counted only.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from soccer_stats.data import RAW_DIR
from soccer_stats.lab import cashout_plan as xp
from soccer_stats.lab import cashout_test as ct
from soccer_stats.lab import comeback_fetch as cf
from soccer_stats.lab import comebacks2 as c2
from soccer_stats.lab.comeback_plan import load_season

BANDS = {"4+": (4.0, np.inf), "3-4": (3.0, 4.0)}


def frozen() -> pd.DataFrame:
    res = json.loads(cf.slots_path("X").read_text())
    rows = [
        {"league": s["league"], "kickoff": s["kickoff"], "season": c2.season_of(s["kickoff"]), **m}
        for s in res["matches"]
        for m in s["matches"]
    ]
    return pd.DataFrame(rows)


def seasons(pairs) -> pd.DataFrame:
    frames = []
    for lg, y in sorted(pairs):
        df = load_season(lg, y)
        df["ko"] = df["kickoff"].dt.strftime("%Y-%m-%dT%H:%MZ")
        df["season"] = y
        frames.append(df)
    return pd.concat(frames, ignore_index=True)


def outcome(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    home = df["dog"] == "home"
    df["won"] = np.where(home, df["fthg"] > df["ftag"], df["ftag"] > df["fthg"]).astype(int)
    df["ht_lead"] = np.where(home, df["hthg"] > df["htag"], df["htag"] > df["hthg"]).astype(int)
    df["profit"] = np.where(df["won"] == 1, df["dog_odds"] - 1, -1.0)
    pin = df["price"] == "pinnacle"
    h, d, a = (np.where(pin, df[p], df[q]) for p, q in (("ph", "ah"), ("pd", "ad"), ("pa", "aa")))
    df["margin"] = 1 / h + 1 / d + 1 / a - 1
    return df


def summary(df: pd.DataFrame) -> dict:
    if df.empty:
        return {"n": 0}
    won = df["won"] == 1
    return {
        "n": len(df),
        "roi": round(float(df["profit"].mean()), 4),
        "roi_ci95": ct._boot(df["profit"].to_numpy(float)),
        "won": round(float(won.mean()), 4),
        "implied": round(float((1 / df["dog_odds"]).mean()), 4),
        "won_x_odds": round(float((won * df["dog_odds"]).mean()), 4),
        "margin": round(float(df["margin"].mean()), 4),
        "odds_winners": round(float(df.loc[won, "dog_odds"].mean()), 3) if won.any() else None,
        "odds_losers": round(float(df.loc[~won, "dog_odds"].mean()), 3),
        "ht_lead": round(float(df["ht_lead"].mean()), 4),
    }


def by(df: pd.DataFrame, col: str) -> dict:
    return {str(k): summary(g) for k, g in df.groupby(col)}


def band(df: pd.DataFrame, name: str) -> pd.DataFrame:
    lo, hi = BANDS[name]
    return df[(df["dog_odds"] >= lo) & (df["dog_odds"] < hi)]


def key(df: pd.DataFrame) -> pd.Series:
    return df["league"] + "|" + df["kickoff"] + "|" + df["home"]


def run(cache_dir: Path) -> dict:
    x = frozen()
    fd = seasons({(r.league, r.season) for r in x.itertuples()})
    cols = ["league", "season", "home", "away", "ko", "fthg", "ftag", "hthg", "htag"]
    cols += ["ph", "pd", "pa", "ah", "ad", "aa"]
    j = x.merge(
        fd[cols].rename(columns={"ko": "kickoff"}),
        on=["league", "season", "home", "away", "kickoff"],
        how="left",
    )
    out = {"frozen": len(x), "joined_by_kickoff": int(j["fthg"].notna().sum())}
    j = outcome(j[j["fthg"].notna()])
    chk = np.where(
        j["dog"] == "home",
        np.where(j["price"] == "pinnacle", j["ph"], j["ah"]),
        np.where(j["price"] == "pinnacle", j["pa"], j["aa"]),
    )
    out["entry_odds_differ"] = int((np.abs(chk - j["dog_odds"]) > 1e-9).sum())

    # 1. football-data alone on every frozen match
    out["fd_alone"] = {
        b: {
            "all": summary(band(j, b)),
            "by_price": by(band(j, b), "price"),
            "by_league": by(band(j, b), "league"),
            "by_season": by(band(j, b), "season"),
        }
        for b in BANDS
    }

    # 2. the teams-only result join of cashout_test.build
    reps = fd.groupby(["league", "season", "home", "away"]).size()
    reps = reps[reps > 1]
    first = (
        fd.sort_values("kickoff")
        .groupby(["league", "season", "home", "away"], as_index=False)
        .first()[["league", "season", "home", "away", "fthg", "ftag"]]
        .rename(columns={"fthg": "f_hg", "ftag": "f_ag"})
    )
    k = j.merge(first, on=["league", "season", "home", "away"], how="left")
    k["won_teams_join"] = np.where(
        k["dog"] == "home", k["f_hg"] > k["f_ag"], k["f_ag"] > k["f_hg"]
    ).astype(int)
    rep_idx = k.set_index(["league", "season", "home", "away"]).index.isin(reps.index)
    out["teams_join"] = {
        "pairs_repeated_at_same_ground": int(len(reps)),
        "by_league": {lg: int(n) for lg, n in reps.groupby(level=0).size().items()},
        "frozen_matches_in_repeated_pairs": int(rep_idx.sum()),
        "result_differs": int((k["won"] != k["won_teams_join"]).sum()),
        "result_differs_by_league": {
            lg: int(n)
            for lg, n in k[k["won"] != k["won_teams_join"]].groupby("league").size().items()
        },
        "hold_teams_join_4plus": round(
            float(
                np.where(k["won_teams_join"] == 1, k["dog_odds"] - 1, -1.0)[
                    k["dog_odds"] >= 4
                ].mean()
            ),
            4,
        ),
    }

    # 3. analysed positions against unmatched matches
    slots = json.loads(cf.slots_path("X").read_text())["matches"]
    pos, counts = ct.build(cf.slots_path("X"), cache_dir, c2.load_fd(slots, any_price=True))
    p = pd.DataFrame(pos)
    j["analysed"] = key(j).isin(set(key(p)))
    out["counts"] = counts
    pk = p.assign(won_build=p["won"])[["league", "kickoff", "home", "won_build"]]
    pj = j.merge(pk, on=["league", "kickoff", "home"], how="inner")
    out["positions_result_differs"] = int((pj["won"] != pj["won_build"]).sum())
    out["positions_result_differs_by_league"] = {
        lg: int(n) for lg, n in pj[pj["won"] != pj["won_build"]].groupby("league").size().items()
    }
    out["analysed_vs_unmatched"] = {
        b: {
            "analysed": summary(band(j[j["analysed"]], b)),
            "unmatched": summary(band(j[~j["analysed"]], b)),
            "unmatched_by_league": {
                lg: int(n) for lg, n in band(j[~j["analysed"]], b).groupby("league").size().items()
            },
            "unmatched_by_season": {
                str(y): int(n)
                for y, n in band(j[~j["analysed"]], b).groupby("season").size().items()
            },
            "unmatched_by_price": {
                pr: int(n) for pr, n in band(j[~j["analysed"]], b).groupby("price").size().items()
            },
        }
        for b in BANDS
    }

    # 4. matches left out because B or C bought them; H counted only
    bc, _ = xp.used_matches(("B", "C"))
    h, _ = xp.used_matches(("H",))
    rows = []
    for (lg, y), g in fd.groupby(["league", "season"]):
        d = xp.underdogs(g.drop(columns=["ko", "season"]))
        d["kickoff"] = d["kickoff"].dt.strftime("%Y-%m-%dT%H:%MZ")
        d["league"], d["season"] = lg, y
        rows.append(d)
    u = pd.concat(rows, ignore_index=True)
    trip = list(zip(u["league"], u["kickoff"], u["home"], strict=True))
    u["in_bc"] = [t in bc for t in trip]
    u["in_h"] = [t in h for t in trip]
    u["in_x"] = key(u).isin(set(key(x)))
    out["left_out"] = {"h_matches_not_read": int(u["in_h"].sum())}
    u = outcome(u[~u["in_h"]])
    for b in BANDS:
        ub = band(u, b)
        out["left_out"][b] = {
            "bought_by_b_or_c": summary(ub[ub["in_bc"]]),
            "frozen_sample": summary(ub[ub["in_x"]]),
            "frozen_plus_b_c": summary(ub[ub["in_x"] | ub["in_bc"]]),
            "other": int((~ub["in_x"] & ~ub["in_bc"]).sum()),
            "dog_led_at_ht_share_left_out": round(
                float(ub.loc[ub["ht_lead"] == 1, "in_bc"].mean()), 4
            ),
            "by_season_left_out": {
                str(y): int(n) for y, n in ub[ub["in_bc"]].groupby("season").size().items()
            },
        }
    return out


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--cache-dir", default="", help="the bought bodies (default raw dir)")
    args = p.parse_args(argv)
    cache = Path(args.cache_dir) if args.cache_dir else Path(RAW_DIR) / cf.CACHE
    res = run(cache)
    print("CASHOUT_CHECK_JSON " + json.dumps(res, default=str))


if __name__ == "__main__":
    main()
