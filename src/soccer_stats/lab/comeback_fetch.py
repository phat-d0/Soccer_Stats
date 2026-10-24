"""Comeback phase 2, design B fetch (pre-registered in `docs/lab.md`, "Phase 2, step 2"):
one historical bulk h2h snapshot per frozen slot (`comeback_b_slots.json`), at ten named
books (one region, about 10 credits), requested at kickoff + 55 minutes.

Spending rules:
- every call goes through `CappedBudget` (the `cap` and the 3,000-credit reserve on the
  freshest balance; cost from `x-requests-last`);
- the first call is budgeted at `FIRST_ESTIMATE`, and the run stops if any call costs more
  than `MAX_CALL_COST`;
- an HTTP or network error stops every call;
- a slot is never bought twice: its body in the cache, or an answered call in the call
  record, skips it.

cap 0 (or no key) is a dry run: the slots still to buy and the estimate, no paid call. The
key is redacted from every message. This reads only the slot list, never how a match ended.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import requests

from soccer_stats.cup_odds import HIST_COST, URL, _call, balance, iso, redact, stamp
from soccer_stats.data import RAW_DIR
from soccer_stats.player_goal_odds import CappedBudget

SLOTS_FILE = Path(__file__).with_name("comeback_b_slots.json")
CACHE = "comeback_odds"  # under RAW_DIR; the Actions cache `comeback-odds-*` holds it
CALLS = "calls.jsonl"
BOOKS = (
    "pinnacle",
    "draftkings",
    "betfair_ex_eu",
    "betfair_ex_uk",
    "fanduel",
    "betmgm",
    "unibet_eu",
    "sport888",
    "bovada",
    "paddypower",
)
FIRST_ESTIMATE = 20  # the first call is budgeted high, until one call's cost is seen
MAX_CALL_COST = 10  # a call costing more than this stops the run


def load_slots(path: Path = SLOTS_FILE) -> list[dict]:
    return json.loads(Path(path).read_text())["slots"]


def body_path(cache_dir: Path, slot: dict) -> Path:
    return Path(cache_dir) / f"{slot['sport']}_{stamp(slot['request_at'])}.json"


def read_calls(cache_dir: Path) -> list[dict]:
    path = Path(cache_dir) / CALLS
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def _append_call(cache_dir: Path, row: dict) -> None:
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    with (Path(cache_dir) / CALLS).open("a") as f:
        f.write(json.dumps(row) + "\n")


def held(cache_dir: Path, slot: dict, answered: set) -> bool:
    return body_path(cache_dir, slot).exists() or (slot["sport"], slot["request_at"]) in answered


def run(
    cap: int = 0,
    api_key: str | None = None,
    cache_dir: Path | None = None,
    slots: list[dict] | None = None,
    get: Callable = requests.get,
    books=BOOKS,
) -> dict:
    api_key = api_key if api_key is not None else os.environ.get("ODDS_API_KEY", "")
    cache_dir = Path(cache_dir) if cache_dir is not None else Path(RAW_DIR) / CACHE
    slots = load_slots() if slots is None else slots
    answered = {
        (c["sport"], c["request_at"]) for c in read_calls(cache_dir) if c.get("status") == 200
    }
    pending = [s for s in slots if not held(cache_dir, s, answered)]
    budget = CappedBudget(max(int(cap), 0))
    res: dict = {
        "slots": len(slots),
        "held": len(slots) - len(pending),
        "pending": len(pending),
        "estimate": len(pending) * HIST_COST,
        "cap": int(cap),
        "books": list(books),
        "bought": 0,
        "spent": 0,
        "stopped": None,
        "log": budget.log,
    }
    if cap <= 0 or not api_key:
        res["dry_run"] = True
        budget.log.append(
            "Dry run (cap 0 or no key): no Odds API calls; "
            f"{len(pending)} slots to buy, about {res['estimate']} credits"
        )
        res["to_buy"] = [f"{s['league']} {s['request_at']}" for s in pending]
        return res
    res["dry_run"] = False
    budget.left = balance(get, api_key)
    res["left_before"] = budget.left
    budget.log.append(f"Credits left before the run: {budget.left}")
    for s in pending:
        estimate = FIRST_ESTIMATE if res["bought"] == 0 else HIST_COST
        params = {
            "apiKey": api_key,
            "date": iso(pd.Timestamp(s["request_at"])),
            "bookmakers": ",".join(books),
            "markets": "h2h",
            "oddsFormat": "decimal",
            "dateFormat": "iso",
        }
        label = f"{s['league']} {s['request_at']}"
        body, info = _call(
            get, URL.format(sport=s["sport"]), params, budget, estimate, label, api_key
        )
        if info.get("skipped"):
            res["stopped"] = redact(info.get("message", "skipped"), api_key)
            break
        row = {
            "league": s["league"],
            "sport": s["sport"],
            "request_at": s["request_at"],
            "status": info.get("status"),
            "cost": info.get("cost"),
            "fetched_at": iso(pd.Timestamp.now(tz="UTC")),
            "message": redact(info.get("message", ""), api_key),
        }
        if body is not None:
            body_path(cache_dir, s).parent.mkdir(parents=True, exist_ok=True)
            body_path(cache_dir, s).write_text(json.dumps(body))
            row["snapshot"] = body.get("timestamp")
            row["events"] = len(body.get("data") or [])
            res["bought"] += 1
        _append_call(cache_dir, row)
        res["spent"] = budget.spent
        if info.get("stop"):
            res["stopped"] = row["message"] or f"HTTP {row['status']}"
            break
        if (info.get("cost") or 0) > MAX_CALL_COST:
            res["stopped"] = f"a call cost {info['cost']} credits (> {MAX_CALL_COST})"
            budget.log.append(f"Stopping: {res['stopped']}")
            break
    res["left_after"] = budget.left
    res["log"] = [redact(line, api_key) for line in budget.log]
    return res


def report(res: dict) -> str:
    lines = [
        f"== Comeback design B fetch: {res['slots']} slots, {res['held']} held, "
        f"{res['pending']} to buy (about {res['estimate']} credits), cap {res['cap']} ==",
        *[f"  {line}" for line in res["log"][-12:]],
    ]
    if not res.get("dry_run"):
        lines.append(
            f"  bought {res['bought']} snapshots, spent {res['spent']} credits; credits left "
            f"{res.get('left_before')} -> {res.get('left_after')}"
        )
        if res.get("stopped"):
            lines.append(f"  stopped: {res['stopped']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--cap", type=int, default=0, help="most credits to spend (0 = dry run)")
    p.add_argument("--cache-dir", default="", help="bodies and call record (default raw dir)")
    args = p.parse_args(argv)
    res = run(cap=args.cap, cache_dir=Path(args.cache_dir) if args.cache_dir else None)
    print(report(res))
    compact = {k: v for k, v in res.items() if k not in ("log", "to_buy")}
    print("COMEBACK_FETCH_JSON " + json.dumps(compact))


if __name__ == "__main__":
    main()
