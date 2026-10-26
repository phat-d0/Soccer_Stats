"""Comeback phase 2, design B analysis (pre-registered in `docs/lab.md`, "Phase 2, step 2").
No key: reads only the cached snapshot bodies (`comeback_fetch`), the frozen slot list and
football-data's 2024/25 files.

Per trailing favourite: the snapshot is checked (48-62 minutes after kickoff), the event is
matched by both team names and kickoff (30 minutes), each book's quote goes through the
quote check (all three prices, updated after kickoff, at most 5 minutes before the snapshot,
the favourite's normalised chance below its fair pre-match chance at Pinnacle's close),
and Pinnacle's normalised chance must sit within `PIN_TOL` of the other passing books'
median (at least two of them).
- Primary: $1 on the favourite at DraftKings' passing in-play price, ROI with a
  match-resampled 95% range; pass = at least `MIN_MATCHES` usable and the range above 0.
- Second line: Betfair's exchange back price (eu, then uk), wins paid after `COMMISSION`.
- Secondary: realized minus market-implied win rate (Pinnacle Shin-fair when it passes, else
  Betfair normalised), overall and by band; the logistic blend weight c of phase 1's model
  (second-half share `SECOND_HALF`) beside the market's fair chance.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from soccer_stats.data import RAW_DIR
from soccer_stats.lab import comeback_fetch as cf
from soccer_stats.lab import comebacks as cb
from soccer_stats.lab.comeback_plan import load_season
from soccer_stats.odds_feed import _team

SNAP_MIN, SNAP_MAX = 48, 62  # the snapshot must fall in the half-time break
KICKOFF_TOL = 30  # minutes between football-data's kickoff and the event's
STALE_MIN = 5  # a quote updated longer before the snapshot than this fails
PIN_TOL = 0.05  # Pinnacle's normalised chance vs the other books' median
PIN_OTHERS = 2  # Pinnacle's check needs at least this many other passing books
MIN_MATCHES = 100
COMMISSION = 0.02
SECOND_HALF = 0.555  # phase 1's measured share of goals after half-time
DRAWS, SEED = 2000, 0
BETFAIR = ("betfair_ex_eu", "betfair_ex_uk")
CHECKS = ("not_quoted", "missing_price", "before_kickoff", "stale", "not_moved", "pinnacle_off")


def _ts(x) -> pd.Timestamp | None:
    try:
        t = pd.Timestamp(x)
    except (TypeError, ValueError):
        return None
    if pd.isna(t):
        return None
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def season_of(kickoff) -> int:
    """football-data's season start year for a kickoff (seasons start in July)."""
    t = _ts(kickoff)
    return t.year if t.month >= 7 else t.year - 1


def find_event(events: list[dict], home: str, away: str, kickoff, known: set[str]):
    """The snapshot event for a football-data match: both names map and kickoff within
    KICKOFF_TOL minutes."""
    ko = _ts(kickoff)
    for ev in events:
        if _team(ev.get("home_team", ""), known) != home:
            continue
        if _team(ev.get("away_team", ""), known) != away:
            continue
        ct = _ts(ev.get("commence_time"))
        if ct is not None and abs((ct - ko).total_seconds()) <= KICKOFF_TOL * 60:
            return ev
    return None


def quote(ev: dict, book: str) -> dict | None:
    """A book's h2h prices as {home, draw, away} plus its last update, or None if unlisted."""
    for b in ev.get("bookmakers") or []:
        if b.get("key") != book:
            continue
        for m in b.get("markets") or []:
            if m.get("key") != "h2h":
                continue
            prices = {}
            for o in m.get("outcomes") or []:
                name = o.get("name")
                side = (
                    "home"
                    if name == ev.get("home_team")
                    else "away"
                    if name == ev.get("away_team")
                    else "draw"
                    if str(name).lower() == "draw"
                    else None
                )
                if side and o.get("price"):
                    prices[side] = float(o["price"])
            return {"prices": prices, "last_update": m.get("last_update") or b.get("last_update")}
    return None


def norm_chance(prices: dict, side: str) -> float:
    inv = {k: 1 / v for k, v in prices.items()}
    return inv[side] / sum(inv.values())


def check_quote(q, commence, snap, side: str, p_pre: float) -> str | None:
    """None when the quote passes, else the first failing check's name."""
    if q is None:
        return "not_quoted"
    if set(q["prices"]) != {"home", "draw", "away"}:
        return "missing_price"
    upd = _ts(q["last_update"])
    if upd is None or upd <= commence:
        return "before_kickoff"
    if (snap - upd).total_seconds() > STALE_MIN * 60:
        return "stale"
    if norm_chance(q["prices"], side) >= p_pre:
        return "not_moved"
    return None


