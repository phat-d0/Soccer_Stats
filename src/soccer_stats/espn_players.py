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
import shutil
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


def board_names(board: dict | None) -> list[str]:
    """Every club name on a scoreboard (ESPN's displayName)."""
    out = []
    for e in (board or {}).get("events", []) or []:
        comp = ((e.get("competitions") or [{}])[0]) or {}
        out += [
            (c.get("team") or {}).get("displayName")
            for c in comp.get("competitors", [])
            if (c.get("team") or {}).get("displayName")
        ]
    return out


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
    # Own goals count for the other side: the score = players' goals + own goals (lead, 26 Nov).
    own = sum((p["stats"].get("ownGoals") or 0) for p in players)
    scored = sum((p["stats"].get(goal_col) or 0) for p in players) if goal_col else None
    player_goals = scored + own if scored is not None else None
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
        "goals_without_own": scored,
        "own_goals": own,
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
        c = {
            "matches": len(fx),
            "found": 0,
            "with_players": 0,
            "goals_add_up": 0,
            "goals_add_up_without_own": 0,
            "stopped": False,
        }
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
                c["goals_add_up_without_own"] += int(
                    info["goals_without_own"] == info["score_goals"]
                )
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


# --- B-espn, frozen for §16 (amendment of 26 Nov 2026; as run at commit 60e0949) ---------

MODEL_SOURCE = "B-espn"
FEATURE_SOURCE = "espn"
CHECK_SEASON = "2526"  # the four-league check reads 2025/26 development rows only
CHECK_MIN_JOIN = 0.5
XG_WEIGHT = 0.7  # the frozen match model's xG weight (publish.XG_WEIGHT)


def match_xg_weight(matches: pd.DataFrame) -> float:
    """The live model's rule (publish.league_fixtures): XG_WEIGHT when any match has xG, else 0.
    Matches in a season without xG fall back to goals inside DixonColes."""
    has = "home_xg" in matches and matches["home_xg"].notna().any()
    return (
        XG_WEIGHT if has else 0.0
    )  # under half the rescued rows joined = "not checked" (treated as failed)


def b_espn_predict(espn: pd.DataFrame, preds: pd.DataFrame, start, end, holdout=None):
    """B-espn's walk-forward chances on ESPN appearance rows (`appearances` output):
    B's mixture (al.CountCandidate on goals) with `B_ESPN_FACTORS`, team xG from the match
    model's walk-forward `preds`. Returns the feature rows (kickoff < end) with `p_B_espn`,
    `model_source` and `feature_source`."""
    from soccer_stats import player_assist_lab as al
    from soccer_stats import player_backtest as pb
    from soccer_stats import player_goals as pg
    from soccer_stats.factors import build_features
    from soccer_stats.lab import harness

    fe = goal_rate_features(pg.goal_features(build_features(espn, pb.match_info(preds, espn))))
    fe = fe[fe["kickoff"] < pd.Timestamp(end)].reset_index(drop=True)
    pr = harness.walk_forward(
        fe,
        lambda **kw: al.CountCandidate(**kw),
        {"target": "goals", "factors": B_ESPN_FACTORS},
        start,
        end,
        holdout=holdout,
        time_col="kickoff",
        refit=al.REFIT,
        min_train=1,
    )
    p = pr["p_1"] if not pr.empty else float("nan")
    return fe.assign(p_B_espn=p, model_source=MODEL_SOURCE, feature_source=FEATURE_SOURCE)


