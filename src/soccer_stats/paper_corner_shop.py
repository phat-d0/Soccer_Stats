"""Corner line shop vs Pinnacle: a paper track in Team corners (owner's decision of 24 Nov 2026,
via the lead; pre-registered 8 Nov in docs/totals.md "Corner line shop vs Pinnacle:
pre-registration", before any shop-book price was logged).

No model. At the look snapshot (18-30 h before kickoff), the one corner call that asks every
book (corners_live.LOG_BOOKS, logged since 8 Nov), for each team half line Pinnacle quotes
two-sided in that call:

* gap per side = the book's price x Pinnacle's Shin-fair chance for that side - 1;
* books that can be bet: DraftKings, FanDuel, BetMGM and BetRivers (BET_BOOKS, also the tie
  order); Bovada is logged for description only (its gap is recorded, never bet);
* both quotes from the same call, each updated within 3 h before the download;
* entry at the look when the best gap reaches T, at that price, $10 at the book named; at most
  one bet per team per match: the larger gap (ties: the lower line);
* T = 2% is judged (KEY); T = 1% is a descriptive shadow with its own entries (SHADOW_KEY).

Close: Pinnacle's Shin-fair close at the same team and line (a real close, PR #258), CLV =
entry x fair - 1 (`clv_pinnacle`; none and counted as missing without a two-sided close); the
entry book's own close (`clv_book`) is descriptive. Settled like the corner tracks. Judged once
(paper_corner_lean.judged): 300 bets opened from 8 Nov, each with a final close, or on
31 May 2027 (under 300 = inconclusive); pass = the 95% range of mean CLV above 0 (matches
resampled, 2,000 draws, seed 0).

Counted from 8 Nov (JUDGED_FROM), as pre-registered: the rules were frozen before any
shop-book corner price was logged and nothing is fitted, so applying them to the log from the
first look call with those books is out of sample (lead, 24 Nov; only prices were read before,
on 20 and 22 Nov). Entries the first run opens from looks logged before `defined_at` (the build
date, kept for display) carry `from_log: true` and count toward the 300; there is no backcast.
The 1% shadow is descriptive. Own ledger (paper_trades/<code>_cornershop_<season>.jsonl),
outside the corner tracks' record. No API calls.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd

from soccer_stats import paper_corner_lean as pcl
from soccer_stats import paper_lineshop as plsh
from soccer_stats import trades as tr

KEY = "corner_shop"
LABEL = "Corner line shop vs Pinnacle"
SHADOW_KEY = "corner_shop_1pct"
SHADOW_LABEL = "Corner line shop vs Pinnacle, 1% gap (descriptive)"
BET_TYPE = "corner_shop"
FILE_TAG = "cornershop"  # <code>_cornershop_<season>.jsonl (never matches *_corners_*)
ID_TAG = {KEY: "cornershop", SHADOW_KEY: "cornershop1"}
THRESHOLDS = {KEY: 0.02, SHADOW_KEY: 0.01}
PIN = "pinnacle"
BET_BOOKS = ("draftkings", "fanduel", "betmgm", "betrivers")  # tie order too
SEEN_BOOKS = ("bovada",)  # logged and recorded, never bet
BOOK_NAMES = {
    "draftkings": "DraftKings",
    "fanduel": "FanDuel",
    "betmgm": "BetMGM",
    "betrivers": "BetRivers",
    "bovada": "Bovada",
    "pinnacle": "Pinnacle",
}
FRESH = pd.Timedelta(hours=3)
STAKE = 10.0
# The shop books' first logged corner call was 8 Nov 11:05 UTC: the pre-registered start of
# the count (no backcast; lead, 24 Nov).
JUDGED_FROM = "2026-11-08T00:00:00+00:00"
DEFINED_FILE = "corner_shop_defined.json"
VERDICT_FILE = "corner_shop_verdict.json"
JUDGE_BETS = 300
DEADLINE = pcl.DEADLINE
UPDATE_FIELDS = {
    "book_close_odds",
    "book_close_prices",
    "book_close_fetched_at",
    "book_close_minutes_before",
    "clv_book",
}

RULE_NOTE = (
    "No model. At the look (about a day before kickoff), for each team corner line Pinnacle "
    "prices both ways, the best price among DraftKings, FanDuel, BetMGM and BetRivers in the "
    "same call is compared with Pinnacle's margin-free (Shin) chance for that side; when best "
    "x fair - 1 reaches {t:.0%}, one $10 paper bet opens at the book named, one per team per "
    "match. Both quotes must be under 3 hours old. Judged once at 300 bets on CLV against "
    "Pinnacle's close (pre-registered 8 Nov in docs/totals.md, 'Corner line shop vs Pinnacle')."
)
SHADOW_NOTE = (
    "Shadow at a 1% gap: the same rule at a lower bar, tracked beside the 2% line. It places "
    "no bets of its own and is never judged; it enters on more lines."
)
FROM_LOG_NOTE = (
    "Counted from {started}, as pre-registered: the rule was written on 8 Nov before any of "
    "these books' corner prices were logged, and it fits nothing, so the {n} bets opened from "
    "looks logged before the track was built ({defined}) are part of the test, not a backcast. "
    "They were opened from the look prices logged at the time."
)


def rule(key: str = KEY) -> dict:
    t = THRESHOLDS[key]
    return {
        "key": key,
        "rule": "corner_shop",
        "threshold": t,
        "books": list(BET_BOOKS),
        "seen_books": list(SEEN_BOOKS),
        "reference": PIN,
        "fresh_hours": FRESH / pd.Timedelta(hours=1),
        "stake": STAKE,
        "p_source": "pinnacle_fair",
        "clv_field": "clv_pinnacle",
        "source": "preregistered",
        "judge_bets": JUDGE_BETS if key == KEY else None,
        "deadline": DEADLINE if key == KEY else None,
        "note": RULE_NOTE.format(t=t) if key == KEY else SHADOW_NOTE,
    }


# ---------- files ----------


def ledger_files(log_dir: Path, league: str) -> list[Path]:
    return sorted((Path(log_dir) / "paper_trades").glob(f"{league}_{FILE_TAG}_*.jsonl"))


def load_ledger(log_dir: Path, league: str) -> dict[str, dict]:
    from soccer_stats import paper

    return paper._fold(ledger_files(log_dir, league), paper.CORNER_UPDATE_FIELDS | UPDATE_FIELDS)


def append_events(log_dir: Path, events: list[dict]) -> int:
    if not events:
        return 0
    d = Path(log_dir) / "paper_trades"
    d.mkdir(parents=True, exist_ok=True)
    by_file: dict[Path, list[str]] = {}
    for ev in events:
        lg, season = ev["id"].split("|")[:2]
        by_file.setdefault(d / f"{lg}_{FILE_TAG}_{season}.jsonl", []).append(
            json.dumps(ev, separators=(",", ":"), default=float)
        )
    for path, lines in by_file.items():
        with open(path, "a") as f:
            f.write("\n".join(lines) + "\n")
    return len(events)


def defined_at(log_dir: Path | None, now: pd.Timestamp, write: bool = True) -> str:
    """When the track went live: paper_trades/corner_shop_defined.json, written on the first
    call with a ledger directory (the first default-branch paper run after the merge)."""
    stamp = pd.Timestamp(now).isoformat(timespec="seconds")
    if log_dir is None:
        return stamp
    path = Path(log_dir) / "paper_trades" / DEFINED_FILE
    if path.exists():
        try:
            return json.loads(path.read_text())["defined_at"]
        except (ValueError, KeyError):
            pass
    if not write:
        return stamp
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "defined_at": stamp,
        "commit": (os.environ.get("GITHUB_SHA") or "")[:7] or None,
        "note": "The first paper run of the corner line-shop track (its live start).",
    }
    path.write_text(json.dumps(rec, indent=1) + "\n")
    return stamp


# ---------- the rule ----------


def _utc(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tz is None else t.tz_convert("UTC")


def _fresh(r: dict) -> bool:
    try:
        got = _utc(r["downloaded_at"])
        return pd.Timedelta(0) <= got - _utc(r.get("fetched_at") or got) <= FRESH
    except (KeyError, TypeError, ValueError):
        return False


def _price(v) -> float | None:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if v > 1 else None


def look_calls(rows: list[dict], start: str = JUDGED_FROM) -> list[dict]:
    """Each team half line's first look call from `start` that Pinnacle quotes two-sided and
    fresh, before kickoff: {league, home, away, kickoff, side, line, at, pin, quotes: {book:
    row}} with every other book's row from the same call (same downloaded_at)."""
    lo = _utc(start)
    calls: dict[tuple, dict[str, dict]] = {}
    for r in rows:
        if r.get("snapshot") != "look":
            continue
        try:
            line = float(r["line"])
            k, got = _utc(r["kickoff"]), _utc(r["downloaded_at"])
        except (KeyError, TypeError, ValueError):
            continue
        if line == int(line) or got < lo or got >= k:
            continue
        key = (r.get("league"), r["home"], r["away"], k, r["side"], line, got)
        calls.setdefault(key, {})[r.get("book") or PIN] = r
    first: dict[tuple, dict] = {}
    for key in sorted(calls, key=lambda k: k[-1]):
        books = calls[key]
        pin = books.get(PIN)
        if pin is None or _price(pin.get("over")) is None or _price(pin.get("under")) is None:
            continue
        if not _fresh(pin):
            continue
        line_key = key[:-1]
        if line_key in first:
            continue
        lg, h, a, k, side, line, got = key
        first[line_key] = {
            "league": lg,
            "home": h,
            "away": a,
            "kickoff": k,
            "side": side,
            "line": line,
            "at": got,
            "pin": pin,
            "quotes": {b: r for b, r in books.items() if b != PIN},
        }
    return sorted(first.values(), key=lambda c: (c["at"], c["home"], c["side"], c["line"]))


