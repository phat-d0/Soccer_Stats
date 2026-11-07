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


MAX_DOWNLOADS = 10  # copies tried per season, newest after the season first


def best_capture(league: str, year: int, fd: int | None, session=None) -> tuple[dict, dict | None]:
    """(row, data): the most complete good copy of a league-season. Copies are tried newest
    after the season first, then the newest of the rest; the search stops at the first with
    every football-data match played (`fd`), else keeps the one with the most played results
    (ties: the newer), within MAX_DOWNLOADS downloads. `data` is the decoded file or None."""
    us = LEAGUES[league]
    row: dict = {"league": league, "season": year, "fd_matches": fd}
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
        return row, None
    order = sorted(found, key=lambda c: (c["timestamp"] >= end, c["timestamp"]), reverse=True)
    tried, best, best_data = [], None, None
    for c in order[:MAX_DOWNLOADS]:
        try:
            r = _get(RAW.format(ts=c["timestamp"], url=c["original"]), session=session)
            if r.status_code != 200:
                tried.append(f"{c['timestamp']} HTTP {r.status_code}")
                continue
            data = parse(r.content)
            check_league_json(encode(data))
        except Exception as exc:  # noqa: BLE001  try the next capture
            tried.append(f"{c['timestamp']} {type(exc).__name__}: {str(exc)[:80]}")
            continue
        n = played(data)
        tried.append(f"{c['timestamp']} ok, {n} played")
        if best is None or n > played(best_data):
            best, best_data = c, data
        if fd is not None and n >= fd:
            break
    row["tried"] = tried
    if best is None:
        row["status"] = "no good capture"
        return row, None
    text = encode(best_data)
    row.update(
        status="ok",
        kind=best["kind"],
        timestamp=best["timestamp"],
        captured=f"{best['timestamp'][:4]}-{best['timestamp'][4:6]}-{best['timestamp'][6:8]}",
        after_season=best["timestamp"] >= end,
        matches=len(best_data["dates"]),
        played=played(best_data),
        teams=len(best_data.get("teams") or {}),
        players=len(best_data.get("players") or []),
        sha256=hashlib.sha256(text).hexdigest(),
        source="wayback",
        url=RAW.format(ts=best["timestamp"], url=best["original"]),
    )
    if fd is not None:
        row["complete"] = row["played"] == fd
    return row, best_data


def encode(data: dict) -> bytes:
    """A decoded copy as the bytes saved (compact JSON, UTF-8)."""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()


def probe_one(league: str, year: int, out_dir: Path | None = None, session=None) -> dict:
    """One league-season's row (see the module doc); `out_dir` keeps the decoded file."""
    row, data = best_capture(league, year, fd_matches(league, year), session)
    if data is not None and out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"understat_{LEAGUES[league]}_{year}.json").write_bytes(encode(data))
    return row


def load_wanted(wanted: dict, good_dir: Path, now: str, minutes: float = 40) -> list[dict]:
    """Step 2 for every wanted league-season: its most complete copy, loaded if complete."""
    stop = time.time() + minutes * 60
    pairs = []
    with requests.Session() as s:
        for league, years in wanted.items():
            for year in years:
                if time.time() > stop:
                    pairs.append(
                        ({"league": league, "season": year, "status": "not reached"}, None)
                    )
                    continue
                pairs.append(best_capture(league, year, fd_matches(league, year), s))
    loaded = load(pairs, good_dir, now)
    return [{**row, **res} for (row, _), res in zip(pairs, loaded, strict=True)]


# ---------- step 1: does a copy equal the file we hold? (lead, 7 Nov) ----------

# Seasons we hold from Understat's own JSON endpoint, compared with their archived copies.
COMPARE = {"E0": [2022, 2023], "SP1": [2024], "D1": [2024]}
XG_TOL = 1e-6  # beyond rounding: a revised xG model would move values by far more


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def match_fields(m: dict) -> dict:
    """The fields the xG merge reads, normalised (strings vs numbers don't count as a change)."""
    g, x = m.get("goals") or {}, m.get("xG") or {}
    return {
        "isResult": bool(m.get("isResult")),
        "h": (m.get("h") or {}).get("title"),
        "a": (m.get("a") or {}).get("title"),
        "datetime": m.get("datetime"),
        "goals_h": _num(g.get("h")),
        "goals_a": _num(g.get("a")),
        "xg_h": _num(x.get("h")),
        "xg_a": _num(x.get("a")),
    }


