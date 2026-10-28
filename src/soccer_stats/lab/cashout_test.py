"""The cash-out test's frozen analysis (design X; docs/lab.md "Cash-out test", pre-registered
with `lab/cashout_slots.json` before any purchase; this code was written after the purchase
started and before any outcome was read). No key: the bought snapshots in `comeback-odds-*`
and football-data only. Run once.

* Matches: the slot file's (underdog quoted >= 3.0 at the close; primary band >= 4.0).
  A match counts when its event is found in at least one of its two snapshots; matches in
  a league-season the fetch's guard blocked count as not priced.
* In-play chance q at each moment (half-time: kickoff + 55, window 48-62 min; the 70th
  minute: kickoff + 85, window 78-92): DraftKings' h2h, Shin-fair, for the underdog. The
  quote must be updated after kickoff and at most 5 minutes before the snapshot; otherwise
  no cash-out is offered at that moment and the bet holds.
* Per $1 on the underdog at its quoted close: hold = the result; rule (T, m) = cash out at
  half-time if q >= T, else at the 70th minute if q >= T, for odds x q x (1 - m); Kelly
  partial (half-time only) = keep the share quarter Kelly supports at DraftKings' live
  price on the phase-1 model's chance, cash out the rest.
* Measures: mean profit and sd; hold - rule; growth = mean log(1 + s x profit) per bet at
  stake s, and its difference rule - hold; the worst drawdown at fixed stakes in kickoff
  order; q's calibration by band and moment. Ranges resample matches (2,000 draws, seed 0).
* Pass (one primary): margin 5%, T = 0.50, stake 2%, band 4.0+: the growth difference's
  range above 0 on >= 300 qualifying decisions (bets the rule cashed out on a passing
  quote). Fail at >= 300 otherwise; inconclusive under 300.
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
from soccer_stats.lab import comebacks2 as c2

MOMENTS = {"ht": (55, 48, 62), "m70": (85, 78, 92)}  # request minute, window lo, hi
THRESHOLDS = (0.40, 0.50, 0.60)
MARGINS = (0.0, 0.05, 0.10)
STAKES = (0.01, 0.02, 0.05)
PRIMARY = {"t": 0.50, "m": 0.05, "s": 0.02}
PRIMARY_DOG = 4.0
MIN_DECISIONS = 300
KELLY_FRACTION = 0.25
Q_BANDS = ((0.0, 0.40), (0.40, 0.55), (0.55, 1.01))
SECOND_HALF = c2.SECOND_HALF  # phase 1's share of goals after half-time
DRAWS, SEED = 2000, 0


# ---------- quotes ----------


def dk_chance(ev: dict, side: str, commence, snap) -> tuple[float, float] | None:
    """(Shin-fair chance, live decimal price) for `side` at DraftKings, or None when its
    quote fails the checks."""
    q = c2.quote(ev, "draftkings")
    if q is None or set(q["prices"]) != {"home", "draw", "away"}:
        return None
    upd = c2._ts(q["last_update"])
    if upd is None or upd <= commence or (snap - upd).total_seconds() > c2.STALE_MIN * 60:
        return None
    p = q["prices"]
    f = cb._fair(np.array([p["home"], p["draw"], p["away"]], float))
    return float(f[0] if side == "home" else f[2]), float(p[side])


def moment_quote(body: dict | None, moment: str, m: dict, kickoff, known) -> dict:
    """{found, q, live_odds} for one match at one moment."""
    out = {"found": False, "q": None, "live_odds": None}
    if body is None:
        return out
    snap, ko = c2._ts(body.get("timestamp")), c2._ts(kickoff)
    _, lo, hi = MOMENTS[moment]
    if snap is None or not lo <= (snap - ko).total_seconds() / 60 <= hi:
        return out
    ev = c2.find_event(body.get("data") or [], m["home"], m["away"], ko, known)
    if ev is None:
        return out
    out["found"] = True
    got = dk_chance(ev, m["dog"], c2._ts(ev["commence_time"]), snap)
    if got:
        out["q"], out["live_odds"] = got
    return out


# ---------- positions ----------


def model_dog(r: dict, dog: str, ht: tuple[int, int]) -> float | None:
    """The phase-1 model's half-time chance that the underdog wins, from the pre-match fair
    1X2 and over/under 2.5 (Poisson means) and the half-time score."""
    from soccer_stats.team_totals import implied_means

    if not np.isfinite(r.get("f_over", np.nan)) or not np.isfinite(r.get("fd", np.nan)):
        return None
    lh, la = implied_means(
        {"home": r["fh"], "draw": r["fd"], "away": r["fa"]},
        {"over25": r["f_over"], "under25": 1 - r["f_over"]},
    )
    l_fav, l_dog = (la, lh) if dog == "home" else (lh, la)
    dog_g, fav_g = (ht[0], ht[1]) if dog == "home" else (ht[1], ht[0])
    win, draw = cb.in_play(l_fav, l_dog, dog_g - fav_g, SECOND_HALF)
    return max(0.0, 1.0 - win - draw)


def build(slot_file: Path, cache_dir: Path, fd: pd.DataFrame) -> tuple[list[dict], dict]:
    """One position per match found in at least one snapshot."""
    res = json.loads(Path(slot_file).read_text())
    guard = cf.read_guard(cache_dir)
    counts = {"matches": 0, "not_priced": 0, "unmatched": 0, "no_result": 0}
    out = []
    for s in res["matches"]:
        season = c2.season_of(s["kickoff"])
        key = cf.guard_key(s)
        lg = fd[(fd["league"] == s["league"]) & (fd["season_start"] == season)]
        known = set(lg["home"]) | set(lg["away"])
        ko = pd.Timestamp(s["kickoff"])
        bodies = {}
        for moment, (minutes, _, _) in MOMENTS.items():
            req = {
                "sport": s["sport"],
                "request_at": (ko + pd.Timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%MZ"),
            }
            path = cf.body_path(cache_dir, req)
            bodies[moment] = json.loads(path.read_text()) if path.exists() else None
        for m in s["matches"]:
            counts["matches"] += 1
            if all(b is None for b in bodies.values()) and guard.get(key, {}).get("blocked"):
                counts["not_priced"] += 1
                continue
            quotes = {k: moment_quote(bodies[k], k, m, s["kickoff"], known) for k in MOMENTS}
            if not any(q["found"] for q in quotes.values()):
                counts["unmatched"] += 1
                continue
            hit = lg[(lg["home"] == m["home"]) & (lg["away"] == m["away"])]
            if hit.empty:
                counts["no_result"] += 1
                continue
            r = hit.iloc[0].to_dict()
            hg, ag = int(r["fthg"]), int(r["ftag"])
            ht = None
            if np.isfinite(r.get("hthg", np.nan)) and np.isfinite(r.get("htag", np.nan)):
                ht = (int(r["hthg"]), int(r["htag"]))
            won = (hg > ag) if m["dog"] == "home" else (ag > hg)
            out.append(
                {
                    "league": s["league"],
                    "season": season,
                    "kickoff": s["kickoff"],
                    "home": m["home"],
                    "away": m["away"],
                    "dog": m["dog"],
                    "entry": float(m["dog_odds"]),
                    "price": m["price"],
                    "won": int(won),
                    "q_ht": quotes["ht"]["q"],
                    "live_ht": quotes["ht"]["live_odds"],
                    "q_m70": quotes["m70"]["q"],
                    "p_model": model_dog(r, m["dog"], ht) if ht else None,
                }
            )
    counts["positions"] = len(out)
    return out, counts


def hold(p: dict) -> float:
    return p["entry"] - 1 if p["won"] else -1.0


def rule(p: dict, t: float, m: float) -> tuple[float, str | None]:
    """(profit per $1, the moment cashed out at or None)."""
    for moment in ("ht", "m70"):
        q = p[f"q_{moment}"]
        if q is not None and q >= t:
            return p["entry"] * q * (1 - m) - 1, moment
    return hold(p), None


def kelly(p: dict, m: float, s: float) -> float:
    """Keep the share quarter Kelly supports at half-time (bankroll 1/s per $1 entry)."""
    q, d, pm = p["q_ht"], p["live_ht"], p["p_model"]
    if q is None or d is None or pm is None or d <= 1:
        return hold(p)
    f = KELLY_FRACTION * max(0.0, (pm * d - 1) / (d - 1))
    h = min(1.0, f / (s * p["entry"] / d))
    return h * hold(p) + (1 - h) * (p["entry"] * q * (1 - m) - 1)


# ---------- measures ----------


def _boot(values: np.ndarray, draws: int = DRAWS, seed: int = SEED) -> list[float] | None:
    if len(values) < 2:
        return None
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(values), size=(draws, len(values)))
    means = values[idx].mean(axis=1)
    return [round(float(np.quantile(means, 0.025)), 5), round(float(np.quantile(means, 0.975)), 5)]


def drawdown(profits: np.ndarray, s: float) -> float:
    """The worst peak-to-trough fall of a bankroll staking s of itself on each bet."""
    bank = np.cumprod(1 + s * profits)
    peak = np.maximum.accumulate(np.concatenate([[1.0], bank]))[1:]
    return round(float((1 - bank / peak).max()), 4) if len(bank) else 0.0


def variant(name: str, prof: np.ndarray, base: np.ndarray, decisions: int | None) -> dict:
    out = {
        "name": name,
        "mean": round(float(prof.mean()), 5),
        "sd": round(float(prof.std(ddof=1)), 5) if len(prof) > 1 else None,
        "ci95": _boot(prof),
        "minus_hold": _boot(prof - base) if name != "hold" else None,
        "decisions": decisions,
        "growth": {},
    }
    for s in STAKES:
        g, g0 = np.log1p(s * prof), np.log1p(s * base)
        out["growth"][f"{s:.2f}"] = {
            "mean": round(float(g.mean()), 6),
            "ci95": _boot(g),
            "minus_hold": round(float((g - g0).mean()), 6),
            "minus_hold_ci95": _boot(g - g0) if name != "hold" else None,
            "drawdown": drawdown(prof, s),
        }
    return out


def calibration(pos: list[dict]) -> list[dict]:
    rows = []
    for moment in MOMENTS:
        for lo, hi in Q_BANDS:
            x = [p for p in pos if p[f"q_{moment}"] is not None and lo <= p[f"q_{moment}"] < hi]
            if not x:
                continue
            gap = np.array([p["won"] - p[f"q_{moment}"] for p in x], float)
            rows.append(
                {
                    "moment": moment,
                    "lo": lo,
                    "hi": min(hi, 1.0),
                    "n": len(x),
                    "won": round(float(np.mean([p["won"] for p in x])), 4),
                    "implied": round(float(np.mean([p[f"q_{moment}"] for p in x])), 4),
                    "gap": round(float(gap.mean()), 4),
                    "gap_ci95": _boot(gap),
                }
            )
    return rows


def analyse_band(pos: list[dict]) -> dict:
    pos = sorted(pos, key=lambda p: p["kickoff"])
    if not pos:
        return {"positions": 0}
    base = np.array([hold(p) for p in pos])
    out = {
        "positions": len(pos),
        "won": round(float(np.mean([p["won"] for p in pos])), 4),
        "avg_entry": round(float(np.mean([p["entry"] for p in pos])), 3),
        "quoted_ht": sum(p["q_ht"] is not None for p in pos),
        "quoted_m70": sum(p["q_m70"] is not None for p in pos),
        "variants": [variant("hold", base, base, None)],
        "calibration": calibration(pos),
    }
    for t in THRESHOLDS:
        for m in MARGINS:
            got = [rule(p, t, m) for p in pos]
            prof = np.array([g[0] for g in got])
            dec = sum(g[1] is not None for g in got)
            v = variant(f"cash T={t:.2f} m={m:.2f}", prof, base, dec)
            v.update(
                {
                    "t": t,
                    "m": m,
                    "at_ht": sum(g[1] == "ht" for g in got),
                    "at_m70": sum(g[1] == "m70" for g in got),
                }
            )
            out["variants"].append(v)
    for m in MARGINS:
        for s in STAKES:
            prof = np.array([kelly(p, m, s) for p in pos])
            v = variant(f"kelly m={m:.2f} s={s:.2f}", prof, base, None)
            v.update({"m": m, "s": s})
            out["variants"].append(v)
    return out


def verdict(primary_band: dict) -> dict:
    t, m, s = PRIMARY["t"], PRIMARY["m"], PRIMARY["s"]
    v = next(
        (x for x in primary_band.get("variants", []) if x.get("t") == t and x.get("m") == m),
        None,
    )
    if v is None:
        return {"verdict": "inconclusive", "decisions": 0}
    g = v["growth"][f"{s:.2f}"]
    ci = g["minus_hold_ci95"]
    if v["decisions"] < MIN_DECISIONS or ci is None:
        word = "inconclusive"
    elif ci[0] > 0:
        word = "pass"
    else:
        word = "fail"
    return {
        "verdict": word,
        "decisions": v["decisions"],
        "growth_minus_hold": g["minus_hold"],
        "ci95": ci,
        "below_zero": bool(ci and ci[1] < 0),
        "rule": f"T {t}, margin {m}, stake {s}, underdog {PRIMARY_DOG}+",
    }


def analyse(pos: list[dict], counts: dict) -> dict:
    primary = [p for p in pos if p["entry"] >= PRIMARY_DOG]
    second = [p for p in pos if p["entry"] < PRIMARY_DOG]
    res = {
        "counts": counts,
        "primary": analyse_band(primary),
        "band_3_4": analyse_band(second),
        "by_league": {},
        "by_season": {},
    }
    res["verdict"] = verdict(res["primary"])
    t, m, s = PRIMARY["t"], PRIMARY["m"], PRIMARY["s"]
    for key, field in (("by_league", "league"), ("by_season", "season")):
        for k in sorted({p[field] for p in primary}):
            x = sorted((p for p in primary if p[field] == k), key=lambda p: p["kickoff"])
            base = np.array([hold(p) for p in x])
            got = [rule(p, t, m) for p in x]
            prof = np.array([g[0] for g in got])
            res[key][str(k)] = {
                "positions": len(x),
                "decisions": sum(g[1] is not None for g in got),
                "hold": round(float(base.mean()), 4),
                "cash": round(float(prof.mean()), 4),
                "growth_minus_hold": round(
                    float((np.log1p(s * prof) - np.log1p(s * base)).mean()), 6
                ),
            }
    return res


def report(res: dict) -> str:
    c, v = res["counts"], res["verdict"]
    lines = [
        f"== Cash-out test (design X): {c['positions']} positions of {c['matches']} matches "
        f"({c['not_priced']} not priced, {c['unmatched']} unmatched, "
        f"{c['no_result']} no result) ==",
        f"  VERDICT: {v['verdict']} ({v.get('rule')}): {v['decisions']} decisions, growth "
        f"difference {v.get('growth_minus_hold')} per bet, 95% {v.get('ci95')}",
    ]
    for band in ("primary", "band_3_4"):
        b = res[band]
        if not b.get("positions"):
            lines.append(f"  {band}: none")
            continue
        lines.append(
            f"  {band}: {b['positions']} positions, won {b['won']:.1%}, avg entry "
            f"{b['avg_entry']}, quoted at half-time {b['quoted_ht']}, at 70' {b['quoted_m70']}"
        )
        for x in b["variants"]:
            g = x["growth"][f"{PRIMARY['s']:.2f}"]
            lines.append(
                f"    {x['name']}: mean {x['mean']:+.4f} {x['ci95']} sd {x['sd']}; minus hold "
                f"{x['minus_hold']}; decisions {x['decisions']}; growth@2% {g['mean']:+.6f}, "
                f"minus hold {g['minus_hold']:+.6f} {g['minus_hold_ci95']}, "
                f"drawdown {g['drawdown']}"
            )
        for r in b["calibration"]:
            lines.append(
                f"    q {r['moment']} {r['lo']:.0%}-{r['hi']:.0%}: n {r['n']}, won {r['won']:.1%} "
                f"vs {r['implied']:.1%} (gap {r['gap']:+.3f} {r['gap_ci95']})"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--cache-dir", default="", help="the bought bodies (default raw dir)")
    p.add_argument("--json", default="")
    args = p.parse_args(argv)
    cache = Path(args.cache_dir) if args.cache_dir else Path(RAW_DIR) / cf.CACHE
    path = cf.slots_path("X")
    slots = json.loads(path.read_text())["matches"]
    fd = c2.load_fd(slots, any_price=True)
    pos, counts = build(path, cache, fd)
    res = analyse(pos, counts)
    print(report(res))
    print("CASHOUT_TEST_JSON " + json.dumps(res))
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps({**res, "positions": pos}))


if __name__ == "__main__":
    main()
