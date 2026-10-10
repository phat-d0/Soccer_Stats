"""Team-corner recalibration, round 12 (docs/totals.md, "Team-corner recalibration").

This first part is the coverage check only: how many 2025/26 matches in each league have
home and away corner counts in football-data's file. Counts only, no outcomes are read.
"""

from __future__ import annotations

import pandas as pd

from soccer_stats.edge.corners import LEAGUES

TEST_SEASON = 2025  # 2025/26


def coverage_counts(raw: pd.DataFrame) -> dict:
    """Played matches and how many carry both corner counts (HC and AC)."""
    played = raw.dropna(subset=["HomeTeam", "AwayTeam", "FTHG", "FTAG"])
    has = pd.Series(True, index=played.index)
    for col in ("HC", "AC"):
        has &= pd.to_numeric(played[col], errors="coerce").notna() if col in played else False
    n = int(len(played))
    k = int(has.sum())
    return {"played": n, "with_corners": k, "share": round(k / n, 4) if n else None}


def coverage(leagues=LEAGUES, season: int = TEST_SEASON) -> dict:
    from soccer_stats.data import download

    out = {}
    for lg in leagues:
        raw = pd.read_csv(download(lg, season), encoding="latin-1", on_bad_lines="skip")
        out[lg] = coverage_counts(raw)
    return out


def main(argv: list[str] | None = None) -> None:
    import argparse
    import json

    ap = argparse.ArgumentParser(prog="python -m soccer_stats.edge.corner_recal")
    ap.add_argument("--coverage-only", action="store_true")
    args = ap.parse_args(argv)
    if args.coverage_only:
        res = coverage()
        for lg, r in res.items():
            n, k = r["played"], r["with_corners"]
            print(f"{lg} 2025/26: {n} played, {k} with HC and AC ({r['share']})")
        print("COVERAGE_JSON " + json.dumps(res))


if __name__ == "__main__":
    main()
