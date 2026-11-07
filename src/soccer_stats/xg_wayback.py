"""Can the Wayback Machine give back the Understat seasons no cache holds? (lead, 7 Nov).

Understat has answered every URL with a maintenance page since about 2 Nov 2026, and the
older seasons' league files (E0 2017-2021, SP1/D1/I1/F1 2017-2023) were evicted from every
GitHub cache before that. A finished season never changes, so an archived copy taken after
the season ended is as good as a fresh download.

This probe only counts: for each league-season it asks web.archive.org's CDX index (free, no
key) for captures of the JSON endpoint our fetcher uses (`xg.URL`, getLeagueData) and of the
league page (understat.com/league/<league>/<year>, whose HTML embedded the same data as
`datesData`/`teamsData`/`playersData` until Understat moved to the JSON endpoint), picks the
latest good capture, downloads it once (the `id_` form: the original bytes), turns it into the
fetcher's shape ({"dates", "teams", "players"}) and checks it with `xg.check_league_json`.
Each row reports whether a capture exists, its date, whether it passes, its matches and played
results, and football-data's match count for the same season. Nothing is written into the
good-copy folder; `--out` keeps the decoded files and their sha256 for a later, reviewed load.
"""

from __future__ import annotations

import codecs
import hashlib
import json
import re
import time
from pathlib import Path

import requests

from soccer_stats.xg import LEAGUES, URL, check_league_json

CDX = "https://web.archive.org/cdx/search/cdx"
RAW = "https://web.archive.org/web/{ts}id_/{url}"
PAGE = "https://understat.com/league/{league}/{season}"
HEADERS = {"User-Agent": "soccer-stats xG archive probe (research; one request a second)"}
PAUSE = 1.5  # seconds between requests: the Wayback Machine asks for a gentle pace
TIMEOUT = 60
TRIES = 3
SEASON_END = "{next}0701"  # a capture from 1 July after the season holds every result
# The seasons Research needs that no cache holds (CLAUDE.md "Waiting on Understat").
WANTED = {"E0": range(2017, 2022), **{c: range(2017, 2024) for c in ("SP1", "D1", "I1", "F1")}}
EMBEDDED = {
    "dates": "datesData",
    "teams": "teamsData",
    "players": "playersData",
}


def _get(url: str, params: dict | None = None, session=None) -> requests.Response:
    """GET with a pause before it and a retry on 429/5xx or a network error."""
    s = session or requests
    last: Exception | None = None
    for attempt in range(TRIES):
        time.sleep(PAUSE * (attempt + 1))
        try:
            r = s.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
        except requests.RequestException as exc:
            last = exc
            continue
        if r.status_code == 429 or r.status_code >= 500:
            last = RuntimeError(f"HTTP {r.status_code}")
            continue
        return r
    raise last or RuntimeError("no answer")


def captures(url: str, session=None) -> list[dict]:
    """Every 200 capture of `url` in the CDX index, oldest first: {timestamp, original,
    mimetype, length}."""
    r = _get(
        CDX,
        {
            "url": url,
            "output": "json",
            "filter": "statuscode:200",
            "fl": "timestamp,original,mimetype,length",
        },
        session,
    )
    if r.status_code != 200 or not r.text.strip():
        return []
    rows = r.json()
    if not rows:
        return []
    head, *body = rows
    return [dict(zip(head, row, strict=False)) for row in body]


def _unescape(s: str) -> str:
    """Understat's embedded JSON: a JS string of \\xHH escapes (UTF-8 bytes)."""
    return codecs.decode(s, "unicode_escape").encode("latin-1").decode("utf-8")


def parse(body: bytes) -> dict:
    """A capture's bytes in the fetcher's shape ({"dates", "teams", "players"}), from the
    JSON endpoint as is, or from the league page's embedded JSON.parse('...') strings.
    Raises ValueError when neither is there."""
    text = body.lstrip()
    if text[:1] in (b"{", b"["):
        data = json.loads(body)
        return data if isinstance(data, dict) else {"dates": data}
    html = body.decode("utf-8", "replace")
    out = {}
    for key, var in EMBEDDED.items():
        m = re.search(rf"var\s+{var}\s*=\s*JSON\.parse\('(.*?)'\)", html, re.S)
        if m:
            out[key] = json.loads(_unescape(m.group(1)))
    if "dates" not in out:
        raise ValueError("no datesData in the page")
    return out


