"""The cash-out test for the app (lead brief, 28 Oct): builds `cashout_research.json`, a
frozen summary of the pre-registered cash-out test (design X, docs/lab.md "Cash-out test"),
in the shape the app's research studies read (`kind: "cashout"`, UI PR #134).

No key: re-runs `cashout_test.build` and `analyse` on the cached snapshot bodies
(`comeback-odds-*`) and football-data, so every number is recomputed the frozen way. `EXPECTED`
holds the published numbers of the analysis run (`RUNS["analysis"]`, the rerun after the
result-join fix), and the build fails unless they reproduce. This is a rebuild for the app,
not a second analysis.

Beside the judged band it carries descriptive designs the lead asked for (28 Oct): the
half-time cash-out with the matches designs B and C had bought put back (already read, not
judged; they have half-time snapshots only), the frozen sample at half-time only to compare
with, and 2025/26 alone, where no B or C match was left out. Design H's matches are never
joined to a result.

`odds-check.yml` `task=cashout-research` prints `CASHOUT_RESEARCH_JSON` (without the bets) and
the whole file gzipped in base64 chunks with its SHA-256; the committed file is that text.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import sys
import tempfile
from pathlib import Path

import numpy as np

from soccer_stats.data import RAW_DIR
from soccer_stats.lab import cashout_plan as xp
from soccer_stats.lab import cashout_test as ct
from soccer_stats.lab import comeback_fetch as cf
from soccer_stats.lab import comebacks2 as c2
from soccer_stats.lab.comeback_plan import load_season
from soccer_stats.lab.heavy_favourites import season_name

FILE = Path(__file__).with_name("cashout_research.json")
B64_CHUNK = 60000
S = f"{ct.PRIMARY['s']:.2f}"
START = 1000
MAX_POINTS = 200
# The analysis run's published numbers (`CASHOUT_TEST_JSON`), which the rebuild must match.
EXPECTED = {
    "counts": {
        "matches": 11864,
        "not_priced": 0,
        "unmatched": 1715,
        "no_result": 0,
        "positions": 10149,
    },
    "verdict": {
        "verdict": "fail",
        "decisions": 570,
        "growth_minus_hold": 0.000199,
        "ci95": [-0.00022, 0.00062],
    },
    "primary_positions": 5401,
    "band_3_4_positions": 4748,
}
RUNS = {
    "plan": "40646353530",
    "dry_run": "40647360245",
    "fetch": ["40647535315", "40702312973"],
    "first_analysis": "40706645115",
    "checks": "40708947532",
    "analysis": "40709968765",
}
COST = {"purchase": 150910, "snapshots_bought": 15091, "snapshots_reused": 197, "total": 150910}
MOMENT_LABELS = {"ht": "Half-time", "m70": "70th minute"}
NOTES = [
    "Entry: $1 on the underdog at its quoted pre-match close (Pinnacle's, else the market "
    "average's). The in-play price is DraftKings' moneyline, margin removed, from the odds "
    "archive's snapshots at kickoff + 55 min (half-time) and + 85 min (about the 70th "
    "minute). Only quotes updated after kickoff and at most 5 minutes old count.",
    "A cash-out pays entry odds x the in-play chance x (1 - fee). The fee stands in for the "
    "margin a book takes on a cash-out offer; 5% is the realistic case.",
    "Growth is the long-run rate a bankroll grows at when every bet risks the same share of "
    "it (2% here). It is the fair way to compare holding with cashing out, because cashing "
    "out mainly cuts the swings rather than the average.",
    "The sample left out every match designs B, C and H had bought, as pre-registered. Those "
    "were favourites trailing at half-time, so in 2024/25's main leagues most underdogs "
    "leading at half-time are missing. The left-out set depends only on the pre-match price "
    "and the half-time score, both known when the rule decides, so the cash-out against hold "
    "comparison stays fair for the matches kept; the hold level does not describe 'backing "
    "4.0+ underdogs' in general.",
    "A first run (40706645115) joined a pair that meets twice at the same ground in a season "
    "(Scotland, and the Belgian and Greek play-offs) to its first meeting's result: 90 "
    "positions. The join now matches the kickoff too, and this file is the rerun with that "
    "fix.",
    "Every by-league, by-season, fee, threshold and extra-design line is descriptive; only "
    "the primary rule was judged.",
    "The matches sit in seasons the strategy search keeps locked. This test read them for the "
    "cash-out question only; the search's holdout stays unread for pre-match strategies.",
]


def _r(x: float | None, n: int = 4) -> float | None:
    return None if x is None else round(float(x), n)


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%".replace("-", "−")


def _signed(x: float, nd: int = 1) -> str:
    return (f"{x * 100:+.{nd}f}%").replace("-", "−")


def by_group(pos: list[dict], field: str) -> dict:
    """{key: {positions, decisions, hold, cash, growth_minus_hold}} under the judged rule."""
    t, m, s = ct.PRIMARY["t"], ct.PRIMARY["m"], ct.PRIMARY["s"]
    out = {}
    for k in sorted({p[field] for p in pos}):
        x = sorted((p for p in pos if p[field] == k), key=lambda p: p["kickoff"])
        base = np.array([ct.hold(p) for p in x])
        got = [ct.rule(p, t, m) for p in x]
        prof = np.array([g[0] for g in got])
        name = f"{k}/{str(k + 1)[-2:]}" if field == "season" else str(k)
        out[name] = {
            "positions": len(x),
            "decisions": sum(g[1] is not None for g in got),
            "hold": _r(base.mean()),
            "cash": _r(prof.mean()),
            "growth_minus_hold": _r((np.log1p(s * prof) - np.log1p(s * base)).mean(), 6),
        }
    return out


def design(pos: list[dict], role: str, label: str, **extra) -> dict:
    """analyse_band's dict plus the app's fields."""
    band = ct.analyse_band(pos)
    return {
        "role": role,
        "label": label,
        **band,
        "by_league": by_group(pos, "league") if pos else {},
        "by_season": by_group(pos, "season") if pos else {},
        **extra,
    }


def halftime_only(pos: list[dict]) -> list[dict]:
    """The same positions with no 70th-minute quote, so a rule can cash out at half-time only."""
    return [{**p, "q_m70": None} for p in pos]


def curves(pos: list[dict]) -> dict:
    """Bankrolls staking 2% of themselves on each bet in kickoff order: hold and the judged
    rule, at the end of each day, thinned to MAX_POINTS (the last day always kept)."""
    s = ct.PRIMARY["s"]
    hold = cash = float(START)
    days: dict[str, dict] = {}
    for p in sorted(pos, key=lambda p: (p["kickoff"], p["league"], p["home"])):
        hold *= 1 + s * ct.hold(p)
        cash *= 1 + s * ct.rule(p, ct.PRIMARY["t"], ct.PRIMARY["m"])[0]
        day = p["kickoff"][:10]
        days[day] = {"date": day, "hold": round(hold, 2), "cash": round(cash, 2)}
    pts = list(days.values())
    if len(pts) > MAX_POINTS:
        idx = np.unique(np.linspace(0, len(pts) - 1, MAX_POINTS).round().astype(int))
        pts = [pts[i] for i in idx]
    return {"stake": s, "start": START, "points": pts}


def bet_rows(pos: list[dict]) -> list[dict]:
    """The judged rule's cash-outs in kickoff order: hold and cash are profit per $1."""
    out = []
    for p in sorted(pos, key=lambda p: (p["kickoff"], p["league"], p["home"])):
        prof, at = ct.rule(p, ct.PRIMARY["t"], ct.PRIMARY["m"])
        if at is None:
            continue
        out.append(
            {
                "date": p["kickoff"][:10],
                "lg": p["league"],
                "home": p["home"],
                "away": p["away"],
                "odds": round(p["entry"], 2),
                "won": bool(p["won"]),
                "moment": at,
                "q": _r(p[f"q_{at}"], 3),
                "hold": round(ct.hold(p), 4),
                "cash": round(prof, 4),
            }
        )
    return out


