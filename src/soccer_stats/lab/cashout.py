"""Cash-out study (owner via the lead, 28 Oct): hold a winning underdog bet or cash it out at
half-time? Exploratory and free: no key, only the cached design B and C snapshots
(`comeback-odds-*`) and football-data, through `comebacks2.match_rows`.

Those snapshots are exactly the matches where the pre-match underdog led at half-time, the
moment a winning underdog bet would be cashed out. For each match with a passing DraftKings
in-play quote:
- entry: $1 on the underdog at Pinnacle's pre-match close (football-data);
- hold: the actual result (entry odds - 1 if the underdog won, else -1);
- cash out at half-time: entry odds x the underdog's margin-free in-play chance at
  DraftKings (Shin) x (1 - m) - 1, for each cash-out margin m in `MARGINS`;
- Kelly line: keep the share of the position quarter Kelly supports at DraftKings' live
  price on the phase-1 model's chance (bankroll `BANKROLL` per $1 entry), cash out the rest.
Every number here is conditional on the underdog leading at half-time, so the absolute
returns are far above any real strategy's; the comparison is hold against cash out.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from soccer_stats.data import RAW_DIR
from soccer_stats.lab import comeback_fetch as cf
from soccer_stats.lab import comebacks as cb
from soccer_stats.lab import comebacks2 as c2

MARGINS = (0.0, 0.05, 0.10)
ENTRY_BANDS = ((0.0, 3.0), (3.0, 4.0), (4.0, 1000.0))  # the underdog's pre-match decimal odds
LIVE_BANDS = ((0.0, 0.40), (0.40, 0.55), (0.55, 1.01))  # its in-play chance at half-time
KELLY_FRACTION = 0.25
BANKROLL = 100.0  # units of bankroll per $1 entry stake (a 1% entry)


def band_label(lo: float, hi: float, pct: bool = False) -> str:
    if pct:
        return f"<{hi:.0%}" if lo == 0 else f"{lo:.0%}+" if hi > 1 else f"{lo:.0%}-{hi:.0%}"
    return f"<{hi:.1f}" if lo == 0 else f"{lo:.1f}+" if hi >= 1000 else f"{lo:.1f}-{hi:.1f}"


def position(row: dict, design: str) -> dict | None:
    """One underdog position at half-time, or None without a passing DraftKings quote."""
    prices = row.get("dk_prices")
    if not prices or not all(prices.get(k) for k in ("home", "draw", "away")):
        return None
    dog = "away" if row["favourite"] == "home" else "home"
    entry = row["close"][2] if dog == "away" else row["close"][0]
    fair = cb._fair(np.array([prices["home"], prices["draw"], prices["away"]], float))
    q = float(fair[2] if dog == "away" else fair[0])
    fav_ft, dog_ft = (row["ft"][0], row["ft"][1]) if dog == "away" else (row["ft"][1], row["ft"][0])
    p_model = None
    if row.get("model") is not None and row.get("model_draw") is not None:
        p_model = max(0.0, 1.0 - row["model"] - row["model_draw"])
    return {
        "design": design,
        "league": row["league"],
        "entry": float(entry),
        "live_odds": float(prices[dog]),
        "q": q,
        "p_model": p_model,
        "won": int(dog_ft > fav_ft),
    }


def kelly_hold(p: dict) -> float:
    """The share of the position quarter Kelly keeps: its target stake at the live price
    over the stake that would pay the position's full return at that price."""
    if p["p_model"] is None:
        return 0.0
    d = p["live_odds"]
    f = KELLY_FRACTION * max(0.0, (p["p_model"] * d - 1) / (d - 1))
    exposure = p["entry"] / d  # stake at the live price paying the same return
    return float(min(1.0, f * BANKROLL / exposure))


def profits(p: dict) -> dict:
    hold = p["entry"] - 1 if p["won"] else -1.0
    out = {"hold": hold}
    h = kelly_hold(p)
    for m in MARGINS:
        cash = p["entry"] * p["q"] * (1 - m) - 1
        out[f"cash_{m:.2f}"] = cash
        out[f"kelly_{m:.2f}"] = h * hold + (1 - h) * cash
    out["kelly_share"] = h
    return out


