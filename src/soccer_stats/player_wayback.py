"""Does the Wayback Machine hold Understat's match files? (lead, 8 Nov; counts only, free).

B needs each match's Understat player data (`player_data.MATCH_URL`, getMatchData/<id>).
SP1/D1/I1/F1's files are in no cache and Understat has been down since 2 Nov
(docs/player_props.md §16, note of 8 Nov). This asks web.archive.org's CDX index (free, no
key, through `xg_wayback`'s polite GET) for every 200 capture under two prefixes, the JSON
endpoint and the match page (understat.com/match/<id>), and counts, per league-season, how
many played matches (from the league files in `xg.GOOD_DIR`) have a capture of either.
Nothing is downloaded beyond the index; nothing is written.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from soccer_stats.xg import LEAGUES
from soccer_stats.xg_wayback import CDX, _get

PREFIXES = {
    "api": "understat.com/getMatchData/",
    "page": "understat.com/match/",
}
ID_RE = re.compile(r"/(?:getMatchData|match)/(\d+)")
B_LEAGUES = ("E0", "SP1", "D1", "I1", "F1")
B_YEARS = range(2021, 2027)  # B's features use 2021/22 onward (cli._goal_league_feats)


def played_ids(good_dir: Path, league: str, year: int) -> set[str] | None:
    """Played match ids in one league-season file, or None when the file isn't held."""
    path = Path(good_dir) / f"understat_{LEAGUES[league]}_{year}.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    return {
        str(d["id"])
        for d in data.get("dates") or []
        if isinstance(d, dict) and d.get("id") and d.get("isResult")
    }


def captured_ids(prefix: str, session=None) -> set[str]:
    """Match ids with at least one 200 capture under `prefix`, every CDX page read."""
    base = {
        "url": prefix,
        "matchType": "prefix",
        "filter": "statuscode:200",
        "collapse": "urlkey",
        "fl": "original",
    }
    r = _get(CDX, {**base, "showNumPages": "true"}, session)
    try:
        pages = int((r.text or "0").strip() or 0) if r.status_code == 200 else 0
    except ValueError:
        pages = 0
    ids: set[str] = set()
    for page in range(max(pages, 1)):
        r = _get(CDX, {**base, "page": str(page)}, session)
        if r.status_code != 200:
            continue
        for line in (r.text or "").splitlines():
            m = ID_RE.search(line)
            if m:
                ids.add(m.group(1))
    return ids


def report(good_dir: Path, session=None, leagues=B_LEAGUES, years=B_YEARS) -> dict:
    """Per league-season: played matches, and how many have an archived API answer, an
    archived match page, or either. Pooled per league and overall."""
    got = {k: captured_ids(p, session) for k, p in PREFIXES.items()}
    rows, missing = [], []
    for lg in leagues:
        for y in years:
            ids = played_ids(good_dir, lg, y)
            if ids is None:
                missing.append(f"{lg} {y}")
                continue
            api, page = ids & got["api"], ids & got["page"]
            rows.append(
                {
                    "league": lg,
                    "season": f"{y}/{str(y + 1)[-2:]}",
                    "played": len(ids),
                    "api": len(api),
                    "page": len(page),
                    "either": len(api | page),
                }
            )
    total = {k: sum(r[k] for r in rows) for k in ("played", "api", "page", "either")}
    return {
        "captured": {k: len(v) for k, v in got.items()},
        "rows": rows,
        "by_league": {
            lg: {k: sum(r[k] for r in rows if r["league"] == lg) for k in total} for lg in leagues
        },
        "total": total,
        "share": round(total["either"] / total["played"], 4) if total["played"] else None,
        "league_files_missing": missing,
    }
