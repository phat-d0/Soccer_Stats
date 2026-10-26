"""The comeback research for the app (lead brief, 26 Oct): builds `comeback_research.json`,
a frozen summary of the pre-registered phase 2 tests (design C judged, B and C+B descriptive)
and the free studies (phase 1, ESPN goal timelines), in the shape the Portfolio view reads.

No key: re-runs `comebacks2.match_rows` on the cached snapshot bodies (`comeback-odds-*`) and
football-data, so every design number is recomputed the frozen way; `EXPECTED` holds the
published numbers of runs 40176653428 (C) and 40145295663 (B), and the build reports whether
they reproduce. Phase 1 and the timelines are copied verbatim from docs/lab.md (their runs
are named). `odds-check.yml` `task=comeback-research` prints `COMEBACK_RESEARCH_JSON`; the
committed file is that line, unchanged.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from soccer_stats.data import RAW_DIR
from soccer_stats.lab import comeback_fetch as cf
from soccer_stats.lab import comebacks as cb
from soccer_stats.lab import comebacks2 as c2

FILE = Path(__file__).with_name("comeback_research.json")
# The published numbers each design must reproduce (bets, wins, ROI and its range).
EXPECTED = {
    "C": {"bets": 732, "wins": 123, "roi": -0.25071, "roi_ci95": [-0.38318, -0.10207]},
    "B": {"bets": 198, "wins": 55, "roi": 0.07273, "roi_ci95": [-0.19162, 0.34343]},
    "pooled": {"bets": 930, "wins": 178, "roi": -0.18185, "roi_ci95": [-0.29962, -0.05086]},
}
LABELS = {
    "C": "Design C: eight leagues, 2022/23 to 2024/25 (without B's matches)",
    "B": "Design B: top five leagues, 2024/25",
    "pooled": "C and B together",
}
ROLES = {"C": "judged", "B": "descriptive", "pooled": "descriptive"}
RUNS = {
    "fetch_b": "40144988040",
    "analysis_b": "40145295663",
    "fetch_c": "40176031079",
    "analysis_c": "40176653428",
    "phase1": "40082512216",
    "timelines": ["40176002361", "40184239564"],
    "probes": ["40136522857", "40136975819", "40204253352"],
}
COST = {"design_b": 1920, "design_c": 6070, "probes": 21, "total": 8011}
PHASE1 = {
    "ht_behind": {"matches": 2166, "won": 0.197, "model": 0.140},
    "conceded_first": {"matches": 2174, "won": 0.288, "model": 0.245},
    "tests_above_zero": 5,
    "tests": 18,
    "note": (
        "Free data, 2017/18 to 2024/25. Favourites who fall behind won more often than a simple "
        "model that ignores the score and spreads goals evenly over 90 minutes. About a third "
        "of the half-time gap is just that second halves have more goals. This compared "
        "against a model, not against any price."
    ),
}
# docs/lab.md "Goal timelines: results" (run 40184239564): won and model on the benchmarked
# matches (Pinnacle's close and over/under in a season with >= 90% coverage), so won - model
# is the gap.
TIMELINES = {
    "matches": 15838,
    "joined_share": 0.983,
    "trail_first": 4980,
    "trail_first_share": 0.3144,
    "benchmarked": 3789,
    "won": 0.2615,
    "model": 0.2209,
    "gap": 0.0406,
    "gap_ci95": [0.0282, 0.0542],
    "by_minute": [
        {
            "lo": 0,
            "hi": 15,
            "label": "0-15",
            "matches": 1709,
            "benchmarked": 1345,
            "won": 0.342,
            "model": 0.3078,
            "gap": 0.0342,
            "gap_ci95": [0.0099, 0.0585],
        },
        {
            "lo": 15,
            "hi": 30,
            "label": "15-30",
            "matches": 1215,
            "benchmarked": 907,
            "won": 0.2988,
            "model": 0.2502,
            "gap": 0.0485,
            "gap_ci95": [0.0185, 0.076],
        },
        {
            "lo": 30,
            "hi": 45,
            "label": "30-45",
            "matches": 923,
            "benchmarked": 678,
            "won": 0.2522,
            "model": 0.1912,
            "gap": 0.061,
            "gap_ci95": [0.0286, 0.0922],
        },
        {
            "lo": 45,
            "hi": 60,
            "label": "45-60",
            "matches": 545,
            "benchmarked": 402,
            "won": 0.1617,
            "model": 0.124,
            "gap": 0.0377,
            "gap_ci95": [0.0043, 0.0738],
        },
        {
            "lo": 60,
            "hi": 75,
            "label": "60-75",
            "matches": 334,
            "benchmarked": 258,
            "won": 0.0891,
            "model": 0.0566,
            "gap": 0.0326,
            "gap_ci95": [-0.001, 0.0678],
        },
        {
            "lo": 75,
            "hi": 90,
            "label": "75+",
            "matches": 254,
            "benchmarked": 199,
            "won": 0.005,
            "model": 0.0095,
            "gap": -0.0045,
            "gap_ci95": [-0.0107, 0.0067],
        },
    ],
    "priced_test_cost": 18820,
    "note": (
        "Free ESPN goal timelines, eight leagues, 2017/18 to 2024/25. When the underdog scores "
        "first, the favourite beats the score-blind model by a similar 3 to 6 points whatever "
        "the minute, so no minute stands out. Testing it at real prices would need about "
        "1,882 snapshots (18,820 credits); not bought."
    ),
}
NOTES = [
    "The price is DraftKings' in-play moneyline on the favourite, taken from the odds "
    "archive's snapshot about 55 minutes after kickoff, inside the half-time break. Only "
    "quotes updated after kickoff and at most 5 minutes old count.",
    "Phase 1 found favourites come back more often than a simple model says. The market "
    "already knows: at half-time it priced the comeback at 21.3% while 17.0% happened (design "
    "C), so backing the trailing favourite lost about a quarter of the stake.",
    "Design B's positive season (+7.3%) was not a pattern. C, on prices never seen before, "
    "lost 25.1%, and every by-league, by-season and by-band line here is descriptive.",
    "A new idea from these numbers, laying the trailing favourite included, needs its own "
    "pre-registration on seasons not used here (2025/26 or later) and the owner's approval "
    "before any credits are spent.",
]


def _band(lo: float, hi: float) -> str:
    return cb.band_name(lo, hi)


def group(rows: list[dict]) -> dict:
    """One table line: the primary's bets and ROI, and the market's mean fair chance."""
    p = c2.roi(rows, "dk_odds")
    g = c2.implied_gap(rows)
    return {
        "bets": p["matches"],
        "wins": p["wins"],
        "win_rate": p.get("win_rate"),
        "implied": g.get("implied"),
        "roi": p.get("roi"),
        "roi_ci95": p.get("roi_ci95"),
    }


def design_block(key: str, slots: list[dict], rows: list[dict], counts: dict) -> dict:
    p = c2.roi(rows, "dk_odds")
    bf = c2.roi(rows, "bf_odds", c2.COMMISSION)
    gap = c2.implied_gap(rows)
    blend = c2.blend_weight(rows)
    leagues = [lg for lg in cb.LEAGUES_B if any(s["league"] == lg for s in slots)]
    seasons = sorted({c2.season_of(s["kickoff"]) for s in slots})
    return {
        "role": ROLES[key],
        "label": LABELS[key],
        "leagues": leagues,
        "seasons": [f"{y}/{str(y + 1)[-2:]}" for y in seasons],
        "slots": len(slots),
        "matches": sum(len(s["matches"]) for s in slots),
        "not_priced": counts["matches_not_priced"],
        "unmatched": counts["unmatched"],
        "rows": len(rows),
        "bets": p["matches"],
        "wins": p["wins"],
        "win_rate": p.get("win_rate"),
        "implied": gap.get("implied"),
        "implied_matches": gap["matches"],
        "gap": gap.get("gap"),
        "gap_ci95": gap.get("gap_ci95"),
        "roi": p.get("roi"),
        "roi_ci95": p.get("roi_ci95"),
        "betfair_bets": bf["matches"],
        "betfair_roi": bf.get("roi"),
        "betfair_roi_ci95": bf.get("roi_ci95"),
        "blend_c": blend.get("c"),
        "blend_c_ci95": blend.get("c_ci95"),
        "avg_odds": p.get("avg_odds"),
        "by_league": [
            {"code": lg, **group([r for r in rows if r["league"] == lg])}
            for lg in cb.LEAGUES_B
            if any(r["league"] == lg for r in rows)
        ],
        "by_season": [
            {"season": f"{y}/{str(y + 1)[-2:]}", **group([r for r in rows if r["season"] == y])}
            for y in sorted({r["season"] for r in rows})
        ],
        "by_band": [
            {"lo": lo, "hi": min(hi, 1.0), **group([r for r in rows if r["band"] == _band(lo, hi)])}
            for lo, hi in cb.BANDS
        ],
    }


def bet_rows(rows: list[dict], design: str) -> list[dict]:
    """The DraftKings bets, compact: one row per trailing favourite with a passing quote."""
    out = []
    for r in rows:
        if not r["dk_odds"]:
            continue
        out.append(
            {
                "date": r["kickoff"][:10],
                "kickoff": r["kickoff"],
                "lg": r["league"],
                "home": r["home"],
                "away": r["away"],
                "fav": r["favourite"],
                "ht": f"{r['ht'][0]}-{r['ht'][1]}",
                "odds": r["dk_odds"],
                "implied": None if r["fair"] is None else round(r["fair"], 4),
                "won": bool(r["won"]),
                "design": design,
            }
        )
    return out


def check(designs: dict) -> list[str]:
    """Differences from the published runs (empty = reproduced exactly)."""
    bad = []
    for key, want in EXPECTED.items():
        got = designs[key]
        for field, value in want.items():
            if got[field] != value:
                bad.append(f"{key} {field}: {got[field]} != {value}")
    return bad


def build(cache_dir: Path | None = None) -> dict:
    cache_dir = Path(cache_dir) if cache_dir else Path(RAW_DIR) / cf.CACHE
    c_slots = cf.load_slots(cf.slots_path("C"))
    b_slots = cf.load_slots(cf.slots_path("B"))
    fd = c2.load_fd(c_slots + b_slots)
    c_rows, c_counts = c2.match_rows(c_slots, cache_dir, fd)
    b_rows, b_counts = c2.match_rows(b_slots, cache_dir, fd)
    pooled_counts = {k: c_counts[k] + b_counts[k] for k in ("matches_not_priced", "unmatched")}
    designs = {
        "C": design_block("C", c_slots, c_rows, c_counts),
        "B": design_block("B", b_slots, b_rows, b_counts),
        "pooled": design_block("pooled", c_slots + b_slots, c_rows + b_rows, pooled_counts),
    }
    bets = sorted(
        bet_rows(c_rows, "C") + bet_rows(b_rows, "B"),
        key=lambda b: (b["kickoff"], b["lg"], b["home"]),
    )
    return {
        "title": "Favourites coming back from behind",
        "question": (
            "When the favourite is losing at half-time, does backing it at the in-play price "
            "make money?"
        ),
        "rule": (
            "$1 on the favourite trailing at half-time, at DraftKings' in-play price from the "
            "snapshot at kickoff + 55 min."
        ),
        "verdict": "fail",
        "verdict_text": (
            "No. On 732 bets the in-play price paid out less than the stakes: ROI -25.1% "
            "(-38.3% to -10.2%). The market already rates these comebacks higher than they "
            "happen."
        ),
        "closed_at": "2026-10-25",
        "cost": COST,
        "runs": RUNS,
        "preregistered": {"b": "e4b3f8b", "c": "dca429e"},
        "designs": designs,
        "bets": bets,
        "phase1": PHASE1,
        "timelines": TIMELINES,
        "notes": NOTES,
        "reproduces": check(designs) == [],
    }


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--cache-dir", default="", help="the fetch's bodies (default raw dir)")
    p.add_argument("--json", default="")
    args = p.parse_args(argv)
    out = build(Path(args.cache_dir) if args.cache_dir else None)
    bad = check(out["designs"])
    for key, d in out["designs"].items():
        print(
            f"  {key}: {d['bets']} bets, won {d['wins']}, ROI {d['roi']:+.2%}, "
            f"implied {d['implied']}, gap {d['gap']}, c {d['blend_c']}"
        )
    print(f"  bets listed: {len(out['bets'])}")
    print("Reproduces runs 40176653428 and 40145295663: " + ("yes" if not bad else "NO"))
    for line in bad:
        print("  " + line)
    print("COMEBACK_RESEARCH_JSON " + json.dumps(out, separators=(",", ":")))
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(out, indent=1))
    if bad:
        sys.exit(1)


if __name__ == "__main__":
    main()
