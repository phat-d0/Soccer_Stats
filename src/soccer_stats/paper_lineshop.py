"""Line shop vs Pinnacle: a paper track in the Moneyline portfolio (owner-approved 5 Nov 2026).

Pre-registered in docs/lab.md "Line shop vs Pinnacle". No model: at each moment the
every-book log records (odds_log/<code>_books_<YYYY-MM>.jsonl, one row per fixture, book and
quote), before kickoff only:

* for each outcome (home, draw, away), the best price among the four books the owner can bet
  at (SHOP_BOOKS: DraftKings, FanDuel, BetMGM, BetRivers) x Pinnacle's Shin-fair chance at
  that same moment - 1 is the gap;
* both the book's and Pinnacle's quote were updated within FRESH_MINUTES (15) before the
  moment (moment = the row's `logged_at`; a book's quote at a moment is its latest row logged
  at or before it, since the log keeps only changed quotes);
* entry at the first moment any outcome's gap reaches T (2%), at that logged price, one per
  match; two outcomes at once: the larger gap; books tied on price: SHOP_BOOKS order;
* $10 at the book named. Pinnacle is the reference only and is never bet.

T = 1% runs beside it as a descriptive shadow (SHADOW_KEY): its own entries (it can enter
earlier and on more matches than 2%), never judged.

Close: the log's last Pinnacle quote before kickoff; CLV = entry x its Shin-fair chance - 1
(`clv_pinnacle`, the judged measure), and the entry book's own close (`clv_book`). Settled
like the match trades (football-data, else ESPN). Judged once, at 300 bets opened from
`defined_at`: the 95% range of mean CLV above 0 (matches resampled, 2,000 draws, seed 0).

`defined_at` is when the track went live: the first paper run with a data-log ledger after
the merge writes paper_trades/lineshop_defined.json (only the default branch keeps it).
Moments before it are a backcast from BACKCAST_START (28 Oct, when the every-book log began;
the documented exception to backcast.START's 14 Oct), descriptive and never judged.

The trades live in their own ledger (paper_trades/<code>_lineshop_<season>.jsonl), outside
Moneyline's record and totals. No API calls: the every-book log already holds the prices.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

from soccer_stats import backcast as bc
from soccer_stats import odds_log as ol
from soccer_stats import trades as tr
from soccer_stats.odds import devig_shin

KEY = "lineshop"
LABEL = "Line shop vs Pinnacle"
SHADOW_KEY = "lineshop_1pct"
SHADOW_LABEL = "Line shop vs Pinnacle, 1% gap (descriptive)"
SHOP_BOOKS = ("draftkings", "fanduel", "betmgm", "betrivers")  # tie order too
BOOK_NAMES = {
    "draftkings": "DraftKings",
    "fanduel": "FanDuel",
    "betmgm": "BetMGM",
    "betrivers": "BetRivers",
    "pinnacle": "Pinnacle",
}
SHARP = "pinnacle"
OUTCOMES = ("home", "draw", "away")
THRESHOLDS = {KEY: 0.02, SHADOW_KEY: 0.01}
ID_SUFFIX = {KEY: "|lineshop", SHADOW_KEY: "|lineshop1"}
FRESH_MINUTES = 15
STAKE = 10.0
BACKCAST_START = "2026-10-28T00:00:00+00:00"  # the every-book log's first day
BACKCAST_FROM = "books_log"
DEFINED_FILE = "lineshop_defined.json"
JUDGE_BETS = 300
JUDGE_DRAWS = 2000
RULE_NOTE = (
    "No model. At each logged moment before kickoff, the best price among DraftKings, "
    "FanDuel, BetMGM and BetRivers is compared with Pinnacle's margin-free (Shin) chance for "
    "the same outcome at that moment; when best x fair - 1 reaches {t:.0%}, one $10 paper "
    "trade opens at the book named, at the first such moment, one per match. Both quotes "
    "must be under 15 minutes old. Judged once at 300 bets on CLV against Pinnacle's close "
    "(pre-registered in docs/lab.md, 'Line shop vs Pinnacle')."
)
SHADOW_NOTE = (
    "Shadow at a 1% gap: the same rule at a lower bar, tracked beside the 2% line. It places "
    "no bets of its own and is never judged; it can enter earlier and on more matches."
)
BACKCAST_NOTE = (
    "Backcast: before {defined} this line replays the rule from {started} on the prices the "
    "every-book log recorded at the time (backcast_from books_log). It starts on 28 Oct, not "
    "14 Oct like the other tracks, because the every-book log began then. Descriptive only; "
    "the track is judged on trades opened from {defined}."
)
AGE_NOTE = (
    "Logged prices: the log records a match about every 3 hours a day out and every 15 "
    "minutes near kickoff, so a gap can be gone before anyone sees it. price_age_min is how "
    "old the book's quote was when it was logged."
)
UPDATE_FIELDS = {
    "close_odds",
    "close_prices",
    "close_fetched_at",
    "close_minutes_before",
    "clv_pinnacle",
    "beat_close_pinnacle",
    "book_close_odds",
    "book_close_prices",
    "book_close_fetched_at",
    "book_close_minutes_before",
    "clv_book",
    "status",
    "score",
    "profit",
    "settled_at",
    "result_source",
    "result_check",
    "check_score",
}


def rule(key: str = KEY) -> dict:
    """The track's rule dict (portfolio sections, sizing and the app's rule box)."""
    t = THRESHOLDS[key]
    return {
        "key": key,
        "rule": "lineshop",
        "threshold": t,
        "books": list(SHOP_BOOKS),
        "reference": SHARP,
        "fresh_minutes": FRESH_MINUTES,
        "stake": STAKE,
        "p_source": "pinnacle_fair",
        "clv_field": "clv_pinnacle",
        "source": "preregistered",
        "judge_bets": JUDGE_BETS if key == KEY else None,
        "note": RULE_NOTE.format(t=t) if key == KEY else SHADOW_NOTE,
    }


# ---------- ledger files ----------


def ledger_files(log_dir: Path, league: str) -> list[Path]:
    return sorted((Path(log_dir) / "paper_trades").glob(f"{league}_lineshop_*.jsonl"))


def load_ledger(log_dir: Path, league: str) -> dict[str, dict]:
    """A league's line-shop trades (both thresholds) by id."""
    from soccer_stats import paper

    return paper._fold(ledger_files(log_dir, league), UPDATE_FIELDS)