def bc_slots(x_slots: list[dict]) -> list[dict]:
    """Slots, in the frozen file's shape, for the underdogs (3.0+) of X's league-seasons that
    the sample left out because design B or C had bought them. H's matches stay out."""
    bc, _ = xp.used_matches(("B", "C"))
    h, _ = xp.used_matches(("H",))
    slots: dict = {}
    for lg, y in sorted({(s["league"], c2.season_of(s["kickoff"])) for s in x_slots}):
        d = xp.underdogs(load_season(lg, y))
        for r in d.itertuples():
            ko = r.kickoff.strftime("%Y-%m-%dT%H:%MZ")
            if (lg, ko, r.home) not in bc or (lg, ko, r.home) in h:
                continue
            slot = slots.setdefault(
                (lg, ko),
                {
                    "league": lg,
                    "season": season_name(y),
                    "sport": xp.sport_of(lg),
                    "kickoff": ko,
                    "matches": [],
                },
            )
            slot["matches"].append(
                {
                    "home": r.home,
                    "away": r.away,
                    "dog": r.dog,
                    "dog_odds": float(r.dog_odds),
                    "price": r.price,
                }
            )
    return list(slots.values())


def h_count(x_slots: list[dict], season: int) -> int:
    """Design H's matches in X's leagues in one season (counted, never joined to a result)."""
    leagues = {s["league"] for s in x_slots}
    h, _ = xp.used_matches(("H",))
    return sum(1 for lg, ko, _ in h if lg in leagues and c2.season_of(ko) == season)


