"""The cash-out test for the app (lead brief, 28 Oct): builds `cashout_research.json`, a
frozen summary of the pre-registered cash-out test (design X, docs/lab.md "Cash-out test") in
the shape of `comeback_research.json`, which the Portfolio view reads.

No key: re-runs `cashout_test.build` and `analyse` on the cached snapshot bodies
(`comeback-odds-*`) and football-data, so every number is recomputed the frozen way. `EXPECTED`
holds the published numbers of run 40706645115, and the build fails unless they reproduce.
This is a rebuild for the app, not a second analysis. `odds-check.yml`
`task=cashout-research` prints `CASHOUT_RESEARCH_JSON` (without the bets) and the whole file
gzipped in base64 chunks with its SHA-256; the committed file is that text.
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
# Run 40706645115's published numbers (`CASHOUT_TEST_JSON`), which the rebuild must match.
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
        "growth_minus_hold": 0.000312,
        "ci95": [-0.00011, 0.00073],
    },
    "primary_positions": 5401,
    "band_3_4_positions": 4748,
}
LABELS = {
    "primary": "Underdogs at 4.0 or longer (the judged band)",
    "band_3_4": "Underdogs at 3.0 to 4.0",
}
ROLES = {"primary": "judged", "band_3_4": "descriptive"}
RUNS = {
    "plan": "40646353530",
    "dry_run": "40647360245",
    "fetch": ["40647535315", "40702312973"],
    "analysis": "40706645115",
}
COST = {"purchase": 150910, "snapshots_bought": 15091, "snapshots_reused": 197, "total": 150910}
MOMENT_LABELS = {"ht": "half-time", "m70": "70th minute"}
NOTES = [
    "Entry: $1 on the underdog at its quoted pre-match close (Pinnacle's, else the market "
    "average's). The in-play price is DraftKings' moneyline, margin removed, from the odds "
    "archive's snapshots at kickoff + 55 min (half-time) and + 85 min (about the 70th "
    "minute). Only quotes updated after kickoff and at most 5 minutes old count.",
    "A cash-out pays entry odds x the in-play chance x (1 - fee). The fee stands in for the "
    "margin a book takes on a cash-out offer; 5% is the realistic case.",
    "Growth is the long-run rate a bankroll grows at when every bet risks the same share of "
    "it (2% here). It is the fair way to compare holding with cashing out, because cashing out "
    "mainly cuts the swings rather than the average.",
    "The in-play price overrated leading underdogs a little (55%+ at half-time: 63% won vs "
    "72% implied), so a free cash-out helped. A 5% fee ate that edge.",
    "Every by-league, by-season, fee and threshold line is descriptive; only the primary rule "
    "was judged.",
    "The matches sit in seasons the strategy search keeps locked. This test read them for the "
    "cash-out question only; the search's holdout stays unread for pre-match strategies.",
]


def _r(x: float | None, n: int = 4) -> float | None:
    return None if x is None else round(float(x), n)


def rule_rows(band: dict) -> list[dict]:
    """The hold vs cash-out table by threshold and fee (and the Kelly partial), at 2%."""
    out = []
    for v in band["variants"]:
        g = v["growth"][S]
        kind = "hold" if v["name"] == "hold" else ("kelly" if "kelly" in v["name"] else "cash")
        if kind == "kelly" and v.get("s") != ct.PRIMARY["s"]:
            continue
        out.append(
            {
                "kind": kind,
                "t": v.get("t"),
                "m": v.get("m"),
                "primary": kind == "cash"
                and v.get("t") == ct.PRIMARY["t"]
                and v.get("m") == ct.PRIMARY["m"],
                "decisions": v["decisions"],
                "at_ht": v.get("at_ht"),
                "at_m70": v.get("at_m70"),
                "mean": v["mean"],
                "mean_ci95": v["ci95"],
                "sd": v["sd"],
                "growth": g["mean"],
                "growth_minus_hold": None if kind == "hold" else g["minus_hold"],
                "growth_minus_hold_ci95": g["minus_hold_ci95"],
                "drawdown": {k: x["drawdown"] for k, x in v["growth"].items()},
            }
        )
    return out


def stake_rows(band: dict) -> list[dict]:
    """Hold and the primary rule at each stake: growth, its difference and drawdowns."""
    hold = band["variants"][0]
    prim = next(
        v
        for v in band["variants"]
        if v.get("t") == ct.PRIMARY["t"] and v.get("m") == ct.PRIMARY["m"]
    )
    return [
        {
            "stake": float(s),
            "hold_growth": hold["growth"][s]["mean"],
            "cash_growth": prim["growth"][s]["mean"],
            "growth_minus_hold": prim["growth"][s]["minus_hold"],
            "growth_minus_hold_ci95": prim["growth"][s]["minus_hold_ci95"],
            "hold_drawdown": hold["growth"][s]["drawdown"],
            "cash_drawdown": prim["growth"][s]["drawdown"],
        }
        for s in hold["growth"]
    ]


def group(pos: list[dict]) -> dict:
    """One table line: hold and the primary rule per $1 and their growth difference at 2%."""
    pos = sorted(pos, key=lambda p: p["kickoff"])
    base = np.array([ct.hold(p) for p in pos])
    got = [ct.rule(p, ct.PRIMARY["t"], ct.PRIMARY["m"]) for p in pos]
    prof = np.array([g[0] for g in got])
    s = ct.PRIMARY["s"]
    diff = np.log1p(s * prof) - np.log1p(s * base)
    return {
        "positions": len(pos),
        "won": _r(np.mean([p["won"] for p in pos])),
        "decisions": sum(g[1] is not None for g in got),
        "hold": _r(base.mean()),
        "hold_ci95": ct._boot(base),
        "cash": _r(prof.mean()),
        "growth_minus_hold": _r(diff.mean(), 6),
        "growth_minus_hold_ci95": ct._boot(diff),
    }


def design_block(key: str, pos: list[dict], band: dict) -> dict:
    hold = band["variants"][0]
    return {
        "role": ROLES[key],
        "label": LABELS[key],
        "positions": band["positions"],
        "won": band["won"],
        "avg_entry": band["avg_entry"],
        "quoted_ht": band["quoted_ht"],
        "quoted_m70": band["quoted_m70"],
        "hold": {
            "mean": hold["mean"],
            "ci95": hold["ci95"],
            "sd": hold["sd"],
            "growth": hold["growth"][S]["mean"],
            "drawdown": {k: x["drawdown"] for k, x in hold["growth"].items()},
        },
        "rules": rule_rows(band),
        "by_stake": stake_rows(band),
        "by_band": [{**r, "label": MOMENT_LABELS[r["moment"]]} for r in band["calibration"]],
        "by_league": [
            {"code": lg, **group([p for p in pos if p["league"] == lg])}
            for lg in sorted({p["league"] for p in pos})
        ],
        "by_season": [
            {"season": f"{y}/{str(y + 1)[-2:]}", **group([p for p in pos if p["season"] == y])}
            for y in sorted({p["season"] for p in pos})
        ],
    }


def bet_rows(pos: list[dict]) -> list[dict]:
    """The judged band's positions in kickoff order, compact, for the profit curve: `cash`
    is the primary rule's profit per $1 and `at` the moment it cashed out (None = held)."""
    out = []
    for p in sorted(pos, key=lambda p: (p["kickoff"], p["league"], p["home"])):
        prof, at = ct.rule(p, ct.PRIMARY["t"], ct.PRIMARY["m"])
        out.append(
            {
                "date": p["kickoff"][:10],
                "lg": p["league"],
                "odds": round(p["entry"], 2),
                "won": bool(p["won"]),
                "q_ht": _r(p["q_ht"], 3),
                "q_m70": _r(p["q_m70"], 3),
                "at": at,
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


def halftime_only(pos: list[dict]) -> dict:
    """Hold vs cash out at half-time only (T 0.50, each fee), growth at 2%."""
    pos = sorted(pos, key=lambda p: p["kickoff"])
    if not pos:
        return {"positions": 0}
    base = np.array([ct.hold(p) for p in pos])
    s = ct.PRIMARY["s"]
    out = {
        "positions": len(pos),
        "won": _r(np.mean([p["won"] for p in pos])),
        "led_cases": sum(p["q_ht"] is not None and p["q_ht"] >= ct.PRIMARY["t"] for p in pos),
        "hold": _r(base.mean()),
        "hold_ci95": ct._boot(base),
        "rules": [],
    }
    for m in ct.MARGINS:
        got = [ct.rule({**p, "q_m70": None}, ct.PRIMARY["t"], m) for p in pos]
        prof = np.array([g[0] for g in got])
        diff = np.log1p(s * prof) - np.log1p(s * base)
        out["rules"].append(
            {
                "m": m,
                "decisions": sum(g[1] is not None for g in got),
                "cash": _r(prof.mean()),
                "growth_minus_hold": _r(diff.mean(), 6),
                "growth_minus_hold_ci95": ct._boot(diff),
            }
        )
    return out


def put_back(pos: list[dict], cache_dir: Path, fd) -> dict:
    """Descriptive, already read and not judged (lead, 28 Oct): the half-time cash-out with
    the matches B and C had bought put back. They have half-time snapshots only."""
    x_slots = json.loads(cf.slots_path("X").read_text())["matches"]
    extra = bc_slots(x_slots)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "bc.json"
        path.write_text(json.dumps({"matches": extra}))
        bc_pos, counts = ct.build(path, cache_dir, fd)
    out = {
        "label": "Already read, not judged",
        "note": (
            "The sample left out every match designs B and C had already bought: favourites "
            "trailing at half-time, so underdogs leading at half-time. Put back here at "
            "half-time only (they have no 70th-minute snapshot), with the same frozen code."
        ),
        "matches_put_back": sum(len(s["matches"]) for s in extra),
        "counts": counts,
    }
    for band, keep in (
        ("primary", lambda p: p["entry"] >= ct.PRIMARY_DOG),
        ("band_3_4", lambda p: p["entry"] < ct.PRIMARY_DOG),
    ):
        mine = [p for p in pos if keep(p)]
        theirs = [p for p in bc_pos if keep(p)]
        out[band] = {
            "frozen": halftime_only(mine),
            "put_back_only": halftime_only(theirs),
            "with_put_back": halftime_only(mine + theirs),
        }
    return out


def check(res: dict) -> list[str]:
    """Differences from run 40706645115 (empty = reproduced exactly)."""
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


def assemble(pos: list[dict], res: dict, extra: dict | None = None) -> dict:
    primary = [p for p in pos if p["entry"] >= ct.PRIMARY_DOG]
    second = [p for p in pos if p["entry"] < ct.PRIMARY_DOG]
    v, c = res["verdict"], res["counts"]
    hold = res["primary"]["variants"][0]
    return {
        "title": "Cashing out a winning underdog",
        "question": (
            "When a long-shot underdog bet is going well, does cashing it out at the in-play "
            "price beat holding it to the end?"
        ),
        "rule": (
            "$1 on an underdog at 4.0 or longer at its pre-match close. Cash out at half-time, "
            "or else at the 70th minute, once DraftKings' in-play chance for it reaches 50%, "
            "paying a 5% cash-out fee. Judged on long-run growth at 2% stakes against holding."
        ),
        "verdict": v["verdict"],
        "verdict_text": (
            f"No. Held to the end, these bets lost {-hold['mean']:.0%} of every dollar, so at "
            f"2% stakes the bankroll shrank about {-hold['growth'][S]['mean']:.2%} a bet. "
            f"Cashing out once the underdog reached a 50% chance, paying a 5% fee, shrank it "
            f"{-(hold['growth'][S]['mean'] + v['growth_minus_hold']):.2%} a bet: better by "
            f"{v['growth_minus_hold']:.3%} (range {v['ci95'][0]:+.3%} to {v['ci95'][1]:+.3%}) "
            f"on {v['decisions']} cash-outs, which can't be told apart from zero. A free "
            "cash-out helped a little, because the in-play price slightly overrates a leading "
            "underdog, but a realistic fee eats that. The real problem is the bet itself."
        ),
        "closed_at": "2026-10-28",
        "cost": COST,
        "runs": RUNS,
        "preregistered": {"x": "26ae427", "analysis": "e4db2ef"},
        "primary": {
            "t": ct.PRIMARY["t"],
            "m": ct.PRIMARY["m"],
            "s": ct.PRIMARY["s"],
            "dog": ct.PRIMARY_DOG,
            "min_decisions": ct.MIN_DECISIONS,
            "decisions": v["decisions"],
            "growth_minus_hold": v["growth_minus_hold"],
            "ci95": v["ci95"],
        },
        "counts": c,
        "designs": {
            "primary": design_block("primary", primary, res["primary"]),
            "band_3_4": design_block("band_3_4", second, res["band_3_4"]),
        },
        "bets": bet_rows(primary),
        "bets_note": (
            "Every judged position (underdog 4.0+) in kickoff order. Hold profit per $1 is "
            "odds - 1 when won, else -1; `cash` is the primary rule's, `at` when it cashed out."
        ),
        "put_back": extra,
        "notes": NOTES,
    }


def canonical(out: dict) -> str:
    """The committed text: indented like comeback_research.json, each bet on one line."""
    head = json.dumps({**out, "bets": "@BETS@"}, indent=1)
    rows = ",\n".join("  " + json.dumps(b, separators=(",", ":")) for b in out["bets"])
    return head.replace('"@BETS@"', "[\n" + rows + "\n ]") + "\n"


def build(cache_dir: Path | None = None) -> tuple[dict, list[str]]:
    cache_dir = Path(cache_dir) if cache_dir else Path(RAW_DIR) / cf.CACHE
    path = cf.slots_path("X")
    fd = c2.load_fd(json.loads(path.read_text())["matches"], any_price=True)
    pos, counts = ct.build(path, cache_dir, fd)
    res = ct.analyse(pos, counts)
    out = assemble(pos, res, put_back(pos, cache_dir, fd))
    bad = check(res)
    out["reproduces"] = not bad
    return out, bad


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--cache-dir", default="", help="the fetch's bodies (default raw dir)")
    p.add_argument("--json", default="")
    args = p.parse_args(argv)
    out, bad = build(Path(args.cache_dir) if args.cache_dir else None)
    pr = out["primary"]
    print(
        f"  primary: {pr['decisions']} decisions, growth difference {pr['growth_minus_hold']} "
        f"{pr['ci95']}; bets listed: {len(out['bets'])}"
    )
    print("Reproduces run 40706645115: " + ("yes" if not bad else "NO"))
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