def evaluate(call: dict) -> dict | None:
    """The best bettable gap at one line: {bet, odds, book, gap, fair, gaps} where `gaps` =
    {book: {over, under}} for every fresh book quote (Bovada included, descriptive); None
    without a fresh bettable price."""
    pin = call["pin"]
    fair = tr.devig_pair(pin["over"], pin["under"])
    if fair is None:
        return None
    fair = {"over": fair[0], "under": fair[1]}
    gaps: dict[str, dict] = {}
    best = None
    for b in (*BET_BOOKS, *SEEN_BOOKS):
        q = call["quotes"].get(b)
        if q is None or not _fresh(q):
            continue
        g = {}
        for side in ("over", "under"):
            p = _price(q.get(side))
            if p is None:
                continue
            g[side] = p * fair[side] - 1
            if b in BET_BOOKS and (best is None or g[side] > best["gap"]):
                best = {"bet": side, "odds": p, "book": b, "gap": g[side]}
        if g:
            gaps[b] = {s: round(v, 5) for s, v in g.items()}
    if best is None:
        return None
    return {**best, "fair": fair, "gaps": gaps}


def entries(rows: list[dict], start: str = JUDGED_FROM) -> dict[str, list[dict]]:
    """Per track, one entry per match x team: the line with the largest gap at or over T
    (ties: the lower line)."""
    out: dict[str, dict[tuple, dict]] = {k: {} for k in THRESHOLDS}
    for call in look_calls(rows, start):
        ev = evaluate(call)
        if ev is None:
            continue
        team = (call["league"], call["home"], call["away"], call["kickoff"], call["side"])
        for key, t in THRESHOLDS.items():
            if ev["gap"] < t:
                continue
            have = out[key].get(team)
            if (
                have is None
                or ev["gap"] > have["gap"]
                or (ev["gap"] == have["gap"] and call["line"] < have["call"]["line"])
            ):
                out[key][team] = {**ev, "call": call}
    return {k: sorted(v.values(), key=lambda e: e["call"]["at"]) for k, v in out.items()}


