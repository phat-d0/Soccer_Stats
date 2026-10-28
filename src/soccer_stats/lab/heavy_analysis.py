"""The 75%+ favourites' judged analysis, H ∪ H2 (docs/lab.md "Design H2: final
pre-registration"; written, tested and merged before its one run). No key.

$1 on the favourite priced 75%+ before kickoff that trails at half-time, at DraftKings'
passing in-play price from the snapshot at kickoff + 55 min. H's rows come from
`comebacks2.match_rows` unchanged (football-data results). H2's rows use the same quote checks
on its own snapshots, with:
- the in-play event matched by both names (`prematch_coverage.same_team`, the slot's names,
  else ESPN's) and the kickoff ±30 min, either way round;
- results from 90 minutes only: ESPN's 90-minute score for the cups and UEFA (by `espn_id`, from
  `heavy_more`'s cached ESPN days), football-data's extra file (`HG`, `AG`) for the extra
  leagues (teams and the date within a day of the kickoff).
Pass: at least `MIN_BETS` 136 usable DraftKings bets and the ROI range per $1 entirely above 0.
Under 136 is "inconclusive". The per-sample lines are descriptive only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from soccer_stats import cups
from soccer_stats.data import RAW_DIR
from soccer_stats.lab import comeback_fetch as cf
from soccer_stats.lab import comebacks as cb
from soccer_stats.lab import comebacks2 as c2
from soccer_stats.lab import heavy_more as hm
from soccer_stats.lab import prematch_coverage as pc

MIN_BETS = 136
# The purchases (docs/lab.md): H's in-play snapshots, H2's pre-match prices (H2P, H2U) and its
# in-play snapshots, by the calls' own cost headers.
RUNS = {"h_fetch": 40644447587, "h2p_fetch": 40744814233, "h2u_fetch": 40753094483,
        "h2_fetch": [40761027899, 40764374925]}  # fmt: skip
# (keys are the app's cost line: "H 1,000 · H2P pre-match prices 3,590 · …")
COST = {"design_h": 1000, "H2P pre-match prices": 3590, "H2U pre-match prices": 3800,
        "H2 in-play": 680}  # fmt: skip
CUP_CODES = set(hm.CUPS)
FLIP = {"home": "away", "away": "home"}


def espn_results(espn_dir: Path, slugs: set[str]) -> dict[str, list[int]]:
    """espn_id -> ESPN's 90-minute score [home, away] (extra time never counts)."""
    out = {}
    for slug in slugs:
        for path in sorted((Path(espn_dir) / cups.CACHE / "espn").glob(f"{slug}_*.json")):
            if path.name.endswith(".recent.json"):
                continue
            for e in cups.parse_events(json.loads(path.read_text())):
                if e["completed"] and e["score90"] is not None:
                    out[e["id"]] = list(e["score90"])
    return out


def extra_result(fd: pd.DataFrame, home: str, away: str, kickoff: str) -> list[int] | None:
    """football-data's extra-file score for one match: the teams and the date within a day."""
    day = pd.Timestamp(kickoff).tz_localize(None).normalize()
    hit = fd[(fd["home"] == home) & (fd["away"] == away)]
    hit = hit[(hit["date"] - day).abs() <= pd.Timedelta(days=1)]
    if len(hit) != 1:
        return None
    r = hit.iloc[0]
    return [int(r["fthg"]), int(r["ftag"])]