def match_rows(slots: list[dict], cache_dir: Path, fd: pd.DataFrame) -> tuple[list[dict], dict]:
    """One row per trailing favourite with its quotes, checks, result and model chance."""
    counts = {"slots": len(slots), "slots_missing": 0, "slots_off_half_time": 0}
    counts["matches"] = sum(len(s["matches"]) for s in slots)
    counts["unmatched"] = 0
    counts["slots_not_priced"] = counts["matches_not_priced"] = 0
    guard = cf.read_guard(cache_dir)  # league-seasons the fetch stopped buying
    fails = {}
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
        snap = _ts(body.get("timestamp"))
        ko = _ts(s["kickoff"])
        mins = (snap - ko).total_seconds() / 60 if snap is not None else None
        if mins is None or not SNAP_MIN <= mins <= SNAP_MAX:
            counts["slots_off_half_time"] += 1
            continue
        season = season_of(s["kickoff"])
        lg = fd[(fd["league"] == s["league"]) & (fd["season_start"] == season)]
        known = set(lg["home"]) | set(lg["away"])
        for m in s["matches"]:
            ev = find_event(body.get("data") or [], m["home"], m["away"], ko, known)
            if ev is None:
                counts["unmatched"] += 1
                continue
            hit = lg[(lg["home"] == m["home"]) & (lg["away"] == m["away"])]
            if hit.empty:
                counts["unmatched"] += 1
                continue
            r = hit.iloc[0]
            side, p_pre = m["favourite"], float(m["p_fav"])
            commence = _ts(ev["commence_time"])
            books = {}
            for b in {bk["key"] for bk in ev.get("bookmakers") or []} | {"draftkings", "pinnacle"}:
                q = quote(ev, b)
                why = check_quote(q, commence, snap, side, p_pre)
                books[b] = {"q": q, "fail": why}
            passing = {
                b: norm_chance(v["q"]["prices"], side)
                for b, v in books.items()
                if v["fail"] is None
            }
            pin = books["pinnacle"]
            if pin["fail"] is None:
                others = [c for b, c in passing.items() if b != "pinnacle"]
                if (
                    len(others) < PIN_OTHERS
                    or abs(passing["pinnacle"] - np.median(others)) > PIN_TOL
                ):
                    pin["fail"] = "pinnacle_off"
            for b, v in books.items():
                if v["fail"]:
                    fails.setdefault(b, dict.fromkeys(CHECKS, 0))[v["fail"]] += 1
            fav_ft = r["fthg"] if side == "home" else r["ftag"]
            dog_ft = r["ftag"] if side == "home" else r["fthg"]
            dk = books["draftkings"]
            bf = next((books[b] for b in BETFAIR if b in books and books[b]["fail"] is None), None)
            fair = None
            if pin["fail"] is None:
                prices = pin["q"]["prices"]
                f = cb._fair(np.array([prices["home"], prices["draw"], prices["away"]]))
                fair = float(f[0] if side == "home" else f[2])
            elif bf is not None:
                fair = norm_chance(bf["q"]["prices"], side)
            means = cb.pre_match_means(r.to_dict())
            deficit = (
                int(m["ht"][1] - m["ht"][0]) if side == "home" else int(m["ht"][0] - m["ht"][1])
            )
            model = cb.in_play(*means, deficit, SECOND_HALF)[0] if means else None
            rows.append(
                {
                    "league": s["league"],
                    "season": season,
                    "kickoff": s["kickoff"],
                    "home": m["home"],
                    "away": m["away"],
                    "favourite": side,
                    "ht": [int(m["ht"][0]), int(m["ht"][1])],
                    "band": cb.band_name(*next(b for b in cb.BANDS if b[0] <= p_pre < b[1])),
                    "deficit": deficit,
                    "won": int(fav_ft > dog_ft),
                    "dk_odds": dk["q"]["prices"][side] if dk["fail"] is None else None,
                    "bf_odds": bf["q"]["prices"][side] if bf is not None else None,
                    "fair": fair,
                    "fair_from": "pinnacle" if pin["fail"] is None else "betfair" if bf else None,
                    "model": model,
                }
            )
    counts["fails_by_book"] = fails
    return rows, counts


def boot(values: np.ndarray, draws: int = DRAWS, seed: int = SEED) -> list[float] | None:
    if len(values) < 2:
        return None
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(values), size=(draws, len(values)))
    means = values[idx].mean(axis=1)
    return [round(float(np.percentile(means, 2.5)), 5), round(float(np.percentile(means, 97.5)), 5)]


