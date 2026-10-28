"""Coverage of a bought pre-match cup design (H2P, H2U): counts only, never a result (lead, 28
Oct). Reads the frozen slot file, the fetch's call record and the bought bodies in
`comeback-odds-*`. A pre-match snapshot holds prices, not scores, so nothing here can read an
outcome.

* Snapshots: bought, the cost each call billed, how many held any event, and how many held an
  event kicking off within 30 minutes of the slot.
* Matches: found when an event within 30 minutes of the kickoff has both names close to ESPN's
  (normalised names equal, one inside the other, or a close spelling), home and away either
  way round (finals are at neutral grounds). Then whether Pinnacle and DraftKings quote a full
  1X2. By cup, by season and in total.
* With `--trailing` (lead, 28 Oct; H2's sample size): among the priced matches, how many had a
  favourite priced 75%+ (Shin-fair, Pinnacle's full 1X2, else DraftKings') and how many of
  those trailed at half-time. The half-time score comes from the cached ESPN days of
  `heavy_more` (the first linescore, else goals before minute 46), the same source that chose
  the slots; the full-time score is never read. The Champions League seasons whose closes are
  held (`heavy_more.HELD_UCL`) are counted from `odds_history/UCL_closes.jsonl` on the ESPN
  matches not level at half-time.
"""

from __future__ import annotations

import argparse
import difflib
import json
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

from soccer_stats import cups
from soccer_stats.cups import norm
from soccer_stats.data import RAW_DIR
from soccer_stats.lab import comeback_fetch as cf
from soccer_stats.lab import comebacks as cb
from soccer_stats.lab import comebacks2 as c2
from soccer_stats.lab import heavy_more as hm

KICKOFF_TOL = 30  # minutes between ESPN's kickoff and the event's
CLOSE_NAME = 0.75


def _prefixes(short: list[str], long: list[str]) -> bool:
    """Each word of the shorter name starts a word of the longer one, in order ("man city",
    "manchester city")."""
    i = 0
    for w in long:
        if i < len(short) and w.startswith(short[i]):
            i += 1
    return i == len(short)


def same_team(a: str, b: str) -> bool:
    x, y = norm(a), norm(b)
    if not x or not y:
        return False
    if x == y or x in y or y in x:
        return True
    xs, ys = x.split(), y.split()
    if (len(xs) <= len(ys) and _prefixes(xs, ys)) or (len(ys) <= len(xs) and _prefixes(ys, xs)):
        return True
    return difflib.SequenceMatcher(None, x, y).ratio() >= CLOSE_NAME


def find(events: list[dict], home: str, away: str, kickoff: pd.Timestamp) -> dict | None:
    hit = find_side(events, home, away, kickoff)
    return hit[0] if hit else None


def find_side(events: list[dict], home: str, away: str, kickoff: pd.Timestamp):
    """(event, swapped): swapped when the feed lists ESPN's home side as away."""
    for ev in events:
        t = c2._ts(ev.get("commence_time"))
        if t is None or abs((t - kickoff).total_seconds()) > KICKOFF_TOL * 60:
            continue
        h, a = ev.get("home_team", ""), ev.get("away_team", "")
        if same_team(home, h) and same_team(away, a):
            return ev, False
        if same_team(home, a) and same_team(away, h):
            return ev, True
    return None


def full_1x2(ev: dict, book: str) -> bool:
    q = c2.quote(ev, book)
    return bool(q) and set(q["prices"]) == {"home", "draw", "away"}


def season_of(kickoff: str) -> str:
    t = pd.Timestamp(kickoff)
    y = t.year if t.month >= 7 else t.year - 1
    return f"{y}/{str(y + 1)[-2:]}"