def quote_fields(ev: dict, snap, side: str, p_pre: float, fails: dict) -> dict:
    """`comebacks2.match_rows`' quote block for one event, `side` in the feed's orientation."""
    commence = c2._ts(ev["commence_time"])
    books = {}
    for b in {bk["key"] for bk in ev.get("bookmakers") or []} | {"draftkings", "pinnacle"}:
        q = c2.quote(ev, b)
        books[b] = {"q": q, "fail": c2.check_quote(q, commence, snap, side, p_pre)}
    passing = {
        b: c2.norm_chance(v["q"]["prices"], side) for b, v in books.items() if v["fail"] is None
    }
    pin = books["pinnacle"]
    if pin["fail"] is None:
        others = [c for b, c in passing.items() if b != "pinnacle"]
        if len(others) < c2.PIN_OTHERS or abs(passing["pinnacle"] - np.median(others)) > c2.PIN_TOL:
            pin["fail"] = "pinnacle_off"
    for b, v in books.items():
        if v["fail"]:
            fails.setdefault(b, dict.fromkeys(c2.CHECKS, 0))[v["fail"]] += 1
    dk = books["draftkings"]
    bf = next((books[b] for b in c2.BETFAIR if b in books and books[b]["fail"] is None), None)
    fair = None
    if pin["fail"] is None:
        p = pin["q"]["prices"]
        f = cb._fair(np.array([p["home"], p["draw"], p["away"]]))
        fair = float(f[0] if side == "home" else f[2])
    elif bf is not None:
        fair = c2.norm_chance(bf["q"]["prices"], side)
    return {
        "dk_odds": dk["q"]["prices"][side] if dk["fail"] is None else None,
        "bf_odds": bf["q"]["prices"][side] if bf is not None else None,
        "fair": fair,
        "fair_from": "pinnacle" if pin["fail"] is None else "betfair" if bf else None,
    }


def h2_rows(slots: list[dict], cache_dir: Path, espn_dir: Path,
            raw_dir: Path = RAW_DIR) -> tuple[list[dict], dict]:  # fmt: skip
    """One row per H2 trailing favourite, in `comebacks2.match_rows`' shape (model None)."""
    counts = {"slots": len(slots), "slots_missing": 0, "slots_off_half_time": 0,
              "matches": sum(len(s["matches"]) for s in slots), "unmatched": 0,
              "no_result": 0, "slots_not_priced": 0, "matches_not_priced": 0}  # fmt: skip
    guard = cf.read_guard(cache_dir)
    slugs = {hm.CUPS[s["league"]][1] for s in slots if s["league"] in CUP_CODES}
    espn = espn_results(espn_dir, slugs)
    extra: dict[str, pd.DataFrame] = {}
    fails: dict = {}
    rows = []
    for s in slots:
        path = cf.body_path(cache_dir, s)
        if not path.exists():
            if guard.get(cf.guard_key(s), {}).get("blocked"):
                counts["slots_not_priced"] += 1
                counts["matches_not_priced"] += len(s["matches"])
            else:
                counts["slots_missing"] += 1
            continue
        body = json.loads(path.read_text())
        snap = c2._ts(body.get("timestamp"))
        ko = c2._ts(s["kickoff"])
        mins = (snap - ko).total_seconds() / 60 if snap is not None else None
        if mins is None or not c2.SNAP_MIN <= mins <= c2.SNAP_MAX:
            counts["slots_off_half_time"] += 1
            continue
        events = body.get("data") or []
        for m in s["matches"]:
            hit = pc.find_side(events, m["home"], m["away"], ko)
            if hit is None and m.get("espn_home"):
                hit = pc.find_side(events, m["espn_home"], m["espn_away"], ko)
            if hit is None:
                counts["unmatched"] += 1
                continue
            if m["result_from"] == "espn":
                score = espn.get(str(m.get("espn_id")))
            else:
                if s["league"] not in extra:
                    extra[s["league"]] = hm.read_extra(s["league"], raw_dir)
                score = extra_result(extra[s["league"]], m["home"], m["away"], s["kickoff"])
            if score is None:
                counts["no_result"] += 1
                continue
            ev, swapped = hit
            side, p_pre = m["favourite"], float(m["p_fav"])
            q = quote_fields(ev, snap, FLIP[side] if swapped else side, p_pre, fails)
            fav = score[0] if side == "home" else score[1]
            dog = score[1] if side == "home" else score[0]
            ht = [int(m["ht"][0]), int(m["ht"][1])]
            rows.append(
                {
                    "league": s["league"],
                    "season": c2.season_of(s["kickoff"]),
                    "kickoff": s["kickoff"],
                    "home": m["home"],
                    "away": m["away"],
                    "favourite": side,
                    "ht": ht,
                    "band": cb.band_name(*next(b for b in cb.BANDS if b[0] <= p_pre < b[1])),
                    "deficit": ht[1] - ht[0] if side == "home" else ht[0] - ht[1],
                    "won": int(fav > dog),
                    **q,
                    "model": None,
                    "model_draw": None,
                    "ft": score,
                    "source": m["source"],
                }
            )
    counts["fails_by_book"] = fails
    return rows, counts


