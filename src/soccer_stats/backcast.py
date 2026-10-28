"""Backcasts: a new strategy or track starts on the original portfolio's date (owner, 28 Oct).

A strategy or track added later is replayed from START (14 Oct, the first full day of the
live Moneyline portfolio) up to its `defined_at`, on what was logged at the time: each
DraftKings h2h row of the odds log (odds_log/<code>_<YYYY-MM>.jsonl) is one observation,
taken at the moment it was logged (`logged_at`), with the prices, the quote time and the
model's chances (`p`, `p_bet`) the publish saw then. The track's own open rule runs on
each observation in time order, so a backcast trade opens at the first logged moment its
rule would have opened it live; closes and settlement then follow the live code.

Backcast entries carry `backcast: true` and `backcast_from: "odds_log"`; they are
descriptive and never part of a judged number (those count trades opened at or after
`defined_at`). Each section that has them gets a `block`.

Limits: the odds log is deduplicated on the price, so a model change without a price
change isn't an observation of its own (the row keeps the chances first logged with that
price); `low_data` isn't logged, so that gate can't be replayed.
"""

from __future__ import annotations

from collections.abc import Iterator

import pandas as pd

START = "2026-10-14T00:00:00+00:00"  # the original Moneyline portfolio's first full day
FROM = "odds_log"
NOTE = (
    "Backcast: before {defined} this line replays the rule from {started} on the DraftKings "
    "prices and model chances logged at the time (backcast_from odds_log). It is "
    "descriptive only; the track is judged on trades opened from {defined}."
)


def _utc(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tz is None else t.tz_convert("UTC")


def observations(
    log: pd.DataFrame | None, until: str, start: str = START
) -> Iterator[tuple[pd.Timestamp, dict, pd.Timestamp | None]]:
    """(now, card, downloaded) per logged h2h row with start <= logged_at < until, oldest
    first. `card` is the slice of a data.json card a rule reads: home, away, kickoff, odds
    (h2h prices), p, p_bet and odds_updated (the quote time); `downloaded` is when the
    publish downloaded it (the freshness gate's clock)."""
    if log is None or log.empty or "logged_at" not in log:
        return
    lo, hi = _utc(start), _utc(until)
    df = log[log["market"] == "h2h"].copy()
    df["_now"] = pd.to_datetime(df["logged_at"], utc=True)
    df = df[(df["_now"] >= lo) & (df["_now"] < hi)].sort_values("_now", kind="stable")
    for r in df.to_dict("records"):
        down = r.get("downloaded_at")
        card = {
            "home": r["home"],
            "away": r["away"],
            "kickoff": _utc(r["kickoff"]).isoformat(timespec="seconds"),
            "odds": dict(r.get("prices") or {}),
            "p": r.get("p") if isinstance(r.get("p"), dict) else None,
            "p_bet": r.get("p_bet") if isinstance(r.get("p_bet"), dict) else None,
            "odds_updated": _utc(r["fetched_at"]).isoformat(timespec="seconds"),
        }
        yield r["_now"], card, (_utc(down) if isinstance(down, str) and down else None)


def mark(trade: dict, **extra) -> dict:
    """Tag a replayed entry: backcast true, backcast_from "odds_log", plus `extra`."""
    trade.update(backcast=True, backcast_from=FROM, **extra)
    return trade


def is_backcast(t: dict) -> bool:
    return bool(t.get("backcast"))


def _settled(t: dict) -> bool:
    return t.get("status") in ("won", "lost") or bool(t.get("push"))


def block(
    trades: list[dict],
    defined_at: str,
    note: str | None = None,
    started: str = START,
    **extra,
) -> dict:
    """A section's `backcast` block (one shape for every strategy and track): applied,
    started, until (= defined_at), defined_at, the trades opened before defined_at (count,
    settled: won, lost or a refunded push; first and last opened as from/to), the note and
    descriptive true. `extra` adds strategy-specific keys (Lean's lean_started, ...)."""
    since = _utc(defined_at)
    before = []
    for t in trades:
        try:
            if _utc(t["opened_at"]) < since:
                before.append(t)
        except (KeyError, TypeError, ValueError):
            continue
    opened = sorted(str(t["opened_at"]) for t in before)
    day = lambda x: _utc(x).strftime("%-d %b")  # noqa: E731
    return {
        "applied": True,
        "started": started,
        "until": defined_at,
        "defined_at": defined_at,
        "trades": len(before),
        "settled": sum(1 for t in before if _settled(t)),
        "from": opened[0] if opened else None,
        "to": opened[-1] if opened else None,
        "note": note or NOTE.format(defined=day(defined_at), started=day(started)),
        "descriptive": True,
        **extra,
    }