def check_rows(fe: pd.DataFrame, judged: pd.DataFrame, league: str) -> tuple[pd.DataFrame, dict]:
    """The amendment's check rows: rescued 2025/26 judged rows (`p_B`, `scored`) joined to
    ESPN's starter in the same Understat match by name within the two clubs."""
    from soccer_stats.player_data import match_in_fixture

    j = judged.copy()
    j["match_id"] = j["match_id"].astype(str).str.replace(f"{league}|", "", regex=False)
    j["player_id"] = j["player_id"].astype(str)
    j = j[(j["season"].astype(str) == CHECK_SEASON) & j["started"].astype(bool)]
    st = fe[fe["started"].astype(bool) & fe["p_B_espn"].notna()]
    st = st.assign(match_id=st["match_id"].astype(str))
    by = {m: g for m, g in st.groupby("match_id")}
    out = []
    for mid, g in j.groupby("match_id"):
        e = by.get(mid)
        if e is None:
            continue
        clubs = {
            t: dict(zip(x["player_id"], x["player"], strict=True)) for t, x in g.groupby("team")
        }
        found, _ = match_in_fixture(list(e["player"]), clubs, overrides={})
        pe = {}
        for r in e.itertuples(index=False):
            hit = found.get(r.player)
            if hit and hit[1] == r.team:
                pe[str(hit[0])] = float(r.p_B_espn)
        for r in g.itertuples(index=False):
            if r.player_id in pe:
                out.append(
                    {
                        "match_id": mid,
                        "player_id": r.player_id,
                        "scored": int(r.scored),
                        "p_B": float(r.p_B),
                        "p_B_espn": pe[r.player_id],
                    }
                )
    rows = pd.DataFrame(out, columns=["match_id", "player_id", "scored", "p_B", "p_B_espn"])
    counts = {
        "rescued_rows": len(j),
        "rescued_matches": int(j["match_id"].nunique()),
        "joined_rows": len(rows),
        "join_share": round(len(rows) / len(j), 4) if len(j) else 0.0,
    }
    return rows, counts


def check_verdict(rows: pd.DataFrame, counts: dict) -> dict:
    """B-espn minus B log loss with its 95% range (matches resampled, 2,000 draws, seed 0).
    pass = the range includes 0 or lies below it; fail = it lies above 0; under half the
    rescued rows joined = "not checked" (treated as failed)."""
    import numpy as np

    from soccer_stats.lab.metrics import boot_range

    out = dict(counts)
    if counts.get("join_share", 0) < CHECK_MIN_JOIN or len(rows) < 2:
        return {**out, "verdict": "not checked"}
    y = rows["scored"].to_numpy()

    def ll(p):
        p = np.clip(np.asarray(p, float), 1e-6, 1 - 1e-6)
        return -(y * np.log(p) + (1 - y) * np.log(1 - p))

    lb, le = ll(rows["p_B"]), ll(rows["p_B_espn"])
    d = le - lb
    lo, hi = boot_range(d, rows["match_id"].to_numpy(), n_boot=2000, seed=0)
    return {
        **out,
        "matches": int(rows["match_id"].nunique()),
        "rate": round(float(y.mean()), 4),
        "ll_B": round(float(lb.mean()), 5),
        "ll_B_espn": round(float(le.mean()), 5),
        "B_espn_minus_B": round(float(d.mean()), 5),
        "range": [round(lo, 5), round(hi, 5)],
        "verdict": "fail" if lo > 0 else "pass",
    }


# --- The ESPN appearance cache (the goalscorer paper track on B-espn, §16 amendment) ------
#
# One file per football-data match: data/raw/espn_players/<league>/<season>/<key>.json with
# the match's appearance rows (`appearances`), or a not-found marker retried a few times.
# Seeded per league by odds-check.yml `task=espn-seed` (cache espn-seed-<league>-, the
# league's folder only, merged file by file: `merge_dirs`; espn-players-<league>- before);
# publish's paper run tops it up with the matches played since, within its time budget, and
# its raw-data cache carries the files on. Match ids are football-data's ("fd:home|away|date"),
# so the cache never depends on Understat.

CACHE_ROOT = "espn_players"
SETTLED = pd.Timedelta(hours=3)  # a match is fetched once it kicked off this long ago
RETRY_TRIES = 3  # a match ESPN didn't list (or listed without players) is tried this often
RETRY_AFTER = pd.Timedelta(hours=6)
RETRY_WITHIN = pd.Timedelta(days=14)  # only recent matches are retried


