"""ESPN as a player-data fallback for Understat (lead, 26 Nov 2026; scoping, free, no key).

Understat has answered "We will come back soon" since 2 Nov 2026; goalscorer B, its paper
track and any shots or assists model read Understat's player match files. ESPN's match
summary (`/summary?event=`, already used for lineups and corners) lists both rosters.
This module asks what it holds per player and how far back.

`fields` (CLI `espn-players --mode fields`): per league (E0, SP1, D1, I1, F1) and season
2021/22-2026/27, `PER_SEASON` matches spread through football-data's season, each found
on ESPN's scoreboard for its day (both names, kickoff within 3 h, as espn_results does) and
its summary fetched once. Per summary: roster players, the starter flag, substitution
flags and times (`subbedIn`/`subbedOut`, the key events' clock), each player's stat names
and whether goals, assists, shots, shots on target and cards are there, and whether the
player stats add up to the match's goals. Nothing is cached or saved beyond the run's
`--out` folder (the artifact); requests are paced (`PAUSE`) and stop on `RUN_SECONDS`.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import requests

from soccer_stats.espn_news import BASE, SLUGS

LEAGUES = ("E0", "SP1", "D1", "I1", "F1")
SEASONS = (2021, 2022, 2023, 2024, 2025, 2026)
PER_SEASON = 6
PAUSE = 0.4
TIMEOUT = 20
RUN_SECONDS = 1500
KICKOFF_TOLERANCE = pd.Timedelta(hours=3)
# The per-player stats a goals/assists/shots model needs, by ESPN's usual names.
WANT = {
    "goals": ("totalGoals", "goals"),
    "assists": ("goalAssists", "assists"),
    "shots": ("totalShots", "shots"),
    "on_target": ("shotsOnTarget",),
    "yellow": ("yellowCards",),
    "red": ("redCards",),
    "sub_in": ("subIns",),
}


def sample(kickoffs: pd.DataFrame, n: int = PER_SEASON) -> pd.DataFrame:
    """n matches spread evenly through a season's played matches (kickoff order)."""
    k = kickoffs.sort_values("kickoff").reset_index(drop=True)
    if len(k) <= n:
        return k
    idx = sorted({int(round(i * (len(k) - 1) / (n - 1))) for i in range(n)})
    return k.iloc[idx].reset_index(drop=True)


def _get(get: Callable, url: str, params: dict, stats: dict) -> dict | None:
    stats["requests"] = stats.get("requests", 0) + 1
    try:
        r = get(url, params=params, timeout=TIMEOUT)
    except requests.RequestException as exc:
        stats["errors"] = stats.get("errors", 0) + 1
        stats["last_error"] = type(exc).__name__
        return None
    time.sleep(PAUSE)
    if r.status_code != 200:
        stats["errors"] = stats.get("errors", 0) + 1
        stats["last_error"] = f"HTTP {r.status_code}"
        return None
    try:
        return r.json()
    except ValueError:
        return None


def find_event(board: dict | None, home: str, away: str, kickoff: pd.Timestamp) -> dict | None:
    """The scoreboard event for this fixture: both names (football-data spelling via
    espn_news.team_name) and kickoff within 3 h."""
    from soccer_stats.espn_news import team_name

    for e in (board or {}).get("events", []) or []:
        comp = ((e.get("competitions") or [{}])[0]) or {}
        teams = {
            c.get("homeAway"): (c.get("team") or {}).get("displayName")
            for c in comp.get("competitors", [])
        }
        known = {home, away}
        h, a = team_name(teams.get("home") or "", known), team_name(teams.get("away") or "", known)
        if (h, a) != (home, away):
            continue
        if abs(pd.Timestamp(e.get("date")).tz_convert("UTC") - kickoff) <= KICKOFF_TOLERANCE:
            return e
    return None


def _stats(p: dict) -> dict[str, float]:
    out = {}
    for s in p.get("stats") or []:
        if isinstance(s, dict) and s.get("name") is not None:
            try:
                out[s["name"]] = float(s.get("value"))
            except (TypeError, ValueError):
                out[s["name"]] = None
    return out