def coverage(plan: dict, cache_dir: Path) -> dict:
    calls = {(c["sport"], c["request_at"]): c for c in cf.read_calls(cache_dir)}
    snaps = Counter()
    costs = Counter()
    groups: dict[tuple, Counter] = defaultdict(Counter)
    bodies: dict[tuple, list] = {}
    for s in plan["slots"]:
        key = (s["sport"], s["request_at"])
        call = calls.get(key)
        if call is not None:
            costs[str(call.get("cost"))] += 1
        path = cf.body_path(cache_dir, s)
        if not path.exists():
            snaps["no_body"] += 1
            continue
        events = json.loads(path.read_text()).get("data") or []
        bodies[key] = events
        snaps["bought"] += 1
        snaps["any_events"] += bool(events)
        ko = pd.Timestamp(s["kickoff"])
        near = [
            e for e in events
            if (t := c2._ts(e.get("commence_time"))) is not None
            and abs((t - ko).total_seconds()) <= KICKOFF_TOL * 60
        ]  # fmt: skip
        snaps["events_at_kickoff"] += bool(near)
    for s in plan["kickoff_slots"]:
        events = bodies.get((s["sport"], s["request_at"]), [])
        ko = pd.Timestamp(s["kickoff"])
        for m in s["matches"]:
            ev = find(events, m["home"], m["away"], ko)
            near = any(
                (t := c2._ts(e.get("commence_time"))) is not None
                and abs((t - ko).total_seconds()) <= KICKOFF_TOL * 60
                for e in events
            )
            pin = bool(ev) and full_1x2(ev, "pinnacle")
            dk = bool(ev) and full_1x2(ev, "draftkings")
            row = {
                "matches": 1,
                "events_at_kickoff": int(near),
                "found": int(bool(ev)),
                "pinnacle": int(pin),
                "draftkings": int(dk),
                "either": int(pin or dk),
            }
            for g in (("all",), ("cup", s["league"]), ("season", season_of(s["kickoff"])),
                      ("cup_season", s["league"], season_of(s["kickoff"]))):  # fmt: skip
                groups[g].update(row)
    out = {
        "design": plan.get("design"),
        "snapshots": dict(snaps),
        "cost_per_call": dict(costs),
        "matches": dict(groups[("all",)]),
        "by_cup": {g[1]: dict(v) for g, v in sorted(groups.items()) if g[0] == "cup"},
        "by_season": {g[1]: dict(v) for g, v in sorted(groups.items()) if g[0] == "season"},
        "by_cup_season": {
            f"{g[1]} {g[2]}": dict(v) for g, v in sorted(groups.items()) if g[0] == "cup_season"
        },
    }
    return out


# ---------- 75%+ favourites trailing at half-time (no full-time score) ----------

BOOKS = ("pinnacle", "draftkings")


def half_times(espn_dir: Path, slug: str) -> dict[tuple, dict]:
    """(home, away, kickoff) -> {id, ht, completed} from the cached ESPN days of one cup. Reads
    names, kickoff, status and the half-time score only."""
    out = {}
    for path in sorted((Path(espn_dir) / cups.CACHE / "espn").glob(f"{slug}_*.json")):
        if path.name.endswith(".recent.json"):
            continue
        for e in json.loads(path.read_text()).get("events") or []:
            if not isinstance(e, dict) or not e.get("id") or not e.get("date"):
                continue
            comp = (e.get("competitions") or [{}])[0] or {}
            sides = {c.get("homeAway"): c for c in comp.get("competitors") or []
                     if isinstance(c, dict)}  # fmt: skip
            h, a = sides.get("home") or {}, sides.get("away") or {}
            hn, an = ((c.get("team") or {}).get("displayName") for c in (h, a))
            if not hn or not an:
                continue
            ko = f"{cups._ts(e['date']):%Y-%m-%dT%H:%MZ}"
            done = bool(((e.get("status") or {}).get("type") or {}).get("completed"))
            out[(hn, an, ko)] = {"id": str(e["id"]), "ht": hm.half_time(comp, h, a),
                                 "completed": done}  # fmt: skip
    return out


def favourite(prices: dict) -> tuple[str, float]:
    """The side with the larger Shin-fair chance, in the order of `prices` (home first)."""
    f = cb._fair(np.array([prices["home"], prices["draw"], prices["away"]], float))
    return ("home", float(f[0])) if f[0] >= f[2] else ("away", float(f[2]))