def cache_dir(league: str, raw_dir=None) -> Path:
    from soccer_stats.data import RAW_DIR

    return Path(raw_dir or RAW_DIR) / CACHE_ROOT / league


def season_code(year: int) -> str:
    return f"{year % 100:02d}{(year + 1) % 100:02d}"


def match_key(home: str, away: str, kickoff) -> str:
    k = pd.Timestamp(kickoff)
    k = k.tz_localize("UTC") if k.tz is None else k.tz_convert("UTC")
    return f"{k:%Y%m%d}_{home}_{away}".replace("/", "-")


def match_file(league: str, year: int, home: str, away: str, kickoff, raw_dir=None) -> Path:
    return cache_dir(league, raw_dir) / season_code(year) / f"{match_key(home, away, kickoff)}.json"


def _due(
    path: Path, kickoff: pd.Timestamp, now: pd.Timestamp, refetch: frozenset = frozenset()
) -> bool:
    """A match to fetch: never held, or a not-found marker worth another try. A marker for
    a club in `refetch` (a name the mapping has just learned) is fetched again whatever its
    age or tries."""
    if not path.exists():
        return True
    try:
        rec = json.loads(path.read_text())
    except (OSError, ValueError):
        return True
    if rec.get("found"):
        return False
    if refetch and {rec.get("home"), rec.get("away")} & refetch:
        return True
    tried = pd.Timestamp(rec.get("fetched_at") or "1970-01-01T00:00:00Z")
    return (
        int(rec.get("tries", 1)) < RETRY_TRIES
        and now - kickoff <= RETRY_WITHIN
        and now - tried >= RETRY_AFTER
    )


def fill_cache(
    league: str,
    seasons,
    raw_dir=None,
    get: Callable = requests.get,
    seconds: float = 60.0,
    now: pd.Timestamp | None = None,
    pause: float | None = None,
    matches: Callable[[str, int], pd.DataFrame] | None = None,
    refetch=(),
) -> dict:
    """Fetch every settled football-data match of `seasons` the cache lacks (oldest first),
    until `seconds` run out. Returns counts: due, fetched, found, not_found, left, requests.
    `refetch`: football-data clubs whose not-found markers are fetched again (see `_due`)."""
    refetch = frozenset(refetch or ())
    global PAUSE
    if pause is not None:
        PAUSE = pause
    now = now or pd.Timestamp.now(tz="UTC")
    end = time.monotonic() + seconds
    slug = SLUGS[league]
    stats: dict = {}
    boards: dict[str, dict | None] = {}
    out = {"due": 0, "fetched": 0, "found": 0, "not_found": 0, "left": 0, "stopped": False}
    for year in seasons:
        try:
            fx = (matches or fd_matches)(league, year)
        except Exception as exc:  # noqa: BLE001  no football-data file for that season
            out.setdefault("errors", []).append(f"{year}: {type(exc).__name__}")
            continue
        for m in fx.sort_values("kickoff").to_dict("records"):
            k = pd.Timestamp(m["kickoff"])
            k = k.tz_localize("UTC") if k.tz is None else k.tz_convert("UTC")
            if k > now - SETTLED:
                continue
            path = match_file(league, year, m["home"], m["away"], k, raw_dir)
            if not _due(path, k, now, refetch):
                continue
            out["due"] += 1
            if time.monotonic() > end:
                out["left"] += 1
                out["stopped"] = True
                continue
            found = None
            for day in (f"{k:%Y%m%d}", f"{k - pd.Timedelta(days=1):%Y%m%d}"):
                if day not in boards:
                    boards[day] = _get(
                        get, BASE.format(slug=slug) + "/scoreboard", {"dates": day}, stats
                    )
                found = find_event(boards[day], m["home"], m["away"], k)
                if found:
                    break
            rows, event_id, seen = [], None, []
            if found is None:  # ESPN's names that day, so a missing spelling shows
                for day in (f"{k:%Y%m%d}", f"{k - pd.Timedelta(days=1):%Y%m%d}"):
                    seen += board_names(boards.get(day))
            if found is not None:
                event_id = str(found.get("id"))
                body = _get(get, BASE.format(slug=slug) + "/summary", {"event": event_id}, stats)
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
                    "match_id": f"fd:{m['home']}|{m['away']}|{k:%Y%m%d}",
                    "season": season_code(year),
                    "kickoff": k,
                    "home": m["home"],
                    "away": m["away"],
                    "espn_home": espn_home,
                }
                rows = appearances(body, meta)
            prev = {}
            if path.exists():
                try:
                    prev = json.loads(path.read_text())
                except (OSError, ValueError):
                    prev = {}
            rec = {
                "league": league,
                "season": season_code(year),
                "home": m["home"],
                "away": m["away"],
                "kickoff": k.isoformat(),
                "event_id": event_id,
                "found": bool(rows),
                "tries": int(prev.get("tries", 0)) + 1,
                "fetched_at": now.isoformat(),
                "rows": [{**r, "kickoff": pd.Timestamp(r["kickoff"]).isoformat()} for r in rows],
            }
            if not rows:
                rec["board_names"] = sorted(set(seen))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(rec, separators=(",", ":")))
            out["fetched"] += 1
            out["found" if rows else "not_found"] += 1
    out["requests"] = stats
    return out