def won_row(label: str, pos: list[dict]) -> dict:
    return {
        "label": label,
        "n": len(pos),
        "unit": "bets",
        "won": _r(np.mean([p["won"] for p in pos])) if pos else None,
        "ref": _r(np.mean([1 / p["entry"] for p in pos])) if pos else None,
        "hold": _r(np.mean([ct.hold(p) for p in pos])) if pos else None,
    }


def assemble(pos: list[dict], res: dict, bc_pos: list[dict], h_2025: int) -> dict:
    primary = [p for p in pos if p["entry"] >= ct.PRIMARY_DOG]
    second = [p for p in pos if p["entry"] < ct.PRIMARY_DOG]
    bc4 = [p for p in bc_pos if p["entry"] >= ct.PRIMARY_DOG]
    y2025 = [p for p in primary if p["season"] == 2025]
    v, c = res["verdict"], res["counts"]
    t, m, s = ct.PRIMARY["t"], ct.PRIMARY["m"], ct.PRIMARY["s"]
    designs = {
        "primary": design(
            primary, "judged", "Underdogs at 4.0 or longer (the judged band)", counts=c
        ),
        "band_3_4": design(second, "descriptive", "Underdogs at 3.0 to 4.0"),
        "put_back": design(
            halftime_only(primary + bc4),
            "descriptive",
            "With B and C put back (already read, half-time only)",
            put_back=len(bc4),
        ),
        "frozen_ht": design(
            halftime_only(primary),
            "descriptive",
            "The frozen sample at half-time only (to compare with the line above)",
        ),
        "season_2025": design(
            y2025,
            "descriptive",
            f"2025/26 only (no B or C match left out; design H's {h_2025} are)",
        ),
    }
    hold = designs["primary"]["variants"][0]
    rule = next(x for x in designs["primary"]["variants"] if x.get("t") == t and x.get("m") == m)
    g = rule["growth"][S]
    pb = designs["put_back"]
    pb_rule = next(x for x in pb["variants"] if x.get("t") == t and x.get("m") == m)
    pb_g = pb_rule["growth"][S]
    h25 = designs["season_2025"]["variants"][0]
    hold_bc = float(np.mean([ct.hold(p) for p in primary + bc4]))
    calib = designs["primary"]["calibration"]
    top = next((r for r in calib if r["moment"] == "ht" and r["lo"] >= 0.55), None)
    lo, hi = g["minus_hold_ci95"]
    verdict_text = (
        f"No. First, why holding looks so bad: holding these bets lost {_pct(-hold['mean'])} "
        "of every dollar, but the sample left out the matches earlier tests had already "
        "bought, and those were mostly underdogs leading at half-time. Put them back and "
        f"holding loses {_pct(-hold_bc)}; in 2025/26, where no B or C match was left out, "
        f"{_pct(-h25['mean'])}. Now the test: cashing out once the underdog's in-play chance "
        f"reached {t:.0%}, paying a {m:.0%} fee, beat holding by "
        f"{_signed(g['minus_hold'], 3)} bankroll growth a bet at {s:.0%} stakes (range "
        f"{_signed(lo, 3)} to {_signed(hi, 3)}) on {v['decisions']} cash-outs: not "
        "distinguishable from zero. With the left-out matches back (half-time only, already "
        f"read, not judged) it was {_signed(pb_g['minus_hold'], 3)} on "
        f"{pb_rule['decisions']} cash-outs. A free cash-out helps a little, because the "
        "in-play price slightly overrates a leading underdog; a realistic fee eats that."
    )
    why = [
        {
            "title": "Why holding lost so much",
            "text": (
                "The sample left out every match designs B, C and H had bought, as "
                "pre-registered, so no match was bought twice. B and C's matches were "
                "favourites trailing at half-time: underdogs leading at half-time. In "
                "2024/25's eight main leagues that took out most of them, and they won about "
                "half the time, so the hold line here is lower than backing every long shot "
                "would be. Because the left-out set depends only on the pre-match price and "
                "the half-time score, the cash-out against hold comparison is still fair for "
                "the matches kept. The rows compare how often the underdog won with the "
                "pre-match price's chance (with its margin)."
            ),
            "ref_name": "Pre-match price",
            "rows": [
                won_row("The sample, 4.0+", primary),
                won_row("Left out because B or C bought them", bc4),
                won_row("The sample with them put back", primary + bc4),
                won_row("2025/26 only (no B or C match left out)", y2025),
            ],
        },
        {
            "title": "Is the in-play price right?",
            "text": (
                "Grouped by DraftKings' in-play chance at the moment the rule looked. Where "
                "the underdog was well placed (55% or more at half-time) it won less often "
                "than the price said"
                + (f" ({_pct(top['won'])} vs {_pct(top['implied'])})" if top else "")
                + ", so selling the bet at that price was a small gain before any fee."
            ),
            "ref_name": "In-play price",
            "rows": [
                {
                    "label": f"{MOMENT_LABELS[r['moment']]}, "
                    + (f"{r['lo']:.0%}+" if r["hi"] >= 1 else f"{r['lo']:.0%}–{r['hi']:.0%}"),
                    "n": r["n"],
                    "unit": "bets",
                    "won": r["won"],
                    "ref": r["implied"],
                }
                for r in calib
            ],
        },
        {
            "title": "What the fee does",
            "text": (
                "A cash-out at a fair price leaves the average return where it was and only "
                "cuts the swings. The gain here came from the price overrating the leading "
                "underdog by a few points; a 5% fee costs about as much, and at 10% cashing "
                "out loses. Nothing changes live."
            ),
            "rows": [],
        },
    ]
    return {
        "kind": "cashout",
        "title": "Cashing out a winning underdog",
        "question": (
            "When a long-shot underdog bet is going well, does cashing it out at the in-play "
            "price beat holding it to the end?"
        ),
        "rule": (
            f"$1 on an underdog at {ct.PRIMARY_DOG:.1f} or longer at its pre-match close. Cash "
            f"out at half-time, or else at the 70th minute, once DraftKings' in-play chance "
            f"for it reaches {t:.0%}, paying a {m:.0%} cash-out fee. Judged on long-run "
            f"growth at {s:.0%} stakes against holding, on at least {ct.MIN_DECISIONS} "
            "cash-outs."
        ),
        "verdict": v["verdict"],
        "verdict_text": verdict_text,
        "test_verdict": v,
        "primary_rule": {"t": t, "m": m, "s": s},
        "closed_at": "2026-10-28",
        "preregistered_at": "2026-10-28",
        "preregistered": {"X": "26ae427"},
        "cost": COST,
        "runs": RUNS,
        "designs": designs,
        "curves": curves(primary),
        "bets": bet_rows(primary),
        "why": why,
        "notes": NOTES,
    }