def read_summary(body: dict | None) -> dict:
    """What one summary holds per player (see the module docstring)."""
    body = body if isinstance(body, dict) else {}
    players, names = [], Counter()
    sides = body.get("rosters") or []
    for side in sides:
        for p in side.get("roster") or []:
            st = _stats(p)
            names.update(st)
            players.append(
                {
                    "name": (p.get("athlete") or {}).get("displayName"),
                    "athlete_id": (p.get("athlete") or {}).get("id"),
                    "starter": p.get("starter"),
                    "subbed_in": p.get("subbedIn"),
                    "subbed_out": p.get("subbedOut"),
                    "position": (p.get("position") or {}).get("abbreviation"),
                    "stats": st,
                }
            )
    subs = [
        e
        for e in body.get("keyEvents") or []
        if "substitution" in str((e.get("type") or {}).get("text", "")).lower()
    ]
    have = {k: any(n in names for n in v) for k, v in WANT.items()}
    goal_col = next((n for n in WANT["goals"] if n in names), None)
    player_goals = sum((p["stats"].get(goal_col) or 0) for p in players) if goal_col else None
    comp = (((body.get("header") or {}).get("competitions") or [{}])[0]) or {}
    score = sum(
        float(c.get("score") or 0) for c in comp.get("competitors", []) if isinstance(c, dict)
    )
    return {
        "teams": len(sides),
        "players": len(players),
        "starters": sum(bool(p["starter"]) for p in players),
        "with_stats": sum(bool(p["stats"]) for p in players),
        "starter_flag": any(p["starter"] is not None for p in players),
        "sub_flags": any(p["subbed_in"] is not None for p in players),
        "sub_events": len(subs),
        "sub_clock_example": ((subs[0].get("clock") or {}).get("displayValue") if subs else None),
        "stat_names": sorted(names),
        "have": have,
        "player_goals": player_goals,
        "score_goals": score,
        "example": players[0] if players else None,
    }


def fields(
    leagues=LEAGUES,
    seasons=SEASONS,
    per_season: int = PER_SEASON,
    get: Callable = requests.get,
    kickoffs: Callable | None = None,
    out_dir: Path | None = None,
    seconds: float = RUN_SECONDS,
) -> dict:
    """The fields check (module docstring). `kickoffs(league, season)` gives football-data's
    played matches (home, away, kickoff); default `data.season_fixtures`."""
    kickoffs = kickoffs or fd_matches
    stats: dict = {}
    end = time.monotonic() + seconds
    res: dict = {"leagues": {}, "stats": stats}
    for lg in leagues:
        slug = SLUGS[lg]
        res["leagues"][lg] = {}
        for season in seasons:
            if time.monotonic() > end:
                res["stopped"] = f"time budget {seconds:g} s used"
                break
            try:
                fx = sample(kickoffs(lg, season), per_season)
            except Exception as exc:  # noqa: BLE001  (a missing season is reported, not fatal)
                res["leagues"][lg][season] = {"error": type(exc).__name__}
                continue
            got = []
            for m in fx.to_dict("records"):
                k = pd.Timestamp(m["kickoff"])
                board = _get(
                    get, BASE.format(slug=slug) + "/scoreboard", {"dates": f"{k:%Y%m%d}"}, stats
                )
                e = find_event(board, m["home"], m["away"], k)
                if e is None:
                    got.append({"match": f"{m['home']} v {m['away']}", "found": False})
                    continue
                body = _get(get, BASE.format(slug=slug) + "/summary", {"event": e["id"]}, stats)
                info = read_summary(body)
                info.update(match=f"{m['home']} v {m['away']}", found=True, event_id=e["id"])
                if out_dir is not None and body is not None:
                    path = Path(out_dir) / lg / f"{season}_{e['id']}.json"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps(body))
                got.append(info)
            res["leagues"][lg][season] = {"matches": got}
    return res


def fd_matches(league: str, season: int) -> pd.DataFrame:
    """football-data's played matches for a season: home, away, kickoff (UTC)."""
    from soccer_stats.data import load_matches, season_kickoffs

    m = load_matches([league], [season]).reset_index(drop=True)
    k = season_kickoffs(league, season)
    if len(k) == len(m):
        m = m.assign(kickoff=k.to_numpy())
    else:  # no times: 15:00 UK on the match date
        m = m.assign(
            kickoff=pd.to_datetime(m["date"]).dt.tz_localize("UTC") + pd.Timedelta(hours=15)
        )
    return m[["home", "away", "kickoff"]]


