"""Goal timelines from ESPN (owner, 25 Oct; exploratory; `docs/lab.md` "Goal timelines from
ESPN"). Free: ESPN's site scoreboard and football-data, no key, 0 credits.

When in a match does the favourite trail, and how often does it come back from each scoreline
and minute, against phase 1's fair time-scaled model? The headline event is **trail first**:
the underdog scores the opening goal, at any minute.

Fetch (`--fetch`, prints counts only): one scoreboard per league and date, only on dates
football-data lists a match, for the eight domestic leagues (`espn_news.SLUGS`), 2017/18 to
2024/25 (2025/26 is dropped first, as in phase 1). Each day is reduced to a compact record
(teams, ids, status, score and every scoring play's clock and team) and cached for good under
`data/raw/espn_timelines/<slug>/<YYYYMMDD>.json`, so runs resume. Polite: `PAUSE` between
requests (at most 2 a second), retries on 5xx and network errors only, a time budget per run.

Analysis (default): ESPN events joined to football-data by league, season, both names
(`espn_news.team_name`) and date (a day either side); the 90-minute and half-time scores
rebuilt from ESPN's goals must equal football-data's or the match is dropped. Favourites and
fair means as in phase 1 (Pinnacle's closing 1X2 and over/under 2.5). Everything here is
exploratory: a minute or band that looks good becomes a new pre-registration on fresh seasons,
never a re-reading of the comeback test's design C.
"""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from soccer_stats.cups import _minute
from soccer_stats.data import RAW_DIR
from soccer_stats.espn_news import BASE, SLUGS, team_name
from soccer_stats.lab import comebacks as cb
from soccer_stats.lab.comeback_plan import load_season

LEAGUES = cb.LEAGUES_B
YEARS = cb.YEARS  # 2017/18-2024/25
CACHE = "espn_timelines"
PAUSE = 0.5  # seconds between requests: at most 2 a second
RETRIES = 3
TIMEOUT = 20
RUN_SECONDS = 100 * 60  # one run's budget; the cache makes the next run resume
MAX_FAILURES = 20  # failed days in one run before it stops
LEVEL = 0.95
MINUTE_BANDS = ((0, 15), (15, 30), (30, 45), (45, 60), (60, 75), (75, 200))
CHECKPOINTS = (15, 30, 45, 60, 75)
# Costing a priced trail-first test (historical snapshots are 5 minutes apart from late 2022).
COST_YEARS = (2022, 2023, 2024)
SNAP_AFTER_GOAL = 5  # minutes after the opening goal
BREAK = 17  # minutes a second-half goal adds to the wall clock (half-time and stoppage)
HIST_COST = 10


# ---------- fetch ----------


def day_path(raw_dir: Path, slug: str, day: str) -> Path:
    return Path(raw_dir) / CACHE / slug / f"{day}.json"


def compact(body: dict | None) -> dict:
    """The scoreboard reduced to what the study reads: per event its id, kickoff, teams,
    ids, status, final score and goals as [clock, team id, own goal, penalty]."""
    out = []
    for e in (body or {}).get("events") or []:
        if not isinstance(e, dict):
            continue
        comp = (e.get("competitions") or [{}])[0] or {}
        sides = {c.get("homeAway"): c for c in comp.get("competitors") or [] if isinstance(c, dict)}
        h, a = sides.get("home") or {}, sides.get("away") or {}
        goals = []
        for d in comp.get("details") or []:
            if not isinstance(d, dict) or not d.get("scoringPlay") or d.get("shootout"):
                continue
            goals.append(
                [
                    (d.get("clock") or {}).get("displayValue"),
                    str((d.get("team") or {}).get("id", "")),
                    bool(d.get("ownGoal")),
                    bool(d.get("penaltyKick")),
                ]
            )
        status = (comp.get("status") or e.get("status") or {}).get("type") or {}
        out.append(
            {
                "id": e.get("id"),
                "date": e.get("date"),
                "home": (h.get("team") or {}).get("displayName"),
                "away": (a.get("team") or {}).get("displayName"),
                "home_id": str((h.get("team") or {}).get("id", "")),
                "away_id": str((a.get("team") or {}).get("id", "")),
                "completed": bool(status.get("completed")),
                "state": status.get("name"),
                "score": [h.get("score"), a.get("score")],
                "goals": goals,
            }
        )
    return {"events": out}