def played(data: dict) -> int:
    return sum(1 for d in data.get("dates") or [] if isinstance(d, dict) and d.get("isResult"))


def fd_matches(league: str, year: int) -> int | None:
    """football-data's match count for the season (free CSV), None if it can't be read."""
    import pandas as pd

    from soccer_stats import data as fd

    try:
        path = fd.download(league, year)
        return int(len(pd.read_csv(path, encoding="latin-1").dropna(subset=["HomeTeam"])))
    except Exception:  # noqa: BLE001  a count only
        return None


def probe_one(league: str, year: int, out_dir: Path | None = None, session=None) -> dict:
    """One league-season's row (see the module doc)."""
    us = LEAGUES[league]
    row: dict = {"league": league, "season": year, "fd_matches": fd_matches(league, year)}
    end = SEASON_END.format(next=year + 1)
    found = []
    for kind, url in (
        ("json", URL.format(league=us, season=year)),
        ("page", PAGE.format(league=us, season=year)),
    ):
        try:
            caps = captures(url.removeprefix("https://"), session)
        except Exception as exc:  # noqa: BLE001  report it, try the other source
            row[f"{kind}_error"] = type(exc).__name__
            caps = []
        row[f"{kind}_captures"] = len(caps)
        found += [{**c, "kind": kind} for c in caps]
    row["captures"] = len(found)
    if not found:
        row["status"] = "no capture"
        return row
    # The latest capture after the season ended first, then the latest of the rest.
    order = sorted(found, key=lambda c: (c["timestamp"] >= end, c["timestamp"]), reverse=True)
    tried = []
    for c in order[:4]:  # at most four downloads a season
        try:
            r = _get(RAW.format(ts=c["timestamp"], url=c["original"]), session=session)
            if r.status_code != 200:
                tried.append(f"{c['timestamp']} HTTP {r.status_code}")
                continue
            data = parse(r.content)
            text = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
            check_league_json(text)
        except Exception as exc:  # noqa: BLE001  try the next capture
            tried.append(f"{c['timestamp']} {type(exc).__name__}: {str(exc)[:80]}")
            continue
        row.update(
            status="ok",
            kind=c["kind"],
            timestamp=c["timestamp"],
            captured=f"{c['timestamp'][:4]}-{c['timestamp'][4:6]}-{c['timestamp'][6:8]}",
            after_season=c["timestamp"] >= end,
            matches=len(data["dates"]),
            played=played(data),
            teams=len(data.get("teams") or {}),
            players=len(data.get("players") or []),
            sha256=hashlib.sha256(text).hexdigest(),
            source="wayback",
            url=RAW.format(ts=c["timestamp"], url=c["original"]),
        )
        if row["fd_matches"] is not None:
            row["complete"] = row["played"] == row["fd_matches"]
        if out_dir is not None:
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / f"understat_{us}_{year}.json").write_bytes(text)
        break
    else:
        row["status"] = "no good capture"
    if tried:
        row["tried"] = tried
    return row


def probe(
    wanted: dict | None = None, out_dir: Path | None = None, minutes: float = 40
) -> list[dict]:
    """Every wanted league-season, in order, within `minutes`."""
    stop = time.time() + minutes * 60
    rows = []
    with requests.Session() as s:
        for league, years in (wanted or WANTED).items():
            for year in years:
                if time.time() > stop:
                    rows.append({"league": league, "season": year, "status": "not reached"})
                    continue
                rows.append(probe_one(league, year, out_dir, s))
    return rows


def table(rows: list[dict]) -> str:
    """The rows as a plain table for the job log."""
    head = (
        f"{'league':6} {'season':7} {'status':15} {'source':5} {'captured':10} "
        f"{'played':>6} {'fd':>4}  ok"
    )
    lines = [head]
    for r in rows:
        ok = {True: "yes", False: "NO"}.get(r.get("complete"), "-")
        lines.append(
            f"{r['league']:6} {r['season']}/{(r['season'] + 1) % 100:02d} {r['status']:15} "
            f"{r.get('kind', '-'):5} {r.get('captured', '-'):10} {r.get('played', '-')!s:>6} "
            f"{r.get('fd_matches', '-')!s:>4}  {ok}"
        )
    return "\n".join(lines)