def _r(v, n=5):
    return None if v is None else round(float(v), n)


def new_trade(e: dict, key: str, defined: pd.Timestamp) -> dict:
    c = e["call"]
    side = e["bet"]
    q = c["quotes"][e["book"]]
    t = tr.new_corner_trade(
        {
            "side": side,
            "odds": e["odds"],
            "model_p": e["fair"][side],
            "edge": e["gap"],
            "p_push": 0.0,
        },  # fmt: skip
        league=c["league"],
        home=c["home"],
        away=c["away"],
        team_side=c["side"],
        line=c["line"],
        kickoff=c["kickoff"],
        opened_at=c["at"],
        odds_fetched_at=c["at"].isoformat(),
        snapshot="look",
        book=PIN,
    )
    from soccer_stats import paper

    t.update(
        id=tr.trade_id(c["league"], t["season"], c["home"], c["away"], ID_TAG[key], c["side"]),
        bet_type=BET_TYPE,
        portfolio="corners",
        strategy=key,
        bookmaker=e["book"],
        book=e["book"],
        book_name=BOOK_NAMES.get(e["book"], e["book"]),
        threshold=THRESHOLDS[key],
        rule="corner_shop",
        p_source="pinnacle_fair",
        gap=_r(e["gap"]),
        pinnacle_fair=_r(e["fair"][side]),
        pinnacle_entry_prices={"over": c["pin"]["over"], "under": c["pin"]["under"]},
        pinnacle_fetched_at=c["pin"].get("fetched_at"),
        entry_prices={"over": q.get("over"), "under": q.get("under")},
        price_fetched_at=q.get("fetched_at"),
        gaps=e["gaps"],
        close_missing=paper.NO_CLOSE_AT_LINE,  # a look entry is never its own close
    )
    if key == SHADOW_KEY:
        t.update(variant_of=KEY, descriptive=True)
    if c["at"] < defined:  # opened later from the logged look: still counted (no backcast)
        t["from_log"] = True
    return t