def _record(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _rank(rec: dict | None) -> tuple:
    """Which copy of a match file to keep: a found match beats a not-found marker, then the
    later fetch, then (markers) more tries. An unreadable file ranks last."""
    if rec is None:
        return (-1,)
    return (bool(rec.get("found")), str(rec.get("fetched_at") or ""), int(rec.get("tries", 0)))


def merge_dirs(src, dst, leagues=None) -> dict:
    """Fold the match files under `src` into `dst` (both laid out <league>/<season>/<file>),
    copying a file only where `src`'s copy ranks above `dst`'s (`_rank`). The seed shards
    (one league each) and raw-data's copy both hold match files; restoring one over the other
    let an older copy win (27 Nov), so they are merged file by file instead."""
    src, dst = Path(src), Path(dst)
    out = {"copied": 0, "kept": 0}
    if not src.exists():
        return out
    for lg in sorted(leagues or [d.name for d in src.iterdir() if d.is_dir()]):
        for f in sorted((src / lg).rglob("*.json")) if (src / lg).exists() else []:
            target = dst / f.relative_to(src)
            if _rank(_record(f)) > _rank(_record(target)) if target.exists() else True:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, target)
                out["copied"] += 1
            else:
                out["kept"] += 1
    return out


def load_cache(league: str, seasons=None, raw_dir=None) -> tuple[pd.DataFrame, dict]:
    """Every cached appearance row for `league` (all seasons held, or `seasons` as years),
    in kickoff order, and per season {matches, not_found}."""
    base = cache_dir(league, raw_dir)
    want = None if seasons is None else {season_code(y) for y in seasons}
    rows, counts = [], {}
    for d in sorted(base.iterdir()) if base.exists() else []:
        if not d.is_dir() or (want is not None and d.name not in want):
            continue
        c = counts.setdefault(d.name, {"matches": 0, "not_found": 0})
        for f in sorted(d.glob("*.json")):
            try:
                rec = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            if rec.get("found"):
                c["matches"] += 1
                rows += rec.get("rows") or []
            else:
                c["not_found"] += 1
    df = pd.DataFrame(rows)
    if not df.empty:
        df["kickoff"] = pd.to_datetime(df["kickoff"], utc=True)
        df = df.sort_values(["kickoff", "match_id", "team"]).reset_index(drop=True)
    return df, counts


def cached_match(league: str, home: str, away: str, kickoff, raw_dir=None) -> dict | None:
    """One match's cache record (any season folder), or None."""
    k = pd.Timestamp(kickoff)
    k = k.tz_localize("UTC") if k.tz is None else k.tz_convert("UTC")
    year = k.year if k.month >= 7 else k.year - 1
    path = match_file(league, year, home, away, k, raw_dir)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


