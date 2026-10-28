"""Design H2's slot list (lead, 28 Oct; docs/lab.md "Design H2"): the favourites priced 75%+
before kickoff that trailed at half-time in the cups and `heavy_more`'s extra leagues, one
in-play snapshot per kickoff slot at kickoff + 55 min. Free, no key. It reads pre-match prices
and half-time scores only, never how a match ended (the extra leagues' join to ESPN matches
records by their 90-minute score, as `heavy_more` always has, and keeps nothing of it).

Sources:
- the domestic cups: the H2P pre-match snapshots (`prematch_coverage.trailing`);
- the UEFA cups: the H2U snapshots plus the held Champions League closes;
- the extra leagues: football-data's closing prices with ESPN's half-time score
  (`heavy_more.extra_league`).
Every slot bought by B, C, H or X is left out (none can overlap: other competitions).
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

import pandas as pd
import requests

from soccer_stats.cup_odds import HIST_COST
from soccer_stats.data import RAW_DIR
from soccer_stats.lab import comeback_fetch as cf
from soccer_stats.lab import heavy_more as hm
from soccer_stats.lab import prematch_coverage as pc
from soccer_stats.lab.comeback_plan import SNAPSHOT_AFTER

DESIGNS = ("H2P", "H2U")
USED = ("B", "C", "H", "X")


def cup_matches(cache_dir: Path, espn_dir: Path, closes: Path | None) -> tuple[list, dict]:
    rows, per = [], {}
    for d in DESIGNS:
        plan = json.loads(cf.slots_path(d).read_text())
        tr = pc.trailing(plan, cache_dir, espn_dir, closes)
        for k, v in tr["by_cup_season"].items():
            per[k] = {"priced": v.get("priced", 0), "heavy": v.get("heavy", 0),
                      "trailing": v.get("heavy_trailing", 0)}  # fmt: skip
        for r in tr["trailing_list"]:
            held = d == "H2U" and ("UCL", pc.season_of(r["kickoff"])) in hm.HELD_UCL
            rows.append({**r, "source": "held_close" if held else d, "result_from": "espn"})
    return rows, per


def extra_matches(raw_dir: Path, seconds: float, today: pd.Timestamp,
                  get=requests.get) -> tuple[list, dict, bool]:  # fmt: skip
    paced = hm.Paced(seconds, get=get)
    stats: dict = {}
    rows, per = [], {}
    for code in hm.EXTRA:
        try:
            got = hm.extra_league(code, raw_dir, paced, stats, today)
        except Exception as exc:  # noqa: BLE001 - one league failing is reported, not fatal
            per[code] = {"error": hm.why(exc)}
            continue
        for r in got:
            keep = ("matches", "joined", "heavy", "heavy_with_ht", "trailing")
            per[f"{code} {r['season']}"] = {k: r[k] for k in keep}
            rows += [{**m, "league": code, "source": "extra", "result_from": "football-data"}
                     for m in r["trailing_list"]]  # fmt: skip
    return rows, per, bool(paced.out_of_time or stats.get("out_of_time"))


def used_slots() -> set[tuple[str, str]]:
    return {(s["league"], s["kickoff"]) for d in USED for s in cf.load_slots(cf.slots_path(d))}


def sport_of(league: str) -> str:
    return (hm.CUPS.get(league) or hm.EXTRA[league])[2]


def freeze(matches: list[dict], per: dict, cutoff: pd.Timestamp,
           used: set | None = None) -> dict:  # fmt: skip
    """Group by competition and kickoff; one snapshot per slot at kickoff + SNAPSHOT_AFTER.
    Kickoffs at or after `cutoff` (the freeze) are left out."""
    used = used_slots() if used is None else used
    groups: dict[tuple, list] = {}
    dropped = Counter()
    for m in matches:
        if pd.Timestamp(m["kickoff"]) >= cutoff:
            dropped["after_freeze"] += 1
            continue
        if (m["league"], m["kickoff"]) in used:
            dropped["used"] += 1
            continue
        groups.setdefault((m["league"], m["kickoff"]), []).append(m)
    slots = []
    for (lg, ko), ms in groups.items():
        t = pd.Timestamp(ko) + pd.Timedelta(minutes=SNAPSHOT_AFTER)
        slots.append({"league": lg, "sport": sport_of(lg), "kickoff": ko,
                      "request_at": f"{t:%Y-%m-%dT%H:%MZ}",
                      "matches": [{k: v for k, v in m.items() if k not in ("league", "kickoff")}
                                  for m in sorted(ms, key=lambda m: m["home"])]})  # fmt: skip
    slots.sort(key=lambda s: (s["request_at"], s["league"]))
    by_source = Counter(m["source"] for s in slots for m in s["matches"])
    by_league = Counter(s["league"] for s in slots for _ in s["matches"])
    return {
        "design": "H2",
        "rule": "favourite >= 75% before kickoff (cups: Shin-fair from the bought pre-match "
        "snapshot, Pinnacle else DraftKings; held UCL closes; extra leagues: football-data's "
        "Pinnacle close, else the average's) trailing at half-time (ESPN); one in-play "
        f"snapshot per slot at kickoff + {SNAPSHOT_AFTER} min",
        "guard": True,
        "per_source_season": per,
        "dropped": dict(dropped),
        "slots": slots,
        "totals": {
            "slots": len(slots),
            "matches": sum(len(s["matches"]) for s in slots),
            "credits": len(slots) * HIST_COST,
            "by_source": dict(by_source),
            "by_league": dict(sorted(by_league.items())),
        },  # fmt: skip
        "frozen_at": f"{cutoff:%Y-%m-%d}",
    }


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--cache-dir", default="", help="the bought bodies (default raw dir)")
    p.add_argument("--espn-dir", default="", help="heavy_more's ESPN days (default raw dir)")
    p.add_argument("--closes", default="", help="UCL_closes.jsonl (held Champions League closes)")
    p.add_argument("--minutes", type=float, default=60, help="ESPN time budget")
    p.add_argument("--freeze", default="", help="freeze date YYYY-MM-DD (default today, UTC)")
    p.add_argument("--out", default="", help="write the slot file here")
    args = p.parse_args(argv)
    cache = Path(args.cache_dir) if args.cache_dir else Path(RAW_DIR) / cf.CACHE
    espn = Path(args.espn_dir) if args.espn_dir else Path(RAW_DIR) / hm.SUBDIR
    today = pd.Timestamp.now(tz="UTC")
    cutoff = pd.Timestamp(args.freeze, tz="UTC") if args.freeze else today.normalize()
    cup, cup_per = cup_matches(cache, espn, Path(args.closes) if args.closes else None)
    extra, extra_per, late = extra_matches(Path(RAW_DIR), args.minutes * 60, today)
    if late:
        raise SystemExit("ESPN ran out of time; nothing frozen (re-run to resume)")
    plan = freeze(cup + extra, {**cup_per, **extra_per}, cutoff)
    text = json.dumps(plan, separators=(",", ":"))
    for k, v in plan["per_source_season"].items():
        print(f"  {k}: {v}")
    print("H2 totals: " + json.dumps(plan["totals"]) + " dropped " + json.dumps(plan["dropped"]))
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text)
    print(f"H2_SLOTS_SHA256 {hashlib.sha256(text.encode()).hexdigest()} ({len(text)} bytes)")
    print("H2_SLOTS_JSON " + text)


if __name__ == "__main__":
    main()