def summary_lines(res: dict) -> list[str]:
    lines = [f"ESPN players fields: {res['stats']}"]
    for lg, by in res["leagues"].items():
        for season, r in by.items():
            if "error" in r:
                lines.append(f"{lg} {season}: football-data error {r['error']}")
                continue
            ms = r["matches"]
            found = [m for m in ms if m.get("found")]
            if not found:
                lines.append(f"{lg} {season}: 0 of {len(ms)} found on ESPN")
                continue
            have = Counter()
            for m in found:
                have.update(k for k, v in m["have"].items() if v)
            adds = sum(
                m["player_goals"] == m["score_goals"]
                for m in found
                if m["player_goals"] is not None
            )
            names = sorted(set().union(*(m["stat_names"] for m in found)))
            n_pl = sum(m["players"] for m in found)
            n_st = sum(m["with_stats"] for m in found)
            starter = sum(m["starter_flag"] for m in found)
            subf = sum(m["sub_flags"] for m in found)
            subs = sum(m["sub_events"] for m in found)
            lines.append(
                f"{lg} {season}: {len(found)}/{len(ms)} found; players {n_pl // len(found)} "
                f"a match, with stats {n_st}/{n_pl}; starter flag {starter}, sub flags {subf}, "
                f"sub events {subs} (e.g. {found[0]['sub_clock_example']}); "
                f"has {dict(have)}; player goals add up in {adds}/{len(found)}"
            )
            lines.append(f"{lg} {season} stat names: {', '.join(names)}")
    return lines


# ---------- the full pull and B-espn (lead's part 4; owner approved the E0 pull, 26 Nov) ----------

POSITIONS = {"G": "GK", "D": "DEF", "M": "MID", "F": "FWD"}
DEFAULT_START_OFF = 70  # minutes for a starter subbed off at an unknown time
DEFAULT_SUB_ON = 20  # minutes for a substitute with an unknown time
B_ESPN_FACTORS = [
    "log_rate",  # his shrunk shots per 90 (ESPN)
    "sot_rate",  # his shrunk on-target share
    "log_goal_rate",  # his shrunk goals per 90, in place of xG
    "pos_def",
    "pos_fwd",
    "log_team_xg",
    "log_opp_conceded",
    "home",
    "game_state",
]


def _minute(clock: dict | None) -> int | None:
    import re

    c = clock or {}
    if c.get("value") not in (None, ""):
        try:
            return int(round(float(c["value"]) / 60))
        except (TypeError, ValueError):
            pass
    m = re.match(r"\s*(\d+)", str(c.get("displayValue") or ""))
    return int(m.group(1)) if m else None


def sub_minutes(body: dict | None) -> dict[str, int]:
    """Athlete id -> the minute he came on or went off, from the key events'
    substitutions (every participant of a substitution, by athlete id)."""
    out: dict[str, int] = {}
    for e in (body or {}).get("keyEvents") or []:
        if "substitution" not in str((e.get("type") or {}).get("text", "")).lower():
            continue
        minute = _minute(e.get("clock"))
        if minute is None:
            continue
        for p in e.get("participants") or []:
            aid = str(((p or {}).get("athlete") or {}).get("id") or "")
            if aid:
                out.setdefault(aid, minute)
    return out