SEEDED_MIN = 200  # a season counts as seeded with this many matches held


def judged_ready(
    league: str,
    seasons,
    raw_dir=None,
    now: pd.Timestamp | None = None,
    matches: Callable[[str, int], pd.DataFrame] | None = None,
) -> tuple[bool, dict]:
    """The guard for §16's 1 Feb judged read on B-espn (lead, 27 Nov), stricter than the paper
    track's: every season but the last holds at least SEEDED_MIN matches, and no settled
    football-data match of any season lacks a cache file (a not-found marker counts as tried).
    Returns (ready, {short: [seasons under SEEDED_MIN], unfetched: {season: n}})."""
    now = now or pd.Timestamp.now(tz="UTC")
    seasons = list(seasons)
    _rows, counts = load_cache(league, seasons, raw_dir)
    short = [
        season_code(y)
        for y in seasons[:-1]
        if counts.get(season_code(y), {}).get("matches", 0) < SEEDED_MIN
    ]
    unfetched: dict[str, int] = {}
    for y in seasons:
        try:
            fx = (matches or fd_matches)(league, y)
        except Exception:  # noqa: BLE001  no football-data file: the season can't be checked
            unfetched[season_code(y)] = -1
            continue
        n = 0
        for m in fx.to_dict("records"):
            k = pd.Timestamp(m["kickoff"])
            k = k.tz_localize("UTC") if k.tz is None else k.tz_convert("UTC")
            if (
                k <= now - SETTLED
                and not match_file(league, y, m["home"], m["away"], k, raw_dir).exists()
            ):
                n += 1
        if n:
            unfetched[season_code(y)] = n
    return not short and not unfetched, {"short": short, "unfetched": unfetched}


def not_found_clubs(league: str, seasons=None, raw_dir=None) -> dict[str, dict[str, int]]:
    """Per season, the football-data clubs in matches ESPN's scoreboard didn't list (the
    not-found markers): a club missing all its matches is a name the mapping lacks."""
    base = cache_dir(league, raw_dir)
    want = None if seasons is None else {season_code(y) for y in seasons}
    out: dict[str, dict[str, int]] = {}
    for d in sorted(base.iterdir()) if base.exists() else []:
        if not d.is_dir() or (want is not None and d.name not in want):
            continue
        c: Counter = Counter()
        for f in d.glob("*.json"):
            try:
                rec = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            if not rec.get("found"):
                c[rec.get("home")] += 1
                c[rec.get("away")] += 1
        if c:
            out[d.name] = dict(c.most_common(6))
    return out


def unmapped_board_names(league: str, seasons=None, raw_dir=None) -> dict[str, int]:
    """ESPN names seen on the scoreboards of not-found matches that land on no club of that
    league-season's football-data matches: the spellings `espn_news.ESPN_NAMES` lacks."""
    from soccer_stats.espn_news import team_name

    base = cache_dir(league, raw_dir)
    want = None if seasons is None else {season_code(y) for y in seasons}
    c: Counter = Counter()
    for d in sorted(base.iterdir()) if base.exists() else []:
        if not d.is_dir() or (want is not None and d.name not in want):
            continue
        recs = []
        for f in d.glob("*.json"):
            try:
                recs.append(json.loads(f.read_text()))
            except (OSError, ValueError):
                continue
        known = {r.get(s) for r in recs for s in ("home", "away")} - {None}
        for r in recs:
            if not r.get("found"):
                c.update(n for n in r.get("board_names") or [] if team_name(n, known) not in known)
    return dict(c.most_common(12))


DIAGNOSE_DAYS = 3  # how far either side of football-data's date the diagnostic looks