def roi(rows: list[dict], key: str, commission: float = 0.0) -> dict:
    x = [r for r in rows if r[key]]
    profit = np.array([(r[key] - 1) * (1 - commission) if r["won"] else -1.0 for r in x])
    out = {"matches": len(x), "wins": int(sum(r["won"] for r in x))}
    if len(x):
        out.update(
            roi=round(float(profit.mean()), 5),
            roi_ci95=boot(profit),
            avg_odds=round(float(np.mean([r[key] for r in x])), 3),
            win_rate=round(out["wins"] / len(x), 5),
            breakeven=round(float(np.mean([1 / r[key] for r in x])), 5),
        )
    return out


def implied_gap(rows: list[dict]) -> dict:
    x = [r for r in rows if r["fair"] is not None]
    d = np.array([r["won"] - r["fair"] for r in x])
    out = {"matches": len(x)}
    if len(x):
        out.update(
            realized=round(float(np.mean([r["won"] for r in x])), 5),
            implied=round(float(np.mean([r["fair"] for r in x])), 5),
            gap=round(float(d.mean()), 5),
            gap_ci95=boot(d),
            from_pinnacle=sum(r["fair_from"] == "pinnacle" for r in x),
        )
    return out


def _logit(p):
    p = np.clip(np.asarray(p, float), 1e-6, 1 - 1e-6)
    return np.log(p / (1 - p))


def logistic(X: np.ndarray, y: np.ndarray, iters: int = 50) -> np.ndarray | None:
    beta = np.zeros(X.shape[1])
    for _ in range(iters):
        p = 1 / (1 + np.exp(-X @ beta))
        w = p * (1 - p)
        try:
            step = np.linalg.solve(
                X.T @ (X * w[:, None]) + 1e-9 * np.eye(X.shape[1]), X.T @ (y - p)
            )
        except np.linalg.LinAlgError:
            return None
        beta += step
        if np.abs(step).max() < 1e-8:
            break
    return beta if np.all(np.isfinite(beta)) else None


def blend_weight(rows: list[dict]) -> dict:
    x = [r for r in rows if r["fair"] is not None and r["model"] is not None]
    out = {"matches": len(x)}
    if len(x) < 20 or len({r["won"] for r in x}) < 2:
        return out
    X = np.column_stack(
        [np.ones(len(x)), _logit([r["fair"] for r in x]), _logit([r["model"] for r in x])]
    )
    y = np.array([r["won"] for r in x], float)
    beta = logistic(X, y)
    if beta is None:
        return out
    rng = np.random.default_rng(SEED)
    cs = []
    for _ in range(DRAWS):
        i = rng.integers(0, len(x), len(x))
        b = logistic(X[i], y[i])
        if b is not None:
            cs.append(b[2])
    out.update(
        a=round(float(beta[0]), 4),
        b=round(float(beta[1]), 4),
        c=round(float(beta[2]), 4),
        c_ci95=[round(float(np.percentile(cs, 2.5)), 4), round(float(np.percentile(cs, 97.5)), 4)]
        if cs
        else None,
    )
    return out


def verdict(primary: dict) -> str:
    if primary["matches"] < MIN_MATCHES:
        return f"inconclusive: {primary['matches']} usable matches (< {MIN_MATCHES})"
    lo, hi = primary["roi_ci95"]
    if lo > 0:
        return "pass: ROI range above 0 (justifies proposing design C to the owner)"
    if hi < 0:
        return "fail: the in-play price over-prices the comeback (ROI range below 0)"
    return "fail: ROI range includes 0"


def analyse(rows: list[dict], counts: dict) -> dict:
    primary = roi(rows, "dk_odds")
    by_band = {
        cb.band_name(lo, hi): roi([r for r in rows if r["band"] == cb.band_name(lo, hi)], "dk_odds")
        for lo, hi in cb.BANDS
    }
    return {
        "counts": counts,
        "rows": len(rows),
        "primary_draftkings": primary,
        "primary_by_band": by_band,
        "second_line_betfair": roi(rows, "bf_odds", COMMISSION),
        "implied_gap": implied_gap(rows),
        "implied_gap_by_band": {
            cb.band_name(lo, hi): implied_gap(
                [r for r in rows if r["band"] == cb.band_name(lo, hi)]
            )
            for lo, hi in cb.BANDS
        },
        "blend_weight": blend_weight(rows),
        "primary_by_league": {
            lg: roi([r for r in rows if r["league"] == lg], "dk_odds")
            for lg in sorted({r["league"] for r in rows})
        },
        "primary_by_season": {
            f"{y}/{str(y + 1)[-2:]}": roi([r for r in rows if r["season"] == y], "dk_odds")
            for y in sorted({r["season"] for r in rows})
        },
        "verdict": verdict(primary),
    }