def appearances(body: dict | None, meta: dict) -> list[dict]:
    """One row per player who played (Understat apps' columns), from one summary.
    `meta`: match_id, season, kickoff, home, away (football-data names), espn_home."""
    body = body if isinstance(body, dict) else {}
    subs = sub_minutes(body)
    rows = []
    for side in body.get("rosters") or []:
        espn_team = ((side.get("team") or {}).get("displayName")) or ""
        is_home = (
            side.get("homeAway") == "home"
            if side.get("homeAway")
            else espn_team == meta["espn_home"]
        )
        team = meta["home"] if is_home else meta["away"]
        for p in side.get("roster") or []:
            a = p.get("athlete") or {}
            aid = str(a.get("id") or "")
            st = _stats(p)
            starter = bool(p.get("starter"))
            sub_in = bool(p.get("subbedIn"))
            if not starter and not sub_in:
                continue  # an unused substitute
            known = aid in subs
            if starter:
                minutes = (
                    subs[aid]
                    if (p.get("subbedOut") and known)
                    else (DEFAULT_START_OFF if p.get("subbedOut") else 90)
                )
            else:
                minutes = max(90 - subs[aid], 1) if known else DEFAULT_SUB_ON
            rows.append(
                {
                    "match_id": meta["match_id"],
                    "season": meta["season"],
                    "kickoff": meta["kickoff"],
                    "team": team,
                    "opponent": meta["away"] if is_home else meta["home"],
                    "home": bool(is_home),
                    "player_id": "e" + aid,
                    "athlete_id": aid,
                    "player": a.get("displayName") or "",
                    "position": POSITIONS.get(
                        ((p.get("position") or {}).get("abbreviation") or "")[:1], "MID"
                    ),
                    "minutes": int(minutes),
                    "minutes_known": bool(known or (starter and not p.get("subbedOut"))),
                    "started": starter,
                    "shots": int(st.get("totalShots") or 0),
                    "sot": int(st.get("shotsOnTarget") or 0),
                    "goals": int(st.get("totalGoals") or 0),
                    "assists": int(st.get("goalAssists") or 0),
                    "own_goals": int(st.get("ownGoals") or 0),
                    "yellow": int(st.get("yellowCards") or 0),
                    "red": int(st.get("redCards") or 0),
                    "xg": 0.0,
                    "penalties": 0,
                    "pen_xg": 0.0,
                    "sp_xg": 0.0,
                }
            )
    tot = {}
    for r in rows:
        tot[r["home"]] = tot.get(r["home"], 0) + r["shots"]
    for r in rows:
        r["team_shots"], r["opp_shots"] = tot.get(r["home"], 0), tot.get(not r["home"], 0)
    return rows


def pull(
    league: str = "E0",
    seasons=(2021, 2022, 2023, 2024, 2025),
    get: Callable = requests.get,
    understat_ids: Callable | None = None,
    seconds: float = 5400,
    pause: float = 0.25,
) -> tuple[pd.DataFrame, dict]:
    """Every football-data match in `seasons`: the scoreboard per day (once), the event,
    its summary, the appearance rows. Matches carry Understat's id when its season list
    holds them (`understat_ids(league, season)` -> {(home, away): id}), else "fd:...".
    Returns (rows, coverage per season)."""
    global PAUSE
    PAUSE = pause
    slug = SLUGS[league]
    stats: dict = {}
    end = time.monotonic() + seconds
    rows, cover = [], {}
    boards: dict[str, dict | None] = {}
    for season in seasons:
        fx = fd_matches(league, season)
        ids = (understat_ids or _understat_ids)(league, season)
        sea = f"{season % 100:02d}{(season + 1) % 100:02d}"
        c = {"matches": len(fx), "found": 0, "with_players": 0, "goals_add_up": 0, "stopped": False}
        for m in fx.to_dict("records"):
            if time.monotonic() > end:
                c["stopped"] = True
                break
            k = pd.Timestamp(m["kickoff"])
            found = None
            for day in (f"{k:%Y%m%d}", f"{k - pd.Timedelta(days=1):%Y%m%d}"):
                if day not in boards:
                    boards[day] = _get(
                        get, BASE.format(slug=slug) + "/scoreboard", {"dates": day}, stats
                    )
                found = find_event(boards[day], m["home"], m["away"], k)
                if found:
                    break
            if found is None:
                continue
            c["found"] += 1
            body = _get(get, BASE.format(slug=slug) + "/summary", {"event": found["id"]}, stats)
            comp = ((found.get("competitions") or [{}])[0]) or {}
            espn_home = next(
                (
                    (x.get("team") or {}).get("displayName")
                    for x in comp.get("competitors", [])
                    if x.get("homeAway") == "home"
                ),
                "",
            )
            meta = {
                "match_id": ids.get(
                    (m["home"], m["away"]), f"fd:{m['home']}|{m['away']}|{k:%Y%m%d}"
                ),
                "season": sea,
                "kickoff": k,
                "home": m["home"],
                "away": m["away"],
                "espn_home": espn_home,
            }
            got = appearances(body, meta)
            if got:
                c["with_players"] += 1
                info = read_summary(body)
                c["goals_add_up"] += int(info["player_goals"] == info["score_goals"])
            rows += got
        cover[sea] = c
    cover["requests"] = stats
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(["kickoff", "match_id", "team"]).reset_index(drop=True)
    return df, cover