def match_days(league: str, year: int, raw_dir: Path = RAW_DIR) -> list[str]:
    """The dates (YYYYMMDD) football-data lists a match on, before the 2025/26 holdout."""
    df = load_season(league, year, raw_dir)
    df = df[df["date"] < cb.HOLDOUT_START]
    return sorted({d.strftime("%Y%m%d") for d in df["date"]})


def _get(url: str, params: dict, get: Callable) -> tuple[dict | None, str]:
    """(body, outcome): outcome "ok", "client" (4xx: no retry) or "failed"."""
    for attempt in range(RETRIES + 1):
        try:
            r = get(url, params=params, timeout=TIMEOUT)
            if r.status_code == 200:
                return r.json(), "ok"
            if r.status_code < 500:
                return None, "client"
        except (requests.RequestException, ValueError):
            pass
        if attempt < RETRIES:
            time.sleep(PAUSE * 2 * (attempt + 1))
    return None, "failed"


def fetch(
    leagues=LEAGUES,
    years=YEARS,
    raw_dir: Path = RAW_DIR,
    seconds: float = RUN_SECONDS,
    get: Callable = requests.get,
    pause: float = PAUSE,
    clock: Callable = time.monotonic,
    days: dict | None = None,
) -> dict:
    """Fetch every uncached match day; counts only. Stops at the time budget or after
    MAX_FAILURES failed days (both resumable: cached days are never asked again)."""
    start = clock()
    res: dict = {"seasons": {}, "requests": 0, "failed": 0, "stopped": None}
    for lg in leagues:
        slug = SLUGS[lg]
        for y in years:
            key = f"{lg} {y}/{str(y + 1)[-2:]}"
            todo = days[(lg, y)] if days is not None else match_days(lg, y, raw_dir)
            row = {"days": len(todo), "cached": 0, "fetched": 0, "failed": 0, "events": 0}
            row["with_goals"] = 0
            for day in todo:
                path = day_path(raw_dir, slug, day)
                if path.exists():
                    row["cached"] += 1
                    continue
                if res["stopped"] is None and clock() - start > seconds:
                    res["stopped"] = f"time budget ({seconds / 60:.0f} min) used"
                if res["stopped"] is None and res["failed"] >= MAX_FAILURES:
                    res["stopped"] = f"{MAX_FAILURES} failed days"
                if res["stopped"]:
                    continue
                body, outcome = _get(BASE.format(slug=slug) + "/scoreboard", {"dates": day}, get)
                res["requests"] += 1
                if pause:
                    time.sleep(pause)
                if body is None:
                    row["failed"] += 1
                    res["failed"] += outcome == "failed"
                    continue
                rec = compact(body)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(rec))
                row["fetched"] += 1
                row["events"] += len(rec["events"])
                row["with_goals"] += sum(1 for e in rec["events"] if e["goals"])
            res["seasons"][key] = row
    return res


# ---------- joining ESPN to football-data ----------


def _minute_half(clock) -> tuple[int, int] | None:
    """(minute, half) of a clock like "31'", "45'+2'" (first half) or "90'+4'"."""
    m = _minute(clock)
    if m is None:
        return None
    return m, 1 if m <= 45 else 2


def season_matches(league: str, year: int, raw_dir: Path = RAW_DIR) -> pd.DataFrame:
    """football-data favourites (phase 1's), with the UTC kickoff."""
    df = load_season(league, year, raw_dir)
    df = df[df["date"] < cb.HOLDOUT_START].reset_index(drop=True)
    return cb.favourites(df)


def espn_events(league: str, days: list[str], raw_dir: Path = RAW_DIR) -> list[dict]:
    out = []
    for day in days:
        path = day_path(raw_dir, SLUGS[league], day)
        if path.exists():
            out += json.loads(path.read_text()).get("events", [])
    return out