def report(res: dict, title: str = "design B") -> str:
    c = res["counts"]

    def rng(v):
        return "–" if not v else f"{v[0]:+.1%} to {v[1]:+.1%}"

    p, b, g = res["primary_draftkings"], res["second_line_betfair"], res["implied_gap"]
    lines = [
        f"== Comeback phase 2, {title} ==",
        f"  slots {c['slots']} (missing {c['slots_missing']}, not priced "
        f"{c.get('slots_not_priced', 0)} with {c.get('matches_not_priced', 0)} matches, "
        f"off half-time {c['slots_off_half_time']}); matches {c['matches']}, "
        f"unmatched {c['unmatched']}, "
        f"rows {res['rows']}",
        f"  quote-check fails by book: {json.dumps(c['fails_by_book'])}",
        f"  PRIMARY DraftKings: {p['matches']} bets, won {p['wins']}"
        + (
            f", avg odds {p['avg_odds']}, ROI {p['roi']:+.1%} ({rng(p['roi_ci95'])})"
            if p["matches"]
            else ""
        ),
    ]
    for band, x in res["primary_by_band"].items():
        if x["matches"]:
            lines.append(
                f"    {band}: {x['matches']} bets, ROI {x['roi']:+.1%} ({rng(x['roi_ci95'])})"
            )
    if b["matches"]:
        lines.append(
            f"  Betfair (after 2%): {b['matches']} bets, ROI {b['roi']:+.1%} ({rng(b['roi_ci95'])})"
        )
    if g["matches"]:
        lines.append(
            f"  realized {g['realized']:.1%} vs market {g['implied']:.1%}: gap {g['gap']:+.1%} "
            f"({rng(g['gap_ci95'])}); Pinnacle fair on {g['from_pinnacle']} of {g['matches']}"
        )
    lines.append(f"  blend weight: {json.dumps(res['blend_weight'])}")
    for key, label in (("primary_by_league", "league"), ("primary_by_season", "season")):
        for k, x in res.get(key, {}).items():
            if x["matches"]:
                lines.append(
                    f"    by {label} (descriptive) {k}: {x['matches']} bets, "
                    f"ROI {x['roi']:+.1%} ({rng(x['roi_ci95'])})"
                )
    lines.append(f"VERDICT: {res['verdict']}")
    return "\n".join(lines)


def load_fd(slots: list[dict]) -> pd.DataFrame:
    """football-data favourites for every league-season the slots touch."""
    pairs = sorted({(s["league"], season_of(s["kickoff"])) for s in slots})
    return pd.concat([cb.favourites(load_season(lg, y)) for lg, y in pairs], ignore_index=True)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--cache-dir", default="", help="the fetch's bodies (default raw dir)")
    p.add_argument("--slots", default="B", help="design B or C (C is judged; B stays apart)")
    p.add_argument("--json", default="")
    args = p.parse_args(argv)
    cache_dir = Path(args.cache_dir) if args.cache_dir else Path(RAW_DIR) / cf.CACHE
    design = args.slots.upper()
    slots = cf.load_slots(cf.slots_path(design))
    b_slots = cf.load_slots(cf.slots_path("B")) if design == "C" else []
    fd = load_fd(slots + b_slots)
    rows, counts = match_rows(slots, cache_dir, fd)
    res = analyse(rows, counts)
    print(report(res, f"design {design}" + (" (C minus B, judged)" if design == "C" else "")))
    out = {**res}
    if design == "C":  # B was looked at already: descriptive only, never in the verdict
        b_rows, b_counts = match_rows(b_slots, cache_dir, fd)
        res_b = analyse(b_rows, b_counts)
        res_pool = analyse(rows + b_rows, counts)
        for name, r in (("B alone", res_b), ("C+B pooled", res_pool)):
            x = r["primary_draftkings"]
            if x["matches"]:
                lo, hi = x["roi_ci95"]
                print(
                    f"  descriptive, {name}: {x['matches']} bets, won {x['wins']}, "
                    f"ROI {x['roi']:+.1%} ({lo:+.1%} to {hi:+.1%})"
                )
        out["descriptive_b"] = {k: v for k, v in res_b.items() if k != "verdict"}
        out["descriptive_pooled"] = {
            k: v for k, v in res_pool.items() if k not in ("verdict", "counts")
        }
    print("COMEBACKS2_JSON " + json.dumps(out))
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps({**out, "detail": rows}, indent=1, default=str))


if __name__ == "__main__":
    main()