def _board_events(board: dict | None, known: set[str]) -> list[dict]:
    from soccer_stats.espn_news import team_name

    out = []
    for e in (board or {}).get("events", []) or []:
        comp = ((e.get("competitions") or [{}])[0]) or {}
        names = {
            c.get("homeAway"): (c.get("team") or {}).get("displayName") or ""
            for c in comp.get("competitors", [])
        }
        out.append(
            {
                "id": str(e.get("id")),
                "date": e.get("date"),
                "status": ((e.get("status") or {}).get("type") or {}).get("name"),
                "home": team_name(names.get("home", ""), known),
                "away": team_name(names.get("away", ""), known),
                "espn_home": names.get("home"),
                "espn_away": names.get("away"),
            }
        )
    return out


def diagnose(
    league: str,
    seasons,
    raw_dir=None,
    get: Callable = requests.get,
    seconds: float = 600.0,
    pause: float | None = None,
) -> dict:
    """For each not-found marker in `seasons` (free, reads only, writes nothing): ESPN's
    scoreboards from DIAGNOSE_DAYS before to after football-data's date, every event with
    either club (id, date, offset in days, status, both names as ESPN and as mapped), and
    for an event with both clubs whether its summary has player rows. Each marker gets a
    `cause`: "rescheduled" (both clubs on another day), "no_players" (found on the day but
    the summary has no player rows), "not_listed" (neither found), or "unchecked" (out of
    time)."""
    global PAUSE
    if pause is not None:
        PAUSE = pause
    end = time.monotonic() + seconds
    slug = SLUGS[league]
    stats: dict = {}
    boards: dict[str, dict | None] = {}
    base = cache_dir(league, raw_dir)
    want = {season_code(y) for y in seasons}
    rows = []
    for d in sorted(base.iterdir()) if base.exists() else []:
        if not d.is_dir() or d.name not in want:
            continue
        for f in sorted(d.glob("*.json")):
            rec = _record(f)
            if not rec or rec.get("found"):
                continue
            home, away = rec.get("home"), rec.get("away")
            k = pd.Timestamp(rec.get("kickoff"))
            k = k.tz_localize("UTC") if k.tz is None else k.tz_convert("UTC")
            row = {"season": d.name, "home": home, "away": away, "kickoff": k.isoformat()}
            if time.monotonic() > end:
                rows.append({**row, "cause": "unchecked"})
                continue
            seen = []
            for off in range(-DIAGNOSE_DAYS, DIAGNOSE_DAYS + 1):
                day = f"{k + pd.Timedelta(days=off):%Y%m%d}"
                if day not in boards:
                    boards[day] = _get(
                        get, BASE.format(slug=slug) + "/scoreboard", {"dates": day}, stats
                    )
                for e in _board_events(boards[day], {home, away}):
                    if {e["home"], e["away"]} & {home, away}:
                        when = pd.Timestamp(e["date"]).tz_convert("UTC") if e["date"] else None
                        e["offset_days"] = (
                            round((when - k).total_seconds() / 86400, 2) if when else None
                        )
                        if all(x["id"] != e["id"] for x in seen):
                            seen.append(e)
            both = [e for e in seen if (e["home"], e["away"]) == (home, away)]
            row["events"] = seen
            if not both:
                row["cause"] = "not_listed"
            else:
                e = min(both, key=lambda x: abs(x["offset_days"] or 99))
                body = _get(get, BASE.format(slug=slug) + "/summary", {"event": e["id"]}, stats)
                players = sum(len(s.get("roster") or []) for s in (body or {}).get("rosters") or [])
                row.update(event_id=e["id"], offset_days=e["offset_days"], summary_players=players)
                near = e["offset_days"] is not None and abs(e["offset_days"]) <= 1.2
                row["cause"] = (
                    "no_players" if near and not players else "rescheduled" if not near else "ok"
                )
            rows.append(row)
    causes = Counter(r["cause"] for r in rows)
    by_season = {
        s: dict(Counter(r["cause"] for r in rows if r["season"] == s))
        for s in sorted({r["season"] for r in rows})
    }
    return {"league": league, "markers": len(rows), "causes": dict(causes),
            "by_season": by_season, "requests": stats, "rows": rows}  # fmt: skip
