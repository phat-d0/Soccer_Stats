"""Live DraftKings match prices with timestamps, logged on every publish run.

Each publish run may (or may not, by the credit budget) download fresh DraftKings odds.
`soccer-stats log-odds` reads the built site's data.json and appends one row per
fixture and market (h2h, totals 2.5) to odds_log/<league>_<YYYY-MM>.jsonl on the
data-log branch. No extra API calls: it reuses the prices publish already fetched.

A row holds the fixture (home, away, kickoff), the market, each outcome's decimal
price and margin-free (Shin) chance, the bookmaker, when the price was quoted
(`fetched_at`: DraftKings' last_update when the API gave one, else the download time),
and the model's chances (`p`, and `p_bet`, the blend) at that moment. Rows quoted at or
after kickoff (in-play prices) are skipped. The log is append-only and deduplicated: a
row whose fixture, market, prices and fetched_at are already logged is not written
again, so cached runs add nothing.

The log is what lets late team news be tested later (price moves between the looks),
and it gives live paper trades their DraftKings close (`last_before`).
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from soccer_stats.odds import devig_shin

MARKETS = {"h2h": ("home", "draw", "away"), "totals": ("over25", "under25")}
LINES = {"h2h": None, "totals": 2.5}
BOOKMAKER = "DraftKings"
KICKOFF_TOLERANCE = pd.Timedelta(hours=48)  # a moved fixture still matches its trade


def log_dir(root: Path) -> Path:
    return Path(root) / "odds_log"


def _iso(ts) -> str | None:
    if ts is None or (isinstance(ts, float) and np.isnan(ts)):
        return None
    t = pd.Timestamp(ts)
    t = t.tz_localize("UTC") if t.tz is None else t.tz_convert("UTC")
    return t.isoformat(timespec="seconds")


def _ok(x) -> bool:
    return isinstance(x, (int, float)) and np.isfinite(x) and x > 1


def rows_from_data(data: dict, now: pd.Timestamp, league: str = "E0") -> list[dict]:
    """Log rows for every priced fixture market in a built data.json (DraftKings only)."""
    src = data.get("odds_source") or {}
    if src.get("name") != BOOKMAKER:
        return []
    downloaded = _iso(src.get("fetched_at"))
    out = []
    for c in data.get("fixtures") or []:
        if not c.get("kickoff"):
            continue
        kickoff = pd.Timestamp(_iso(c["kickoff"]))
        quoted = _iso(c.get("odds_updated")) or downloaded
        if quoted is None or pd.Timestamp(quoted) >= kickoff:
            continue  # no time, or an in-play price
        odds = c.get("odds") or {}
        for market, names in MARKETS.items():
            prices = [odds.get(m) for m in names]
            if not all(_ok(p) for p in prices):
                continue
            fair = devig_shin(np.array(prices, dtype=float))
            p = c.get("p") or {}
            pb = c.get("p_bet") or None
            out.append(
                {
                    "league": league,
                    "home": c["home"],
                    "away": c["away"],
                    "kickoff": kickoff.isoformat(timespec="seconds"),
                    "market": market,
                    "line": LINES[market],
                    "prices": {m: float(v) for m, v in zip(names, prices, strict=True)},
                    "fair": {m: round(float(v), 6) for m, v in zip(names, fair, strict=True)},
                    "bookmaker": BOOKMAKER,
                    "fetched_at": quoted,
                    "time_source": "last_update" if c.get("odds_updated") else "download",
                    "downloaded_at": downloaded,
                    "logged_at": now.isoformat(timespec="seconds"),
                    "p": {m: p.get(m) for m in names},
                    "p_bet": {m: pb.get(m) for m in names} if pb else None,
                }
            )
    return out


def _key(r: dict) -> tuple:
    prices = tuple(sorted((r.get("prices") or {}).items()))
    return (r["home"], r["away"], r["kickoff"], r["market"], prices, r["fetched_at"])


def _read(root: Path, league: str) -> list[dict]:
    rows = []
    for path in sorted(log_dir(root).glob(f"{league}_*.jsonl")):
        for line in path.read_text().splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


def append(root: Path, rows: list[dict], league: str = "E0") -> int:
    """Append rows not already logged to their month's file; returns how many were written."""
    if not rows:
        return 0
    seen = {_key(r) for r in _read(root, league)}
    by_file: dict[Path, list[str]] = {}
    d = log_dir(root)
    for r in rows:
        k = _key(r)
        if k in seen:
            continue
        seen.add(k)
        month = pd.Timestamp(r["fetched_at"]).strftime("%Y-%m")
        by_file.setdefault(d / f"{league}_{month}.jsonl", []).append(
            json.dumps(r, separators=(",", ":"))
        )
    if not by_file:
        return 0
    d.mkdir(parents=True, exist_ok=True)
    for path, lines in by_file.items():
        with open(path, "a") as f:
            f.write("\n".join(lines) + "\n")
    return sum(len(v) for v in by_file.values())


def load(root: Path, league: str = "E0") -> pd.DataFrame:
    """Every logged row, oldest quote first, with kickoff and fetched_at as UTC timestamps."""
    rows = _read(root, league)
    if not rows:
        return pd.DataFrame(
            columns=["home", "away", "kickoff", "market", "prices", "fair", "fetched_at"]
        )
    df = pd.DataFrame(rows)
    df["kickoff"] = pd.to_datetime(df["kickoff"], utc=True)
    df["fetched_at"] = pd.to_datetime(df["fetched_at"], utc=True)
    return df.sort_values(["fetched_at", "logged_at"]).reset_index(drop=True)


def last_before(log: pd.DataFrame, home: str, away: str, kickoff, market: str) -> dict | None:
    """The last logged quote for a fixture's market before kickoff, or None.

    `market` is "h2h" or "totals". A logged kickoff within KICKOFF_TOLERANCE of the given
    one counts as the same fixture (kickoff times get corrected).
    """
    if log is None or log.empty:
        return None
    k = pd.Timestamp(kickoff)
    k = k.tz_localize("UTC") if k.tz is None else k.tz_convert("UTC")
    m = log[
        (log["home"] == home)
        & (log["away"] == away)
        & (log["market"] == market)
        & ((log["kickoff"] - k).abs() <= KICKOFF_TOLERANCE)
        & (log["fetched_at"] < k)
    ]
    if m.empty:
        return None
    r = m.iloc[-1].to_dict()
    r["minutes_before"] = round((k - r["fetched_at"]) / pd.Timedelta(minutes=1), 1)
    return r