def join(fav: pd.DataFrame, events: list[dict]) -> tuple[pd.DataFrame, dict]:
    """Attach each favourite match's goal list [(minute, half, side)] from ESPN; check the
    90-minute and half-time scores against football-data and drop mismatches."""
    known = set(fav["home"]) | set(fav["away"])
    by_pair: dict[tuple[str, str], list[dict]] = {}
    for e in events:
        if not (e.get("home") and e.get("away")):
            continue
        k = (team_name(e["home"], known), team_name(e["away"], known))
        by_pair.setdefault(k, []).append(e)
    counts = {"matches": len(fav), "joined": 0, "no_event": 0, "bad_goal": 0, "mismatch": 0}
    goals_col = []
    for r in fav.itertuples():
        hit = None
        for e in by_pair.get((r.home, r.away), []):
            d = pd.Timestamp(e["date"]).tz_convert(None).normalize()
            if abs((d - r.date.normalize()).days) <= 1:
                hit = e
                break
        if hit is None:
            counts["no_event"] += 1
            goals_col.append(None)
            continue
        goals, ok = [], True
        for clock, team, _own, _pen in hit.get("goals") or []:
            mh = _minute_half(clock)
            if mh is None or team not in (hit["home_id"], hit["away_id"]):
                ok = False
                break
            goals.append((mh[0], mh[1], "home" if team == hit["home_id"] else "away"))
        if not ok:
            counts["bad_goal"] += 1
            goals_col.append(None)
            continue
        goals.sort(key=lambda g: (g[1], g[0]))  # stable: ESPN's order within a minute
        ft = [sum(g[2] == s and g[0] <= 90 for g in goals) for s in ("home", "away")]
        ht = [sum(g[2] == s and g[1] == 1 for g in goals) for s in ("home", "away")]
        if ft != [r.fthg, r.ftag] or (
            np.isfinite(r.hthg) and np.isfinite(r.htag) and ht != [r.hthg, r.htag]
        ):
            counts["mismatch"] += 1
            goals_col.append(None)
            continue
        counts["joined"] += 1
        goals_col.append([g for g in goals if g[0] <= 90])
    out = fav.copy()
    out["goals"] = goals_col
    return out[out["goals"].notna()].reset_index(drop=True), counts


# ---------- events ----------


def fav_side(r) -> str:
    return "home" if r["fav_home"] else "away"


def score_at(goals, minute: int, half_one_only: bool = False) -> tuple[int, int]:
    """(favourite-side, underdog-side) is computed by the caller; here (home, away) goals by
    `minute` (a 45 includes first-half stoppage time)."""
    sel = [g for g in goals if (g[1] == 1 if half_one_only else g[0] <= minute)]
    return sum(g[2] == "home" for g in sel), sum(g[2] == "away" for g in sel)


def minute_band(minute: int) -> str:
    """ "0-15" (up to 15'), "15-30", ..., "75+"; a 45 (first-half stoppage) is "30-45"."""
    for lo, hi in MINUTE_BANDS:
        if minute <= hi:
            return f"{lo}-{hi}" if hi < 200 else f"{lo}+"
    return f"{MINUTE_BANDS[-1][0]}+"


def trail_first(m: pd.DataFrame) -> pd.DataFrame:
    """Matches whose opening goal is the underdog's: minute, minute band, the half-time
    state from the favourite's side (behind / level / ahead) and phase 1's model inputs."""
    rows = []
    for r in m.to_dict("records"):
        if not r["goals"]:
            continue
        first = r["goals"][0]
        side = fav_side(r)
        if first[2] == side:
            continue
        h, a = score_at(r["goals"], 45, half_one_only=True)
        f_ht, d_ht = (h, a) if side == "home" else (a, h)
        rows.append(
            {
                **r,
                "minute": first[0],
                "mband": minute_band(first[0]),
                "deficit": 1,
                "left": (90 - min(first[0], 90)) / 90,
                "ht_state": "behind" if f_ht < d_ht else "level" if f_ht == d_ht else "ahead",
                "second_half": first[1] == 2,
            }
        )
    return pd.DataFrame(rows)