def priced(ev: dict, swapped: bool) -> tuple[str, dict] | None:
    """(book, prices in ESPN's home/away order): Pinnacle's full 1X2, else DraftKings'."""
    for book in BOOKS:
        q = c2.quote(ev, book)
        if q and set(q["prices"]) == {"home", "draw", "away"}:
            p = q["prices"]
            return book, ({"home": p["away"], "draw": p["draw"], "away": p["home"]}
                          if swapped else dict(p))  # fmt: skip
    return None


def ucl_closes(path: Path | None) -> dict[str, dict]:
    """espn_id -> {book: prices}, ESPN's home/away order (cup_odds wrote them that way)."""
    out: dict[str, dict] = defaultdict(dict)
    if not path or not Path(path).exists():
        return out
    for line in Path(path).read_text().splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        ps = [r.get("price_home"), r.get("price_draw"), r.get("price_away")]
        if r.get("book") in BOOKS and all(isinstance(x, (int, float)) and x > 1 for x in ps):
            out[str(r.get("espn_id"))][r["book"]] = dict(zip(("home", "draw", "away"), ps,
                                                             strict=True))  # fmt: skip
    return out


def _row(price: tuple[str, dict] | None, ht: list[int] | None) -> dict:
    row = {"matches": 1, "priced": 0, "pinnacle": 0, "no_half_time": int(ht is None),
           "heavy": 0, "heavy_trailing": 0}  # fmt: skip
    if price is None:
        return row
    row["priced"], row["pinnacle"] = 1, int(price[0] == "pinnacle")
    side, chance = favourite(price[1])
    if chance >= hm.MIN_FAV:
        row["heavy"] = 1
        row["heavy_trailing"] = int(hm.trails(ht, side))
    return row


def trailing(plan: dict, cache_dir: Path, espn_dir: Path, closes: Path | None = None,
             now: pd.Timestamp | None = None) -> dict:  # fmt: skip
    """Counts per cup and season: matches, priced, heavy (75%+), heavy and trailing at HT."""
    groups: dict[tuple, Counter] = defaultdict(Counter)
    idx: dict[str, dict] = {}
    found: list[dict] = []  # the trailing heavy favourites themselves (H2's sample)

    def add(cup: str, kickoff: str, row: dict, home: str = "", away: str = "",
            price: tuple | None = None, e: dict | None = None) -> None:  # fmt: skip
        y = season_of(kickoff)
        for g in (("all",), ("cup", cup), ("season", y), ("cup_season", cup, y)):
            groups[g].update(row)
        if row["heavy_trailing"]:
            side, chance = favourite(price[1])
            found.append({"league": cup, "kickoff": kickoff, "home": home, "away": away,
                          "espn_id": (e or {}).get("id"), "favourite": side,
                          "p_fav": round(chance, 4), "ht": (e or {}).get("ht"),
                          "price": price[0]})  # fmt: skip

    for s in plan["kickoff_slots"]:
        slug = hm.CUPS[s["league"]][1]
        if slug not in idx:
            idx[slug] = half_times(espn_dir, slug)
        path = cf.body_path(cache_dir, s)
        body = json.loads(path.read_text()) if path.exists() else {}
        events = body.get("data") or []
        ko = pd.Timestamp(s["kickoff"])
        for m in s["matches"]:
            hit = find_side(events, m["home"], m["away"], ko)
            e = idx[slug].get((m["home"], m["away"], s["kickoff"])) or {}
            price = priced(*hit) if hit else None
            add(s["league"], s["kickoff"], _row(price, e.get("ht")), m["home"], m["away"],
                price, e)  # fmt: skip
    if plan.get("design") == "H2U":
        now = now or pd.Timestamp.now(tz="UTC")
        held = ucl_closes(closes)
        slug = hm.CUPS["UCL"][1]
        idx.setdefault(slug, half_times(espn_dir, slug))
        for (home, away, ko), e in sorted(idx[slug].items(), key=lambda kv: kv[0][2]):
            if ("UCL", season_of(ko)) not in hm.HELD_UCL or not e["completed"]:
                continue
            if not e["ht"] or e["ht"][0] == e["ht"][1] or pd.Timestamp(ko) >= now:
                continue
            books = held.get(e["id"], {})
            price = next(((b, books[b]) for b in BOOKS if b in books), None)
            add("UCL", ko, {**_row(price, e["ht"]), "held_close": 1}, home, away, price, e)
    return {
        "design": plan.get("design"),
        "min_fav": hm.MIN_FAV,
        "all": dict(groups[("all",)]),
        "by_cup": {g[1]: dict(v) for g, v in sorted(groups.items()) if g[0] == "cup"},
        "by_season": {g[1]: dict(v) for g, v in sorted(groups.items()) if g[0] == "season"},
        "by_cup_season": {
            f"{g[1]} {g[2]}": dict(v) for g, v in sorted(groups.items()) if g[0] == "cup_season"
        },
        "trailing_list": sorted(found, key=lambda r: (r["kickoff"], r["league"], r["home"])),
    }