def append_events(log_dir: Path, events: list[dict]) -> int:
    """Append events to <code>_lineshop_<season>.jsonl (league and season from the id)."""
    if not events:
        return 0
    d = Path(log_dir) / "paper_trades"
    d.mkdir(parents=True, exist_ok=True)
    by_file: dict[Path, list[str]] = {}
    for ev in events:
        lg, season = ev["id"].split("|")[:2]
        by_file.setdefault(d / f"{lg}_lineshop_{season}.jsonl", []).append(
            json.dumps(ev, separators=(",", ":"))
        )
    for path, lines in by_file.items():
        with open(path, "a") as f:
            f.write("\n".join(lines) + "\n")
    return len(events)


def defined_at(log_dir: Path | None, now: pd.Timestamp, write: bool = True) -> str:
    """When the track went live: read from paper_trades/lineshop_defined.json, written on
    the first call with a ledger directory (the first default-branch paper run after the
    merge; branch runs never keep data-log). Without a ledger directory, or with `write`
    False (no every-book log to replay yet), `now`, and nothing is written."""
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
        "note": "The first paper run of the line-shop track (its live start).",
    }
    path.write_text(json.dumps(rec, indent=1) + "\n")
    return stamp


# ---------- the rule ----------


def _utc(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tz is None else t.tz_convert("UTC")


def _fair(prices: dict) -> dict | None:
    vals = [prices.get(m) for m in OUTCOMES]
    if not all(isinstance(v, int | float) and np.isfinite(v) and v > 1 for v in vals):
        return None
    f = devig_shin(np.array(vals, dtype=float))
    return {m: float(p) for m, p in zip(OUTCOMES, f, strict=True)}


def _age(moment: pd.Timestamp, row: dict) -> float:
    return (moment - _utc(row["fetched_at"])) / pd.Timedelta(minutes=1)


def evaluate(state: dict[str, dict], moment: pd.Timestamp) -> dict | None:
    """The best gap at one moment from each book's latest quote (`state`: book -> row), or
    None when Pinnacle's quote is missing or stale or no shop book is fresh. Returns
    {market, gap, odds, book, fair, pin_row, row}: per outcome the best fresh shop price
    (ties: SHOP_BOOKS order), the outcome with the largest gap (ties: home, draw, away)."""
    pin = state.get(SHARP)
    if pin is None or not 0 <= _age(moment, pin) <= FRESH_MINUTES:
        return None
    fair = _fair(pin.get("prices") or {})
    if fair is None:
        return None
    best = None
    for m in OUTCOMES:
        top = None
        for b in SHOP_BOOKS:  # first in order wins a tie on price
            row = state.get(b)
            if row is None or not 0 <= _age(moment, row) <= FRESH_MINUTES:
                continue
            price = (row.get("prices") or {}).get(m)
            if not isinstance(price, int | float) or not price > 1:
                continue
            if top is None or price > top[0]:
                top = (float(price), b, row)
        if top is None:
            continue
        gap = top[0] * fair[m] - 1
        if best is None or gap > best["gap"]:
            best = {"market": m, "gap": gap, "odds": top[0], "book": top[1], "row": top[2]}
    if best is None:
        return None
    return {**best, "fair": fair, "pin_row": pin}


def _records(books: pd.DataFrame) -> list[dict]:
    rows = books[books["book"].isin((*SHOP_BOOKS, SHARP))] if "book" in books else books[:0]
    rows = rows[rows["market"] == "h2h"] if "market" in rows else rows
    out = rows.to_dict("records")
    for r in out:
        r["_at"] = _utc(r["logged_at"])
        r["_ko"] = _utc(r["kickoff"])
    return out


def first_entries(
    books: pd.DataFrame | None, thresholds: dict[str, float], until: pd.Timestamp | None = None
) -> list[dict]:
    """Each match's first qualifying moment per track (`thresholds`: key -> T), in time
    order: {key, moment, home, away, kickoff, ...evaluate()}. A moment is a logged_at of
    the match's rows before kickoff (and before `until`); each book's quote then is its
    latest row logged at or before the moment, so nothing later is ever read."""
    if books is None or books.empty or "logged_at" not in books:
        return []
    # A match is (home, away, kickoff as logged): a corrected kickoff starts a new group
    # without the earlier quotes, so its first moment needs fresh quotes of its own (the
    # trade id, one per match, still keeps a second entry out).
    groups: dict[tuple, list[dict]] = {}
    for r in _records(books):
        groups.setdefault((r["home"], r["away"], r["kickoff"]), []).append(r)
    found = []
    for (home, away, _k), rows in groups.items():
        rows.sort(key=lambda r: (r["_at"], _utc(r["fetched_at"])))
        kickoff = rows[0]["_ko"]
        state: dict[str, dict] = {}
        todo = dict(thresholds)
        i = 0
        while i < len(rows) and todo:
            moment = rows[i]["_at"]
            while i < len(rows) and rows[i]["_at"] == moment:
                state[rows[i]["book"]] = rows[i]
                i += 1
            if moment >= kickoff or (until is not None and moment >= until):
                break
            ev = evaluate(state, moment)
            if ev is None:
                continue
            for key, t in list(todo.items()):
                if ev["gap"] >= t:
                    found.append(
                        {
                            "key": key,
                            "moment": moment,
                            "home": home,
                            "away": away,
                            "kickoff": kickoff,
                            **ev,
                        }  # fmt: skip
                    )
                    del todo[key]
    return sorted(found, key=lambda e: (e["moment"], e["key"]))


def _r(v, n=4):
    return None if v is None else round(float(v), n)


def new_trade(e: dict, league: str, defined: pd.Timestamp) -> dict:
    """The trade record: trades.new_trade's shape plus the book, the gap, Pinnacle's fair
    chance (`model_p` = `pinnacle_fair`, `edge` = `gap`), both quotes' ages and prices; a
    moment before `defined` is a backcast entry."""
    key, moment, row, pin = e["key"], e["moment"], e["row"], e["pin_row"]
    pick = {"market": e["market"], "odds": e["odds"], "model_p": e["fair"][e["market"]],
            "edge": e["gap"]}  # fmt: skip
    t = tr.new_trade(
        pick,
        source="live",
        league=league,
        home=e["home"],
        away=e["away"],
        kickoff=e["kickoff"],
        opened_at=moment,
        odds_fetched_at=str(row["fetched_at"]),
        threshold=THRESHOLDS[key],
        model_ref={},
        stake=STAKE,
        rule="lineshop",
        p_source="pinnacle_fair",
        strategy=key,
    )
    t.update(
        id=t["id"] + ID_SUFFIX[key],
        bet_type="lineshop",
        portfolio="moneyline",
        strategy=key,
        book=e["book"],
        book_name=BOOK_NAMES.get(e["book"], e["book"]),
        gap=_r(e["gap"], 5),
        pinnacle_fair=_r(e["fair"][e["market"]], 5),
        pinnacle_fair_prices={m: _r(v, 5) for m, v in e["fair"].items()},
        pinnacle_entry_prices={m: (pin.get("prices") or {}).get(m) for m in OUTCOMES},
        pinnacle_fetched_at=str(pin["fetched_at"]),
        pinnacle_age_min=_r(_age(moment, pin), 1),
        entry_prices={m: (row.get("prices") or {}).get(m) for m in OUTCOMES},
        price_age_min=_r(_age(moment, row), 1),
        logged_at=moment.isoformat(timespec="seconds"),
    )
    if key == SHADOW_KEY:  # a descriptive variant with entries of its own, never judged
        t.update(variant_of=KEY, descriptive=True)
    if moment < defined:
        bc.mark(t)
        t["backcast_from"] = BACKCAST_FROM
    return t


def open_trades(
    ledger: dict[str, dict],
    books: pd.DataFrame | None,
    league: str,
    defined: pd.Timestamp,
    now: pd.Timestamp,
    start: str = BACKCAST_START,
) -> list[dict]:
    """Open each match's trade per track at its first qualifying moment from `start` up to
    `now` (moments before `defined` are backcast); a match holding one is left alone.
    Mutates `ledger`; returns open events."""
    if books is None or books.empty:
        return []
    lo = _utc(start)
    logged = pd.to_datetime(books["logged_at"], utc=True)
    events = []
    for e in first_entries(books[logged >= lo], THRESHOLDS, until=now + pd.Timedelta(seconds=1)):
        t = new_trade(e, league, defined)
        if tr.holds(ledger, t["id"]):
            continue
        ledger[t["id"]] = t
        events.append({"type": "open", **t})
    return events


# ---------- close and settlement ----------


def _close(t: dict, books: pd.DataFrame, now: pd.Timestamp) -> dict | None:
    """An update with Pinnacle's and the entry book's last quotes before kickoff, when newer
    than the trade's; never steps back in time."""
    from soccer_stats import paper

    fields = {}
    pin = ol.last_before(books, t["home"], t["away"], t["kickoff"], "h2h", book=SHARP)
    if pin is not None:
        quoted = pin["fetched_at"].isoformat(timespec="seconds")
        have = t.get("close_fetched_at")
        if not have or pd.Timestamp(quoted) > pd.Timestamp(have):
            prices = {m: pin["prices"].get(m) for m in OUTCOMES}
            clv = tr.clv(t["odds"], t["market"], prices)
            fields.update(
                close_odds=prices.get(t["market"]),
                close_prices=prices,
                close_fetched_at=quoted,
                close_minutes_before=paper._minutes_before(t["kickoff"], quoted),
                clv_pinnacle=clv,
                beat_close_pinnacle=None if clv is None else bool(clv > 0),
            )
    own = ol.last_before(books, t["home"], t["away"], t["kickoff"], "h2h", book=t["book"])
    if own is not None:
        quoted = own["fetched_at"].isoformat(timespec="seconds")
        have = t.get("book_close_fetched_at")
        if not have or pd.Timestamp(quoted) > pd.Timestamp(have):
            prices = {m: own["prices"].get(m) for m in OUTCOMES}
            fields.update(
                book_close_odds=prices.get(t["market"]),
                book_close_prices=prices,
                book_close_fetched_at=quoted,
                book_close_minutes_before=paper._minutes_before(t["kickoff"], quoted),
                clv_book=tr.clv(t["odds"], t["market"], prices),
            )
    return paper._update(t, now, **fields) if fields else None


def _check(t: dict, results: pd.DataFrame | None, now: pd.Timestamp) -> dict | None:
    """football-data's score check for an ESPN-settled trade, once (the settlement stands)."""
    from soccer_stats import paper

    if t.get("result_source") != "espn" or t.get("result_check") or not t.get("score"):
        return None
    res = paper._result(results, t)
    if res is None or paper._source(res) != paper.FOOTBALL_DATA:
        return None
    if pd.isna(res.get("home_goals")) or pd.isna(res.get("away_goals")):
        return None
    fd = f"{int(res['home_goals'])}-{int(res['away_goals'])}"
    return paper._update(
        t, now, result_check="ok" if fd == t["score"] else "mismatch", check_score=fd
    )


def update_ledger(
    ledger: dict[str, dict],
    books: pd.DataFrame | None,
    results: pd.DataFrame | None,
    now: pd.Timestamp,
    league: str,
    defined: pd.Timestamp,
    espn: pd.DataFrame | None = None,
    fixtures: list[dict] | None = None,
) -> list[dict]:
    """Open (first_entries), close (the every-book log) and settle one league's line-shop
    trades. Settlement is the match trades': football-data's result, else ESPN's; a fixture
    moved over paper.VOID_MOVED_HOURS, or with no result in paper.VOID_AFTER_DAYS, voids.
    Mutates `ledger`; returns events."""
    from soccer_stats import paper

    events = open_trades(ledger, books, league, defined, now)
    cards = {
        (ol.name_key(c["home"]), ol.name_key(c["away"])): c
        for c in fixtures or []
        if c.get("kickoff")
    }
    for t in ledger.values():
        if t.get("status") == "void":
            continue
        if books is not None and not books.empty:
            ev = _close(t, books, now)
            if ev:
                events.append(ev)
        if t["status"] != "open":
            ev = _check(t, results, now)
            if ev:
                events.append(ev)
            continue
        kickoff = _utc(t["kickoff"])
        settled_at = now.isoformat(timespec="seconds")
        c = cards.get((ol.name_key(t["home"]), ol.name_key(t["away"])))
        if c and abs(_utc(c["kickoff"]) - kickoff) > pd.Timedelta(hours=paper.VOID_MOVED_HOURS):
            events.append(
                paper._update(t, now, **tr.settle(t, None, None, True), settled_at=settled_at)
            )
            continue
        if kickoff > now:
            continue
        res = paper._result(results, t)
        if res is None:
            res = paper._espn_result(espn, t)
        if res is not None:
            moved = (
                abs((pd.Timestamp(res["date"]) - kickoff.tz_convert(None).normalize()).days)
                > paper.VOID_MOVED_HOURS / 24
            )
            fields = tr.settle(t, res["home_goals"], res["away_goals"], void=moved)
            events.append(
                paper._update(
                    t, now, **fields, settled_at=settled_at, result_source=paper._source(res)
                )
            )
        elif now - kickoff > pd.Timedelta(days=paper.VOID_AFTER_DAYS):
            events.append(
                paper._update(t, now, **tr.settle(t, None, None, True), settled_at=settled_at)
            )
    return events


# ---------- sections for data.json ----------


def _opened_from(trades: list[dict], since: pd.Timestamp) -> list[dict]:
    out = []
    for t in trades:
        try:
            if _utc(t["opened_at"]) >= since:
                out.append(t)
        except (KeyError, TypeError, ValueError):
            continue
    return out


VERDICT_FILE = "lineshop_verdict.json"
# A close counts as final an hour after kickoff: by then no quote from before kickoff can
# still be logged (log-odds skips quotes at or after kickoff).
FINAL_AFTER = pd.Timedelta(hours=1)


def _final(t: dict, now: pd.Timestamp) -> bool:
    """A judged trade's close is final: settled won/lost, or its kickoff over FINAL_AFTER
    ago (before kickoff the ledger's close is the newest quote so far, which right after
    entry equals the entry gap). Voids are never final."""
    if t.get("status") == "void":
        return False
    if t.get("status") in ("won", "lost"):
        return True
    try:
        return _utc(t["kickoff"]) <= now - FINAL_AFTER
    except (KeyError, TypeError, ValueError):
        return False


def _judged_pool(trades: list[dict], defined: str) -> list[dict]:
    """The bets the read counts: opened from defined_at, not backcast, not voided (a moved
    match refunds), oldest first."""
    since = _utc(defined)
    mine = [t for t in _opened_from(trades, since) if not t.get("backcast")]
    return sorted((t for t in mine if t.get("status") != "void"), key=lambda t: str(t["opened_at"]))


def _verdict(first: list[dict]) -> dict:
    """Mean CLV vs Pinnacle's close on `first` and its 95% range (matches resampled, 2,000
    draws, seed 0); a bet without a close counts as missing."""
    vals = [(t["id"].rsplit("|", 1)[0], float(t["clv_pinnacle"])) for t in first
            if t.get("clv_pinnacle") is not None]  # fmt: skip
    if not vals:
        return {"verdict": "fail", "missing": len(first), "clv": None, "clv_ci95": None}
    by: dict[str, list[float]] = {}
    for k, v in vals:
        by.setdefault(k, []).append(v)
    groups = list(by.values())
    rng = np.random.default_rng(0)
    means = []
    for _ in range(JUDGE_DRAWS):
        pick = rng.integers(0, len(groups), len(groups))
        means.append(float(np.mean([v for i in pick for v in groups[i]])))
    lo, hi = np.quantile(means, [0.025, 0.975])
    return {
        "verdict": "pass" if lo > 0 else "fail",
        "missing": len(first) - len(vals),
        "clv": float(np.mean([v for _, v in vals])),
        "clv_ci95": [float(lo), float(hi)],
    }


def judged(
    trades: list[dict],
    defined: str,
    now: pd.Timestamp | None = None,
    log_dir: Path | None = None,
) -> dict:
    """The pre-registered read, once. The first JUDGE_BETS bets opened from defined_at (not
    backcast, voids left out) are read only when every one of them has a final close
    (_final); the verdict is then written to paper_trades/lineshop_verdict.json with the
    trade ids and CLVs it used, and every later call returns that file unchanged (`frozen`).
    Until then: the count toward JUDGE_BETS (`bets`, `final`) and no CLV."""
    now = now or pd.Timestamp.now(tz="UTC")
    out = {
        "since": defined,
        "target": JUDGE_BETS,
        "verdict": None,
        "note": (
            f"Read once, when the first {JUDGE_BETS} bets opened from the start all have a "
            "final close (an hour after kickoff): the 95% range of mean CLV against Pinnacle's "
            "close must sit above 0. Until then only the count is shown."
        ),
    }
    path = Path(log_dir) / "paper_trades" / VERDICT_FILE if log_dir is not None else None
    if path is not None and path.exists():
        try:
            stored = json.loads(path.read_text())
            return {**out, **{k: v for k, v in stored.items() if k != "trades"}, "frozen": True}
        except ValueError:
            pass
    pool = _judged_pool(trades, defined)
    first = pool[:JUDGE_BETS]
    out.update(bets=len(pool), final=sum(1 for t in pool if _final(t, now)))
    if len(first) < JUDGE_BETS or not all(_final(t, now) for t in first):
        return out
    res = {**_verdict(first), "read": JUDGE_BETS, "read_at": now.isoformat(timespec="seconds")}
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        rec = {**res, "since": defined, "trades": [
            {"id": t["id"], "clv_pinnacle": t.get("clv_pinnacle")} for t in first]}  # fmt: skip
        path.write_text(json.dumps(rec, indent=1) + "\n")
    return {**out, **res, "frozen": path is not None}


def _clv_mean(trades: list[dict]) -> dict:
    vals = [t["clv_pinnacle"] for t in trades if t.get("clv_pinnacle") is not None]
    return {"trades": len(vals), "mean": float(np.mean(vals)) if vals else None}


def sections(
    trades: list[dict],
    defined: str,
    now: pd.Timestamp | None = None,
    read: dict | None = None,
) -> list[dict]:
    """The track's section (key "lineshop") and its 1% line (variant_of "lineshop",
    adds "threshold", no bets of its own) for Moneyline -> live -> strategies:
    paper.strategy_section plus defined_at, note, age_note, `backcast` (backcast.block from
    28 Oct, `exception` explaining why), `judged` (the main line) or `compare` (the
    shadow, descriptive), `books` (trades per book), `lineshop_trades` (count)."""
    from soccer_stats import paper

    started = BACKCAST_START
    note = BACKCAST_NOTE.format(
        defined=_utc(defined).strftime("%-d %b"), started=_utc(started).strftime("%-d %b")
    )
    out = []
    for key, label in ((KEY, LABEL), (SHADOW_KEY, SHADOW_LABEL)):
        mine = [t for t in trades if t.get("strategy") == key]
        r = rule(key)
        sec = paper.strategy_section(mine, key, label, r, of=lambda t: t.get("strategy"), now=now)
        books: dict[str, int] = {}
        for t in mine:
            books[t.get("book") or "?"] = books.get(t.get("book") or "?", 0) + 1
        sec.update(
            defined_at=defined,
            note=r["note"],
            age_note=AGE_NOTE,
            books={b: books.get(b, 0) for b in SHOP_BOOKS},
            lineshop_trades=len(mine),
            backcast=bc.block(
                mine,
                defined,
                note=note,
                started=started,
                backcast_from=BACKCAST_FROM,
                exception="starts 28 Oct, when the every-book log began (not 14 Oct)",
            ),
        )
        if key == KEY:
            sec["judged"] = read if read is not None else judged(mine, defined, now)
        else:
            since = _utc(defined)
            main = [t for t in trades if t.get("strategy") == KEY]
            a, b = _opened_from(mine, since), _opened_from(main, since)
            sec.update(
                variant_of=KEY,
                adds="threshold",
                filter=None,
                compare={
                    "since": defined,
                    "descriptive": True,
                    "track": {
                        "trades": len(a),
                        "summary": paper.portfolio_section(a)["summary"],
                        "clv_pinnacle": _clv_mean(a),
                    },  # fmt: skip
                    "parent": {
                        "trades": len(b),
                        "summary": paper.portfolio_section(b)["summary"],
                        "clv_pinnacle": _clv_mean(b),
                    },  # fmt: skip
                    "note": "Descriptive: the 1% line enters earlier and on more matches, so "
                    "this compares the two records over the same period, not the same bets.",
                },
            )
        out.append(sec)
    return out


def safe_sections(trades: list[dict], defined: str, now=None, read=None) -> list[dict]:
    """sections() for display only: a failure costs only this track."""
    try:
        return sections(trades, defined, now, read)
    except Exception as exc:  # noqa: BLE001
        return [{"key": KEY, "label": LABEL, "defined_at": defined, "error": type(exc).__name__}]


def summary_line(trades: list[dict], events: list[dict]) -> str:
    """The publish log's line: trades per track (backcast among them), opened this update."""
    parts = []
    for key, name in ((KEY, "2%"), (SHADOW_KEY, "1% shadow")):
        mine = [t for t in trades if t.get("strategy") == key]
        new = [e for e in events if e.get("type") == "open" and e.get("strategy") == key]
        live = sum(1 for e in new if not e.get("backcast"))
        parts.append(
            f"{name} {len(mine)} trades ({sum(map(bc.is_backcast, mine))} backcast), "
            f"{live} opened this update"
            + (f", {len(new) - live} backcast" if len(new) > live else "")
        )
    return "Line shop vs Pinnacle: " + "; ".join(parts)