def behind_at(m: pd.DataFrame, minute: int) -> pd.DataFrame:
    """Favourites behind at `minute` (45 = half-time, stoppage included), with the deficit."""
    rows = []
    for r in m.to_dict("records"):
        h, a = score_at(r["goals"], minute, half_one_only=minute == 45)
        f, d = (h, a) if fav_side(r) == "home" else (a, h)
        if d > f:
            rows.append({**r, "deficit": d - f, "left": (90 - minute) / 90, "minute": minute})
    return pd.DataFrame(rows)


def band_of(p: float) -> str:
    return next(cb.band_name(lo, hi) for lo, hi in cb.BANDS if lo <= p < hi)


def cells(ev: pd.DataFrame, by: str) -> dict:
    """Rates and model residuals (match-resampled 95% ranges) per value of `by`."""
    if ev.empty:
        return {}
    ev = cb.add_predictions(ev)
    bench = cb.bench_seasons(ev)
    out = {}
    for k in sorted(ev[by].astype(str).unique()):
        out[k] = cb.cell(ev[ev[by].astype(str) == k], bench[bench[by].astype(str) == k], LEVEL)
    out["all"] = cb.cell(ev, bench, LEVEL)
    return out


# ---------- costing a priced trail-first test ----------


def trail_first_costing(tf: pd.DataFrame, ht_slots: dict) -> dict:
    """One snapshot SNAP_AFTER_GOAL minutes after each opening goal (wall clock = kickoff +
    minute, + BREAK in the second half), goals in one league and 5-minute bucket sharing a
    snapshot; per league and season, beside the half-time design's slots."""
    x = tf[tf["season_start"].isin(COST_YEARS) & tf["kickoff"].notna()].copy()
    out = {}
    if x.empty:
        return out
    x["at"] = [
        r.kickoff
        + pd.Timedelta(minutes=r.minute + SNAP_AFTER_GOAL + (BREAK if r.second_half else 0))
        for r in x.itertuples()
    ]
    x["bucket"] = x["at"].dt.floor("5min")
    for (lg, y), g in x.groupby(["league", "season_start"]):
        snaps = g["bucket"].nunique()
        key = f"{lg} {y}/{str(y + 1)[-2:]}"
        out[key] = {
            "trail_first": len(g),
            "snapshots": int(snaps),
            "credits": int(snaps) * HIST_COST,
            "half_time_slots": ht_slots.get(key),
        }
    return out


# ---------- the run ----------


def build(leagues=LEAGUES, years=YEARS, raw_dir: Path = RAW_DIR) -> tuple[pd.DataFrame, dict]:
    frames, counts = [], {}
    for lg in leagues:
        for y in years:
            key = f"{lg} {y}/{str(y + 1)[-2:]}"
            try:
                fav = season_matches(lg, y, raw_dir)
            except Exception as exc:  # noqa: BLE001 - a missing file is reported, not fatal
                counts[key] = {"error": str(exc)[:120]}
                continue
            days = sorted({d.strftime("%Y%m%d") for d in fav["date"]})
            ev = espn_events(lg, days, raw_dir)
            j, c = join(fav, ev)
            c["days_cached"] = sum(day_path(raw_dir, SLUGS[lg], d).exists() for d in days)
            c["days"] = len(days)
            counts[key] = c
            frames.append(j)
    m = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if not m.empty:
        m["fband"] = m["p_fav"].map(band_of)
    return m, counts


def analyse(m: pd.DataFrame) -> dict:
    tf = trail_first(m)
    total = len(m)
    res: dict = {"matches": total, "trail_first": len(tf)}
    if tf.empty:
        return res
    tf["fband"] = tf["p_fav"].map(band_of)
    res["trail_first_share"] = round(len(tf) / total, 4)
    res["trail_first_by_minute"] = cells(tf, "mband")
    res["trail_first_by_band"] = cells(tf, "fband")
    res["trail_first_by_league"] = cells(tf, "league")
    first_half = tf[~tf["second_half"]]
    res["trail_first_half_time_state"] = {
        st: {**cb.rates(g, LEVEL), "share": round(len(g) / len(first_half), 4)}
        for st, g in first_half.groupby("ht_state")
    }
    res["timing_curve"] = {
        lg: {
            "trail_first": int(len(g)),
            "share_of_matches": round(len(g) / max(1, int((m["league"] == lg).sum())), 4),
            "by_minute": {b: int((g["mband"] == b).sum()) for b in sorted(g["mband"].unique())},
            "median_minute": float(g["minute"].median()),
        }
        for lg, g in tf.groupby("league")
    }
    behind = {}
    for minute in CHECKPOINTS:
        b = behind_at(m, minute)
        if b.empty:
            continue
        b["fband"] = b["p_fav"].map(band_of)
        one, two = b[b["deficit"] == 1], b[b["deficit"] >= 2]
        behind[str(minute)] = {
            "share_behind": round(len(b) / total, 4),
            "one_down": cells(one, "fband") if len(one) else {},
            "two_down": cells(two, "fband") if len(two) else {},
        }
    res["behind_at"] = behind
    return res