def trailing_report(res: dict) -> str:
    def line(k, v):
        return (f"    {k}: {v.get('matches', 0)} matches, priced {v.get('priced', 0)} (Pinnacle "
                f"{v.get('pinnacle', 0)}), favourite 75%+ {v.get('heavy', 0)}, of those "
                f"trailing at HT {v.get('heavy_trailing', 0)}"
                + (f", held UCL closes {v['held_close']}" if v.get("held_close") else "")
                )  # fmt: skip

    lines = [
        f"== 75%+ favourites trailing at half-time ({res['design']}) ==",
        line("all", res["all"]),
    ]
    for name in ("by_cup", "by_season", "by_cup_season"):
        lines.append(f"  {name}:")
        lines += [line(k, v) for k, v in res[name].items()]
    return "\n".join(lines)


def report(res: dict) -> str:
    s, m = res["snapshots"], res["matches"]
    lines = [
        f"== Pre-match coverage ({res['design']}) ==",
        f"  snapshots: {s.get('bought', 0)} bought ({s.get('no_body', 0)} missing), "
        f"{s.get('any_events', 0)} with any event, {s.get('events_at_kickoff', 0)} with an "
        f"event at the slot's kickoff; billed per call: {res['cost_per_call']}",
        f"  matches: {m.get('matches', 0)}, an event at kickoff {m.get('events_at_kickoff', 0)}, "
        f"found {m.get('found', 0)}, Pinnacle 1X2 "
        f"{m.get('pinnacle', 0)}, DraftKings 1X2 {m.get('draftkings', 0)}, either "
        f"{m.get('either', 0)}",
    ]
    for name in ("by_cup", "by_season", "by_cup_season"):
        lines.append(f"  {name}:")
        for k, v in res[name].items():
            lines.append(
                f"    {k}: {v['matches']} matches, at kickoff {v['events_at_kickoff']}, found "
                f"{v['found']}, Pinnacle "
                f"{v['pinnacle']}, DraftKings {v['draftkings']}, either {v['either']}"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--slots", default="H2P", help="design H2P or H2U, or a slot file")
    p.add_argument("--cache-dir", default="", help="the bought bodies (default raw dir)")
    p.add_argument(
        "--trailing", action="store_true", help="also count 75%% favourites trailing at HT"
    )
    p.add_argument("--espn-dir", default="", help="heavy_more's ESPN days (default raw dir)")
    p.add_argument("--closes", default="", help="UCL_closes.jsonl (held Champions League closes)")
    args = p.parse_args(argv)
    cache = Path(args.cache_dir) if args.cache_dir else Path(RAW_DIR) / cf.CACHE
    plan = json.loads(cf.slots_path(args.slots).read_text())
    res = coverage(plan, cache)
    print(report(res))
    print("PREMATCH_COVERAGE_JSON " + json.dumps(res))
    if args.trailing:
        espn = Path(args.espn_dir) if args.espn_dir else Path(RAW_DIR) / hm.SUBDIR
        tr = trailing(plan, cache, espn, Path(args.closes) if args.closes else None)
        print(trailing_report(tr))
        print("PREMATCH_TRAILING_JSON " + json.dumps({k: v for k, v in tr.items()
                                                      if k != "trailing_list"}))  # fmt: skip


if __name__ == "__main__":
    main()