def stats(values: np.ndarray) -> dict:
    return {
        "mean": round(float(values.mean()), 4),
        "sd": round(float(values.std(ddof=1)), 4) if len(values) > 1 else None,
        "ci95": c2.boot(values),
    }


def summary(pos: list[dict]) -> dict:
    if not pos:
        return {"matches": 0}
    pl = [profits(p) for p in pos]
    hold = np.array([x["hold"] for x in pl])
    out = {
        "matches": len(pos),
        "won": round(float(np.mean([p["won"] for p in pos])), 4),
        "implied": round(float(np.mean([p["q"] for p in pos])), 4),
        "gap": stats(np.array([p["won"] - p["q"] for p in pos], float)),
        "avg_entry": round(float(np.mean([p["entry"] for p in pos])), 3),
        "hold": stats(hold),
        "kelly_share": round(float(np.mean([x["kelly_share"] for x in pl])), 3),
    }
    for m in MARGINS:
        cash = np.array([x[f"cash_{m:.2f}"] for x in pl])
        kel = np.array([x[f"kelly_{m:.2f}"] for x in pl])
        out[f"cash_{m:.2f}"] = stats(cash)
        out[f"hold_minus_cash_{m:.2f}"] = stats(hold - cash)
        out[f"kelly_{m:.2f}"] = stats(kel)
    return out


def analyse(pos: list[dict]) -> dict:
    return {
        "all": summary(pos),
        "by_design": {d: summary([p for p in pos if p["design"] == d]) for d in ("C", "B")},
        "by_entry": {
            band_label(lo, hi): summary([p for p in pos if lo <= p["entry"] < hi])
            for lo, hi in ENTRY_BANDS
        },
        "by_live": {
            band_label(lo, hi, pct=True): summary([p for p in pos if lo <= p["q"] < hi])
            for lo, hi in LIVE_BANDS
        },
    }


def build(cache_dir: Path | None = None) -> list[dict]:
    cache_dir = Path(cache_dir) if cache_dir else Path(RAW_DIR) / cf.CACHE
    c_slots = cf.load_slots(cf.slots_path("C"))
    b_slots = cf.load_slots(cf.slots_path("B"))
    fd = c2.load_fd(c_slots + b_slots)
    out = []
    for design, slots in (("C", c_slots), ("B", b_slots)):
        rows, _ = c2.match_rows(slots, cache_dir, fd)
        out += [p for p in (position(r, design) for r in rows) if p is not None]
    return out


def _line(name: str, x: dict) -> str:
    lo, hi = x["ci95"] or (float("nan"), float("nan"))
    return f"{name} {x['mean']:+.3f} (sd {x['sd']}, {lo:+.3f} to {hi:+.3f})"


def report(res: dict) -> str:
    lines = ["== Cash-out study (exploratory; underdog leading at half-time, per $1) =="]
    groups = [("all", res["all"])]
    groups += [(f"design {k}", v) for k, v in res["by_design"].items()]
    groups += [(f"entry {k}", v) for k, v in res["by_entry"].items()]
    groups += [(f"live {k}", v) for k, v in res["by_live"].items()]
    for name, s in groups:
        if not s["matches"]:
            lines.append(f"  {name}: no matches")
            continue
        g = s["gap"]
        lines.append(
            f"  {name}: {s['matches']} matches, won {s['won']:.1%} vs implied {s['implied']:.1%} "
            f"({_line('gap', g)}), avg entry {s['avg_entry']}, Kelly keeps {s['kelly_share']:.0%}"
        )
        lines.append("    " + _line("hold", s["hold"]))
        for m in MARGINS:
            k = f"{m:.2f}"
            lines.append(
                f"    m={m:.0%}: {_line('cash', s['cash_' + k])}; "
                f"{_line('hold-cash', s['hold_minus_cash_' + k])}; "
                f"{_line('kelly', s['kelly_' + k])}"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--cache-dir", default="", help="the fetch's bodies (default raw dir)")
    p.add_argument("--json", default="")
    args = p.parse_args(argv)
    pos = build(Path(args.cache_dir) if args.cache_dir else None)
    res = analyse(pos)
    print(report(res))
    print("CASHOUT_JSON " + json.dumps(res))
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps({**res, "positions": pos}, indent=1))


if __name__ == "__main__":
    main()