def ht_slots_from_plan(leagues=LEAGUES) -> dict:
    from soccer_stats.lab.comeback_plan import season_plan

    out = {}
    for lg in leagues:
        for y in COST_YEARS:
            try:
                p = season_plan(lg, y)
                out[f"{lg} {y}/{str(y + 1)[-2:]}"] = p["slots_to_buy"]
            except Exception:  # noqa: BLE001
                continue
    return out


def _fmt(c: dict) -> str:
    if not c or "win" not in c:
        return "-"
    s = f"n={c['matches']} win {c['win']:.1%} draw {c['draw']:.1%}"
    if "win_predicted" in c:
        lo, hi = c["win_residual_range"]
        s += (
            f" | model win {c['win_predicted']:.1%}: {c['win_residual']:+.1%}"
            f" ({lo:+.1%}..{hi:+.1%})"
        )
    return s


def report(counts: dict, res: dict, costing: dict) -> str:
    lines = ["== Goal timelines from ESPN (exploratory) =="]
    for k, c in counts.items():
        if "error" in c:
            lines.append(f"  {k}: {c['error']}")
            continue
        lines.append(
            f"  {k}: days cached {c['days_cached']}/{c['days']}, favourites {c['matches']}, "
            f"joined {c['joined']} (no event {c['no_event']}, bad goal {c['bad_goal']}, "
            f"score mismatch {c['mismatch']})"
        )
    lines.append(f"Matches {res['matches']}, trail first {res['trail_first']}")
    for key in ("trail_first_by_minute", "trail_first_by_band", "trail_first_by_league"):
        lines.append(f"{key}:")
        for k, c in res.get(key, {}).items():
            lines.append(f"  {k:10s} {_fmt(c)}")
    lines.append("trail first, half-time state:")
    for k, c in res.get("trail_first_half_time_state", {}).items():
        lines.append(f"  {k:10s} share {c['share']:.1%} {_fmt(c)}")
    for minute, b in res.get("behind_at", {}).items():
        lines.append(f"behind at {minute}' (share {b['share_behind']:.1%}):")
        lines.append(f"  one down {_fmt(b['one_down'].get('all', {}))}")
        lines.append(f"  two+ down {_fmt(b['two_down'].get('all', {}))}")
    lines.append("costing a priced trail-first test:")
    for k, c in costing.items():
        lines.append(
            f"  {k}: {c['trail_first']} matches, {c['snapshots']} snapshots, {c['credits']} credits"
            f" (half-time design {c['half_time_slots']} slots)"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--fetch", action="store_true", help="fetch uncached days (counts only)")
    p.add_argument("--minutes", type=float, default=RUN_SECONDS / 60, help="run time budget")
    p.add_argument("--json", default="")
    args = p.parse_args(argv)
    if args.fetch:
        res = fetch(seconds=args.minutes * 60)
        for k, row in res["seasons"].items():
            print(f"  {k}: {json.dumps(row)}")
        print(f"requests {res['requests']}, failed {res['failed']}, stopped: {res['stopped']}")
        print("TIMELINES_FETCH_JSON " + json.dumps(res))
        return
    m, counts = build()
    res = analyse(m)
    costing = trail_first_costing(trail_first(m), ht_slots_from_plan()) if len(m) else {}
    print(report(counts, res, costing))
    out = {"counts": counts, "result": res, "costing": costing}
    print("TIMELINES_JSON " + json.dumps(out, default=str))
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    main()