def open_trades(ledger, rows, league, defined, now) -> list[dict]:
    """Open each match x team's entry per track: a look logged before `defined` opens from
    the log (`from_log`); a later look opens only while its kickoff is ahead. Mutates
    `ledger`; returns open events."""
    mine = [r for r in rows if r.get("league") == league]
    events = []
    for key, es in entries(mine).items():
        for e in es:
            c = e["call"]
            if c["at"] > now or (c["at"] >= defined and c["kickoff"] <= now):
                continue
            t = new_trade(e, key, defined)
            if tr.holds(ledger, t["id"]):
                continue
            ledger[t["id"]] = t
            events.append({"type": "open", **t})
    return events


def _book_close(t: dict, quotes: dict, now: pd.Timestamp) -> dict | None:
    """The entry book's own close at the trade's line (descriptive `clv_book`)."""
    from soccer_stats import corners_live as cl
    from soccer_stats import paper

    q = paper._quote_at(quotes.get(t["book"]) or {}, t)
    k = cl._ts(t["kickoff"])
    if not paper._is_close(t, q, k) or q.get(t["side"]) is None:
        return None
    if not paper._newer(q["downloaded_at"], t.get("book_close_fetched_at")):
        return None
    got = q["downloaded_at"]
    return paper._update(
        t,
        now,
        book_close_odds=q[t["side"]],
        book_close_prices={"over": q.get("over"), "under": q.get("under")},
        book_close_fetched_at=got,
        book_close_minutes_before=paper._minutes_before(t["kickoff"], got),
        clv_book=tr.corner_clv(t["odds"], t["side"], q.get("over"), q.get("under")),
    )


def update_ledger(
    ledger: dict[str, dict],
    rows: list[dict],
    results: pd.DataFrame | None,
    now: pd.Timestamp,
    league: str,
    defined: pd.Timestamp,
    espn: pd.DataFrame | None = None,
) -> list[dict]:
    """Open, close (Pinnacle's real close at the line, and the book's own) and settle one
    league's corner line-shop trades. Mutates `ledger`; returns events."""
    from soccer_stats import corners_live as cl
    from soccer_stats import paper

    events = paper.duplicate_sweep(ledger, now)
    events += open_trades(ledger, rows, league, defined, now)
    mine = [r for r in rows if (r.get("league") or league) == league]
    quotes = {b: cl.newest_quotes(mine, book=b) for b in (PIN, *BET_BOOKS)}
    for t in ledger.values():
        if t.get("status") != "open":
            ev = paper._corner_check(t, results, now)
            if ev:
                events.append(ev)
        if t.get("status") == "void":
            continue
        for ev in (
            paper.corner_close_update(t, quotes, now, PIN),
            _book_close(t, quotes, now),
            paper.corner_settle_update(t, results, espn, now),
        ):
            if ev:  # paper._update has already applied it to `t`
                events.append(ev)
    return events


# ---------- sections ----------