def verdict(primary: dict) -> tuple[str, str]:
    n = primary["matches"]
    if n < MIN_BETS:
        return "inconclusive", f"inconclusive: {n} usable bets (< {MIN_BETS})"
    lo, hi = primary["roi_ci95"]
    if lo > 0:
        return "pass", "pass: ROI range above 0 (justifies proposing a live paper track)"
    if hi < 0:
        return "fail", "fail: the in-play price over-prices the comeback (ROI range below 0)"
    return "fail", "fail: ROI range includes 0"


def group(rows: list[dict]) -> dict:
    p = c2.roi(rows, "dk_odds")
    g = c2.implied_gap(rows)
    return {"bets": p["matches"], "wins": p["wins"], "win_rate": p.get("win_rate"),
            "implied": g.get("implied"), "roi": p.get("roi"),
            "roi_ci95": p.get("roi_ci95")}  # fmt: skip


def design_block(role: str, label: str, rows: list[dict], counts: dict) -> dict:
    p = c2.roi(rows, "dk_odds")
    bf = c2.roi(rows, "bf_odds", c2.COMMISSION)
    gap = c2.implied_gap(rows)
    return {
        "role": role,
        "label": label,
        "series": label,
        "matches": counts.get("matches", 0),
        "not_priced": counts.get("matches_not_priced", 0),
        "unmatched": counts.get("unmatched", 0),
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
        "avg_odds": p.get("avg_odds"),
        "by_league": [
            {"code": lg, **group([r for r in rows if r["league"] == lg])}
            for lg in sorted({r["league"] for r in rows})
        ],  # fmt: skip
        "by_season": [
            {"season": f"{y}/{str(y + 1)[-2:]}", **group([r for r in rows if r["season"] == y])}
            for y in sorted({r["season"] for r in rows})
        ],  # fmt: skip
    }


def bet_rows(rows: list[dict]) -> list[dict]:
    return sorted(
        (
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
                "design": r["sample"],
            }  # fmt: skip
            for r in rows
            if r["dk_odds"]
        ),
        key=lambda b: (b["kickoff"], b["lg"], b["home"]),
    )


def add(counts_a: dict, counts_b: dict) -> dict:
    keys = ("matches", "matches_not_priced", "unmatched")
    return {k: counts_a.get(k, 0) + counts_b.get(k, 0) for k in keys}


def analyse(h: tuple[list, dict], h2: tuple[list, dict]) -> dict:
    (h_rows, h_counts), (h2_rows_, h2_counts) = h, h2
    for r in h_rows:
        r["sample"] = "H"
    for r in h2_rows_:
        r["sample"] = "H2"
    rows = h_rows + h2_rows_
    primary = c2.roi(rows, "dk_odds")
    code, text = verdict(primary)
    cup_rows = [r for r in h2_rows_ if r["league"] in CUP_CODES]
    league_rows = h_rows + [r for r in h2_rows_ if r["league"] not in CUP_CODES]
    return {
        "verdict": code,
        "verdict_text": text,
        "min_bets": MIN_BETS,
        "primary": primary,
        "counts": {"H": h_counts, "H2": h2_counts},
        "designs": {
            "pooled": design_block(
                "judged", "H and H2 together (judged)", rows, add(h_counts, h2_counts)
            ),  # fmt: skip
            "H": design_block("descriptive", "H: league matches", h_rows, h_counts),
            "H2": design_block("descriptive", "H2: cups and extra leagues", h2_rows_, h2_counts),
            "cups": design_block("descriptive", "Cups (H2)", cup_rows, {}),
            "leagues": design_block("descriptive", "Leagues (H and H2)", league_rows, {}),
        },
        "rows": rows,
    }