def _understat_ids(league: str, season: int) -> dict:
    from soccer_stats.player_data import season_matches

    try:
        sm = season_matches(league, season)
    except Exception:  # noqa: BLE001  (no league file: football-data keys only)
        return {}
    return {(r.home, r.away): r.match_id for r in sm.itertuples()}


def map_to_understat(espn: pd.DataFrame, us: pd.DataFrame) -> pd.DataFrame:
    """ESPN rows joined to Understat appearances of the same match by name
    (player_data.match_in_fixture within the two clubs): adds `us_player_id` and the
    Understat goals and shots for that row."""
    from soccer_stats.player_data import match_in_fixture

    out = []
    us_by = {mid: g for mid, g in us.groupby("match_id")}
    for mid, g in espn.groupby("match_id"):
        u = us_by.get(mid)
        if u is None:
            continue
        clubs = {
            t: dict(zip(x["player_id"], x["player"], strict=True)) for t, x in u.groupby("team")
        }
        found, _ = match_in_fixture(list(g["player"]), clubs, overrides={})
        for r in g.itertuples(index=False):
            hit = found.get(r.player)
            if hit and hit[1] == r.team:
                out.append({"match_id": mid, "player_id": r.player_id, "us_player_id": hit[0]})
    m = pd.DataFrame(out, columns=["match_id", "player_id", "us_player_id"])
    u = us[["match_id", "player_id", "goals", "shots", "started"]].rename(
        columns={
            "player_id": "us_player_id",
            "goals": "us_goals",
            "shots": "us_shots",
            "started": "us_started",
        }
    )
    return m.merge(u, on=["match_id", "us_player_id"], how="left")


def goal_rate_features(df: pd.DataFrame) -> pd.DataFrame:
    """log_goal_rate: his goals per 90 shrunk toward the position average (earlier
    kickoffs only), the xG-free stand-in for B's log_xg_rate."""
    from soccer_stats.factors import PRIOR_90S, before_kickoff, ewsum_prior

    df = df.copy()
    df["_90s"] = df["minutes"] / 90
    pos = before_kickoff(df, ["position"], ["goals", "_90s"])
    default = df["position"].map({"GK": 0.002, "DEF": 0.04, "MID": 0.1, "FWD": 0.3}).fillna(0.1)
    pos_rate = (pos["goals"] / pos["_90s"].replace(0, float("nan"))).fillna(default)
    sg, s90 = (
        ewsum_prior(df["goals"].astype(float), df["player_id"]),
        ewsum_prior(df["_90s"], df["player_id"]),
    )
    import numpy as np

    df["log_goal_rate"] = np.log(
        ((sg + pos_rate * PRIOR_90S) / (s90 + PRIOR_90S)).clip(lower=0.002)
    )
    return df.drop(columns=["_90s"])


def b_espn_compare(rows: pd.DataFrame) -> dict:
    """Log loss of B-espn, B and season goals on the same starter rows (descriptive),
    pooled over the development seasons and per season, with paired gains."""
    import numpy as np

    from soccer_stats.lab.metrics import boot_range

    def ll(p, y):
        p = np.clip(np.asarray(p, float), 1e-6, 1 - 1e-6)
        return -(y * np.log(p) + (1 - y) * np.log(1 - p))

    out = {}
    parts = (
        ("development", ("2324", "2425")),
        ("2324", ("2324",)),
        ("2425", ("2425",)),
        ("2526_seen", ("2526",)),
    )
    for name, seas in parts:
        r = rows[rows["season"].isin(seas)]
        if r.empty:
            continue
        y = r["scored"].to_numpy()
        lb, le, lg = ll(r["p_B"], y), ll(r["p_B_espn"], y), ll(r["bench_goals"], y)
        g = r["match_id"].to_numpy()
        out[name] = {
            "rows": len(r),
            "matches": int(r["match_id"].nunique()),
            "rate": round(float(y.mean()), 4),
            "ll_B": round(float(lb.mean()), 5),
            "ll_B_espn": round(float(le.mean()), 5),
            "ll_season_goals": round(float(lg.mean()), 5),
            "B_minus_B_espn": [
                round(float((le - lb).mean()), 5),
                [round(v, 5) for v in (boot_range(le - lb, g) or [])],
            ],
            "B_espn_vs_season_goals": [
                round(float((lg - le).mean()), 5),
                [round(v, 5) for v in (boot_range(lg - le, g) or [])],
            ],
        }
    return out