def sections(trades: list[dict], defined: str, now=None, read: dict | None = None) -> list[dict]:
    """The 2% line (KEY, judged) and the 1% line (SHADOW_KEY, variant_of KEY, adds
    "threshold", descriptive) for Team corners -> live -> strategies."""
    from soccer_stats import paper

    out = []
    for key, label in ((KEY, LABEL), (SHADOW_KEY, SHADOW_LABEL)):
        mine = [t for t in trades if t.get("strategy") == key]
        r = rule(key)
        sec = paper.strategy_section(mine, key, label, r, of=lambda t: t.get("strategy"), now=now)
        books: dict[str, int] = {}
        for t in mine:
            books[t.get("book") or "?"] = books.get(t.get("book") or "?", 0) + 1
        seen = sum(
            1
            for t in mine
            for b in SEEN_BOOKS
            if (t.get("gaps") or {}).get(b, {}).get(t["side"], -1) >= r["threshold"]
        )
        sec.update(
            defined_at=defined,
            note=r["note"],
            books={b: books.get(b, 0) for b in BET_BOOKS},
            seen_books={"bovada_at_threshold": seen},
            corner_shop_trades=len(mine),
            judged_from=JUDGED_FROM,
            from_log={
                "trades": sum(1 for t in mine if t.get("from_log")),
                "note": FROM_LOG_NOTE.format(
                    started=_utc(JUDGED_FROM).strftime("%-d %b"),
                    defined=_utc(defined).strftime("%-d %b"),
                    n=sum(1 for t in mine if t.get("from_log")),
                ),
            },
        )
        if key == KEY:
            sec["judged"] = (
                read
                if read is not None
                else pcl.judged(mine, JUDGED_FROM, now, None, VERDICT_FILE, JUDGE_BETS, DEADLINE)
            )
        else:
            since = _utc(JUDGED_FROM)
            main = [t for t in trades if t.get("strategy") == KEY]
            a, b = plsh._opened_from(mine, since), plsh._opened_from(main, since)
            sec.update(
                variant_of=KEY,
                adds="threshold",
                filter=None,
                descriptive=True,
                compare={
                    "since": JUDGED_FROM,
                    "descriptive": True,
                    "track": {
                        "trades": len(a),
                        "summary": paper.portfolio_section(a)["summary"],
                        "clv_pinnacle": plsh._clv_mean(a),
                    },
                    "parent": {
                        "trades": len(b),
                        "summary": paper.portfolio_section(b)["summary"],
                        "clv_pinnacle": plsh._clv_mean(b),
                    },
                    "note": "Descriptive: the 1% line enters on more lines, so this compares "
                    "the two records over the same period, not the same bets.",
                },
            )
        out.append(sec)
    return out


def safe_sections(trades, defined, now=None, read=None) -> list[dict]:
    try:
        return sections(trades, defined, now, read)
    except Exception as exc:  # noqa: BLE001
        return [{"key": KEY, "label": LABEL, "defined_at": defined, "error": type(exc).__name__}]


# ---------- the run ----------


def run(
    log_dir: Path,
    codes: list[str],
    now: pd.Timestamp,
    results: pd.DataFrame | None,
    espn: pd.DataFrame | None = None,
) -> dict:
    """Opens, closes and settles the track in every corner league in play; writes the ledger
    and (once) defined_at. Returns {trades, defined_at, judged, events, opened}."""
    from soccer_stats import corners_live as cl
    from soccer_stats import odds_log as ol
    from soccer_stats import paper

    rows = cl.load_rows(ol.log_dir(log_dir))
    has_shop = any((r.get("book") or PIN) in cl.SHOP_BOOKS for r in rows)
    defined = defined_at(log_dir, now, write=has_shop)
    d = _utc(defined)
    events: list[dict] = []
    trades: list[dict] = []
    for lg in [c for c in codes if c in cl.LEAGUES]:
        ledger = load_ledger(log_dir, lg)
        events += update_ledger(
            ledger,
            rows,
            paper._league_rows(results, lg),
            now,
            lg,
            d,
            paper._league_rows(espn, lg),
        )
        trades += list(ledger.values())
    n = append_events(log_dir, events)
    main = [t for t in trades if t.get("strategy") == KEY]
    read = pcl.judged(
        main, JUDGED_FROM, now, log_dir if has_shop else None, VERDICT_FILE, JUDGE_BETS, DEADLINE
    )
    return {
        "trades": trades,
        "defined_at": defined,
        "judged": read,
        "events": n,
        "opened": [e for e in events if e.get("type") == "open"],
    }


def summary_line(res: dict) -> str:
    parts = []
    for key, name in ((KEY, "2%"), (SHADOW_KEY, "1% shadow")):
        mine = [t for t in res["trades"] if t.get("strategy") == key]
        new = [e for e in res["opened"] if e.get("strategy") == key]
        parts.append(
            f"{name} {len(mine)} trades ({sum(1 for t in mine if t.get('from_log'))} from the "
            f"log before the build), {len(new)} opened this update"
        )
    r = res["judged"]
    read = r["verdict"] if r.get("verdict") else f"{r.get('final', 0)} of {JUDGE_BETS} final"
    return (
        "Corner line shop vs Pinnacle: " + "; ".join(parts)
        + f"; defined_at {res['defined_at']}; judged read: {read}"
    )  # fmt: skip