def app_file(res: dict, cost: dict, runs: dict) -> dict:
    d = res["designs"]["pooled"]
    rng = "" if not d["roi_ci95"] else f" ({d['roi_ci95'][0]:+.1%} to {d['roi_ci95'][1]:+.1%})"
    roi_text = ("" if d["roi"] is None else f", ROI {d['roi']:+.1%}{rng}").replace("-", "−")
    return {
        "title": "Heavy favourites coming back",
        "question": "Does a favourite priced 75% or more before kickoff, behind at half-time, "
        "win more often than its in-play price implies?",
        "rule": "$1 on the 75%+ favourite trailing at half-time, at DraftKings' in-play price "
        "from the snapshot at kickoff + 55 min (H and H2 judged together, at least "
        f"{MIN_BETS} bets, ROI range above 0 to pass).",
        "verdict": res["verdict"],  # pass / fail / inconclusive (the app may need a pill)
        "verdict_text": (
            f"Inconclusive: {d['bets']} usable bets, under the {MIN_BETS} the rule needs. "
            if res["verdict"] == "inconclusive"
            else f"{res['verdict_text'][0].upper()}{res['verdict_text'][1:]}. "
        )
        + f"{d['bets']} bets, won {d['wins']}{roi_text}.",
        "closed_at": f"{pd.Timestamp.now(tz='UTC'):%Y-%m-%d}",
        "preregistered_at": "2026-10-28",
        "preregistered": {"H": "ee40368", "H2": "9271595"},
        "cost": cost,
        "runs": runs,
        "designs": {k: v for k, v in res["designs"].items()},
        "bets": bet_rows(res["rows"]),
        "band_noun": "favourite",
        "price_name": "In-play price",
        "notes": [
            "H is the eight leagues' 2025/26 and 2026/27 plus ten other divisions; H2 adds the "
            "FA Cup, EFL Cup, the three UEFA cups and eleven more leagues.",
            "Cups and minor leagues may behave differently from H's leagues; the per-sample "
            "lines are descriptive only.",
            "Under the pre-registered rule, fewer than 136 usable bets reads inconclusive.",
        ],
    }


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--cache-dir", default="", help="the fetch's bodies (default raw dir)")
    p.add_argument("--espn-dir", default="", help="heavy_more's ESPN days (default raw dir)")
    p.add_argument("--app-json", default="", help="write the app's research file here")
    args = p.parse_args(argv)
    cache = Path(args.cache_dir) if args.cache_dir else Path(RAW_DIR) / cf.CACHE
    espn = Path(args.espn_dir) if args.espn_dir else Path(RAW_DIR) / hm.SUBDIR
    h_slots = cf.load_slots(cf.slots_path("H"))
    h = c2.match_rows(h_slots, cache, c2.load_fd(h_slots, any_price=True))
    h2 = h2_rows(cf.load_slots(cf.slots_path("H2")), cache, espn)
    res = analyse(h, h2)
    for key, d in res["designs"].items():
        roi = "–" if d["roi"] is None else f"{d['roi']:+.1%} {d['roi_ci95']}"
        print(f"  {key}: {d['bets']} bets, won {d['wins']}, ROI {roi}, implied {d['implied']}, "
              f"gap {d['gap']} {d['gap_ci95']}, Betfair {d['betfair_roi']}")  # fmt: skip
    print("counts: " + json.dumps(res["counts"]))
    print(f"VERDICT: {res['verdict_text']}")
    print("HEAVY_ANALYSIS_JSON " + json.dumps({k: v for k, v in res.items() if k != "rows"}))
    if args.app_json:
        cost = {**COST, "total": sum(v or 0 for v in COST.values())}
        text = json.dumps(app_file(res, cost, RUNS), separators=(",", ":"))
        Path(args.app_json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.app_json).write_text(text)
        print(f"HEAVY_RESEARCH_SHA256 {hashlib.sha256(text.encode()).hexdigest()} ({len(text)})")
        print("HEAVY_RESEARCH_JSON " + text)


if __name__ == "__main__":
    main()