def check(res: dict) -> list[str]:
    """Differences from the analysis run (empty = reproduced exactly)."""
    bad = []
    for k, v in EXPECTED["counts"].items():
        if res["counts"][k] != v:
            bad.append(f"counts {k}: {res['counts'][k]} != {v}")
    for k, v in EXPECTED["verdict"].items():
        if res["verdict"][k] != v:
            bad.append(f"verdict {k}: {res['verdict'][k]} != {v}")
    for band in ("primary", "band_3_4"):
        want = EXPECTED[f"{band}_positions"]
        if res[band]["positions"] != want:
            bad.append(f"{band} positions: {res[band]['positions']} != {want}")
    return bad


def canonical(out: dict) -> str:
    """The committed text: indented like comeback_research.json, each bet on one line."""
    head = json.dumps({**out, "bets": "@BETS@"}, indent=1)
    rows = ",\n".join("  " + json.dumps(b, separators=(",", ":")) for b in out["bets"])
    return head.replace('"@BETS@"', "[\n" + rows + "\n ]") + "\n"


def build(cache_dir: Path | None = None) -> tuple[dict, list[str]]:
    cache_dir = Path(cache_dir) if cache_dir else Path(RAW_DIR) / cf.CACHE
    path = cf.slots_path("X")
    x_slots = json.loads(path.read_text())["matches"]
    fd = c2.load_fd(x_slots, any_price=True)
    pos, counts = ct.build(path, cache_dir, fd)
    res = ct.analyse(pos, counts)
    with tempfile.TemporaryDirectory() as tmp:
        extra = Path(tmp) / "bc.json"
        extra.write_text(json.dumps({"matches": bc_slots(x_slots)}))
        bc_pos, _ = ct.build(extra, cache_dir, fd)
    out = assemble(pos, res, bc_pos, h_count(x_slots, 2025))
    bad = check(res)
    out["reproduces"] = not bad
    return out, bad


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--cache-dir", default="", help="the fetch's bodies (default raw dir)")
    p.add_argument("--json", default="")
    args = p.parse_args(argv)
    out, bad = build(Path(args.cache_dir) if args.cache_dir else None)
    tv = out["test_verdict"]
    print(
        f"  judged: {tv['decisions']} decisions, growth difference {tv['growth_minus_hold']} "
        f"{tv['ci95']}; cash-outs listed: {len(out['bets'])}"
    )
    for k, d in out["designs"].items():
        print(f"  {k}: {d['label']}: {d['positions']} positions")
    print(f"Reproduces run {RUNS['analysis']}: " + ("yes" if not bad else "NO"))
    for line in bad:
        print("  " + line)
    summary = {k: v for k, v in out.items() if k != "bets"}
    print("CASHOUT_RESEARCH_JSON " + json.dumps(summary, separators=(",", ":")))
    text = canonical(out)
    print(
        f"CASHOUT_RESEARCH_SHA256 {hashlib.sha256(text.encode()).hexdigest()} ({len(text)} bytes)"
    )
    blob = base64.b64encode(gzip.compress(text.encode(), mtime=0)).decode()
    parts = [blob[i : i + B64_CHUNK] for i in range(0, len(blob), B64_CHUNK)]
    for i, part in enumerate(parts):
        print(f"CASHOUT_RESEARCH_B64 {i + 1}/{len(parts)} {part}")
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(text)
    if bad:
        sys.exit(1)


if __name__ == "__main__":
    main()