def compare(held: dict, copy: dict) -> dict:
    """Every match of the held file against the copy, by Understat match id: matches in
    each, ids missing on either side, matches with any field different, the fields that
    differ (counts) and the largest xG difference (played matches)."""
    a = {str(m.get("id")): m for m in held.get("dates") or []}
    b = {str(m.get("id")): m for m in copy.get("dates") or []}
    both = sorted(set(a) & set(b))
    fields: dict[str, int] = {}
    differ, max_xg, examples = 0, 0.0, []
    for i in both:
        fa, fb = match_fields(a[i]), match_fields(b[i])
        bad = []
        for k in fa:
            va, vb = fa[k], fb[k]
            if k.startswith("xg_") and va is not None and vb is not None:
                if fa["isResult"] and fb["isResult"]:
                    max_xg = max(max_xg, abs(va - vb))
                if abs(va - vb) > XG_TOL:
                    bad.append(k)
            elif va != vb:
                bad.append(k)
        if bad:
            differ += 1
            for k in bad:
                fields[k] = fields.get(k, 0) + 1
            if len(examples) < 5:
                examples.append({"id": i, "fields": bad, "held": fa, "copy": fb})
    return {
        "held": len(a),
        "copy": len(b),
        "both": len(both),
        "only_held": len(set(a) - set(b)),
        "only_copy": len(set(b) - set(a)),
        "differ": differ,
        "fields": fields,
        "max_xg_diff": round(max_xg, 6),
        "examples": examples,
    }


def compare_held(wanted: dict, good_dir: Path, session=None) -> list[dict]:
    """Step 1: each held league-season (good_dir) against its most complete archived copy."""
    rows = []
    for league, years in wanted.items():
        for year in years:
            path = Path(good_dir) / f"understat_{LEAGUES[league]}_{year}.json"
            if not path.exists():
                rows.append({"league": league, "season": year, "status": "not held"})
                continue
            held = json.loads(path.read_bytes())
            row, data = best_capture(league, year, played(held), session)
            row = {
                k: row[k] for k in ("league", "season", "status", "captured", "played") if k in row
            }
            row["held_played"] = played(held)
            if data is not None:
                row.update(compare(held, data))
            rows.append(row)
    return rows


# ---------- step 2: load the complete copies (lead, 7 Nov) ----------

SOURCES = "sources.jsonl"  # in the good-copy folder: one row per file loaded from elsewhere


def load(rows_data: list[tuple[dict, dict | None]], good_dir: Path, now: str) -> list[dict]:
    """Write each complete copy into `good_dir` through xg.keep_good (which never replaces a
    better file) and log its source in good_dir/SOURCES. A copy is loaded only when its played
    matches equal football-data's count (`complete`); partial copies are skipped. Returns one
    {league, season, loaded, reason?} per row."""
    import tempfile

    from soccer_stats.xg import keep_good

    good_dir = Path(good_dir)
    good_dir.mkdir(parents=True, exist_ok=True)
    out = []
    with tempfile.TemporaryDirectory() as tmp:
        for row, data in rows_data:
            res = {"league": row["league"], "season": row["season"], "loaded": False}
            if data is None or row.get("status") != "ok":
                res["reason"] = row.get("status", "no copy")
            elif not row.get("complete"):
                res["reason"] = f"partial: {row.get('played')} of {row.get('fd_matches')} played"
            else:
                name = f"understat_{LEAGUES[row['league']]}_{row['season']}.json"
                text = encode(data)
                check_league_json(text)
                (Path(tmp) / name).write_bytes(text)
                if keep_good(Path(tmp) / name, good_dir):
                    res["loaded"] = True
                    src = {
                        k: row.get(k)
                        for k in ("league", "season", "source", "url", "captured", "played")
                    }
                    src.update(file=name, sha256=row["sha256"], fd_matches=row["fd_matches"])
                    src["loaded_at"] = now
                    with open(good_dir / SOURCES, "a") as f:
                        f.write(json.dumps(src) + "\n")
                    res["sha256"] = row["sha256"]
                else:
                    res["reason"] = "the good copy held is as good or better"
            out.append(res)
    return out


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
