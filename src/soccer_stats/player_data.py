"""Player shots, shots on target and minutes per match, from Understat's match data.

Understat's league-season file (the one xg.py already reads) lists every match with its
id; each match's own file lists every shot (player, minute, result) and both rosters
(minutes, position, whether he started). One cached file per match; played matches
never change, so each is downloaded once.

Shots on target = goals + saved shots (Understat results "Goal" and "SavedShot"). Own
goals are not the shooter's shots. DraftKings settles on its own data provider, which can
differ on blocked shots and deflections; reconcile() measures the mismatch on a sample.

Player names: one id map across Understat, FPL and The Odds API. Names are matched within
a team after normalising (accents, case, punctuation), first on the full name, then on a
unique surname. Anything ambiguous or unmatched is skipped and counted, never guessed;
known exceptions go in player_names.csv next to this file.
"""

from __future__ import annotations

import csv
import json
import re
import time
import unicodedata
from collections.abc import Iterable
from pathlib import Path

import pandas as pd

from soccer_stats.data import RAW_DIR, _fetch
from soccer_stats.xg import HEADERS, TEAM_NAMES, fetch_season

MATCH_URL = "https://understat.com/getMatchData/{id}"
ON_TARGET = {"Goal", "SavedShot"}
POSITION_GROUPS = {
    "GK": "GK",
    "DR": "DEF",
    "DC": "DEF",
    "DL": "DEF",
    "DMR": "MID",
    "DMC": "MID",
    "DML": "MID",
    "MR": "MID",
    "MC": "MID",
    "ML": "MID",
    "AMR": "MID",
    "AMC": "MID",
    "AML": "MID",
    "FWR": "FWD",
    "FW": "FWD",
    "FWL": "FWD",
}
OVERRIDES = Path(__file__).with_name("player_names.csv")


def _team(name: str) -> str:
    return TEAM_NAMES.get(name, name)


def season_matches(league: str, start_year: int, raw_dir: Path = RAW_DIR) -> pd.DataFrame:
    """Every match in a season with its Understat id, kickoff (UTC) and whether it's played."""
    data = json.loads(fetch_season(league, start_year, raw_dir).read_text())
    dates = data.get("dates", []) if isinstance(data, dict) else data
    rows = [
        {
            "match_id": str(d["id"]),
            "kickoff": d["datetime"],
            "home": _team(d["h"]["title"]),
            "away": _team(d["a"]["title"]),
            "played": bool(d.get("isResult")),
        }
        for d in dates
    ]
    df = pd.DataFrame(rows, columns=["match_id", "kickoff", "home", "away", "played"])
    df["kickoff"] = pd.to_datetime(df["kickoff"]).dt.tz_localize("UTC")
    df["season"] = f"{start_year % 100:02d}{(start_year + 1) % 100:02d}"
    return df.sort_values("kickoff").reset_index(drop=True)


def match_path(match_id: str, raw_dir: Path = RAW_DIR) -> Path:
    return raw_dir / "understat_matches" / f"{match_id}.json"


def fetch_match(match_id: str, raw_dir: Path = RAW_DIR) -> Path:
    return _fetch(MATCH_URL.format(id=match_id), match_path(match_id, raw_dir), None, HEADERS)


def parse_match(data: dict, meta: dict) -> list[dict]:
    """One row per player who played: minutes, start, position, shots, shots on target."""
    shots = data.get("shots", {})
    rosters = data.get("rosters", {})
    counts: dict[str, list[float]] = {}  # shots, on target, goals, xG
    pens: dict[str, int] = {}
    team_shots = {"h": 0, "a": 0}
    for side in ("h", "a"):
        for s in shots.get(side, []) or []:
            if s.get("result") == "OwnGoal":
                continue
            pid = str(s.get("player_id"))
            c = counts.setdefault(pid, [0, 0, 0, 0.0])
            c[0] += 1
            c[1] += s.get("result") in ON_TARGET
            c[2] += s.get("result") == "Goal"
            c[3] += float(s.get("xG") or 0)
            team_shots[side] += 1
            if s.get("situation") == "Penalty":
                pens[pid] = pens.get(pid, 0) + 1
    rows = []
    for side in ("h", "a"):
        roster = rosters.get(side, {}) or {}
        players = roster.values() if isinstance(roster, dict) else roster
        for p in players:
            minutes = int(float(p.get("time") or 0))
            if minutes <= 0:
                continue
            pid = str(p.get("player_id"))
            pos = p.get("position") or ""
            started = pos != "Sub" and not int(float(p.get("roster_in") or 0))
            sh, sot, goals, xg = counts.get(pid, [0, 0, 0, 0.0])
            rows.append(
                {
                    "match_id": meta["match_id"],
                    "season": meta.get("season"),
                    "kickoff": meta["kickoff"],
                    "team": meta["home"] if side == "h" else meta["away"],
                    "opponent": meta["away"] if side == "h" else meta["home"],
                    "home": side == "h",
                    "player_id": pid,
                    "player": p.get("player", ""),
                    "position": POSITION_GROUPS.get(pos, "SUB" if pos == "Sub" else "MID"),
                    "minutes": minutes,
                    "started": bool(started),
                    "shots": int(sh),
                    "sot": int(sot),
                    "goals": int(goals),
                    "xg": round(xg, 3),
                    "penalties": pens.get(pid, 0),
                    "team_shots": team_shots[side],
                    "opp_shots": team_shots["a" if side == "h" else "h"],
                }
            )
    return rows


def load_appearances(
    league: str,
    years: Iterable[int],
    raw_dir: Path = RAW_DIR,
    max_new: int | None = None,
    pause: float = 0.3,
) -> tuple[pd.DataFrame, int]:
    """Player appearances for played matches; returns (appearances, matches still missing).

    Downloads at most `max_new` new match files per call (so a scheduled build catches up
    gradually instead of hammering Understat); cached files are always used.
    """
    frames, missing, fetched = [], 0, 0
    for y in years:
        for m in season_matches(league, y, raw_dir).query("played").to_dict("records"):
            path = match_path(m["match_id"], raw_dir)
            if not path.exists():
                if max_new is not None and fetched >= max_new:
                    missing += 1
                    continue
                try:
                    fetch_match(m["match_id"], raw_dir)
                    fetched += 1
                    time.sleep(pause)
                except Exception:
                    missing += 1
                    continue
            frames.extend(parse_match(json.loads(path.read_text()), m))
    df = pd.DataFrame(frames)
    if not df.empty:
        df = df.sort_values(["kickoff", "match_id", "team"]).reset_index(drop=True)
        df["position"] = _fill_positions(df)
    return df, missing


def _fill_positions(df: pd.DataFrame) -> pd.Series:
    """Substitutes are listed as 'Sub': use the position each player starts in most."""
    starts = df[df["position"] != "SUB"].groupby("player_id")["position"]
    usual = starts.agg(lambda s: s.value_counts().index[0])
    return df["position"].where(df["position"] != "SUB", df["player_id"].map(usual)).fillna("MID")


# ---------- names ----------


def norm(name: str) -> str:
    """'Martin Ødegaard' -> 'martin odegaard'."""
    s = unicodedata.normalize("NFKD", name.replace("ø", "o").replace("Ø", "O"))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return " ".join(re.sub(r"[^a-z ]+", " ", s).split())


def load_overrides(path: Path = OVERRIDES) -> dict[tuple[str, str], str]:
    """(team, name as spelled elsewhere) -> Understat player name."""
    if not path.exists():
        return {}
    with open(path) as f:
        return {(r["team"], norm(r["name"])): r["understat_name"] for r in csv.DictReader(f)}


def match_names(
    names: Iterable[str],
    team: str,
    candidates: dict[str, str],
    overrides: dict[tuple[str, str], str] | None = None,
) -> tuple[dict[str, str], list[str]]:
    """Map names from another source to Understat player ids within one team.

    `candidates` maps Understat player id -> name for that team. Returns (name -> id,
    unmatched names). Matches: an override; the same normalised full name; or a unique
    surname (the last word, or the whole name when the other side is a single name such
    as FPL's web names). Ties are never broken by guessing.
    """
    overrides = overrides or {}
    by_name = {norm(n): pid for pid, n in candidates.items()}
    by_last: dict[str, list[str]] = {}
    for pid, n in candidates.items():
        parts = norm(n).split()
        if parts:
            by_last.setdefault(parts[-1], []).append(pid)
    out, unmatched = {}, []
    for name in names:
        key = norm(name)
        if (team, key) in overrides and norm(overrides[(team, key)]) in by_name:
            out[name] = by_name[norm(overrides[(team, key)])]
            continue
        if key in by_name:
            out[name] = by_name[key]
            continue
        parts = key.split()
        last = parts[-1] if parts else ""
        hits = by_last.get(last, [])
        if len(parts) == 1 and len(hits) != 1:  # single name: try any word of the full names
            hits = [pid for pid, n in candidates.items() if key in norm(n).split()]
        if len(hits) == 1 and (
            len(parts) == 1 or norm(candidates[hits[0]]).split()[0][0] == parts[0][0]
        ):
            out[name] = hits[0]
        else:
            unmatched.append(name)
    return out, unmatched


def reconcile(ours: pd.DataFrame, official: pd.DataFrame) -> dict:
    """Mismatch rate between Understat counts and an official sample.

    Both frames have player, kickoff date, shots and sot; `official` is a hand-collected
    sample (e.g. from the bookmaker's grading or a stats site).
    """
    m = ours.assign(day=pd.to_datetime(ours["kickoff"]).dt.date).merge(
        official.assign(day=pd.to_datetime(official["kickoff"]).dt.date),
        on=["player", "day"],
        suffixes=("", "_official"),
    )
    if m.empty:
        return {"matched": 0}
    return {
        "matched": len(m),
        "shots_mismatch": float((m["shots"] != m["shots_official"]).mean()),
        "sot_mismatch": float((m["sot"] != m["sot_official"]).mean()),
    }


def match_in_fixture(
    names: Iterable[str],
    rosters: dict[str, dict[str, str]],
    overrides: dict[tuple[str, str], str] | None = None,
) -> tuple[dict[str, tuple[str, str]], list[str]]:
    """Map names (which don't say their team) to (player id, team) across a match's teams.

    `rosters` maps team -> {Understat player id: name}. An exact (normalised) full-name
    match wins; otherwise match_names' rules within each team. A name that fits both
    teams equally, or neither, is left unmatched.
    """
    overrides = overrides if overrides is not None else load_overrides()
    exact = {team: {norm(n): pid for pid, n in r.items()} for team, r in rosters.items()}
    out, unmatched = {}, []
    for name in dict.fromkeys(names):
        key = norm(name)
        hits = [(exact[t][key], t) for t in rosters if key in exact[t]]
        if not hits:
            for team, r in rosters.items():
                got, _ = match_names([name], team, r, overrides)
                if name in got:
                    hits.append((got[name], team))
        if len(hits) == 1:
            out[name] = hits[0]
        else:
            unmatched.append(name)
    return out, unmatched


def season_stats(apps: pd.DataFrame) -> list[dict]:
    """Shooting stats per player, club and season (a player who moved has a row per club)."""
    if apps.empty:
        return []
    df = apps.copy()
    for col in ("goals", "xg"):
        if col not in df:
            df[col] = 0
    g = df.groupby(["season", "team", "player_id"])
    out = g.agg(
        player=("player", "last"),
        position=("position", "last"),
        apps=("match_id", "nunique"),
        starts=("started", "sum"),
        minutes=("minutes", "sum"),
        shots=("shots", "sum"),
        sot=("sot", "sum"),
        goals=("goals", "sum"),
        xg=("xg", "sum"),
        last=("kickoff", "max"),
    ).reset_index()
    out["last"] = pd.to_datetime(out["last"]).dt.strftime("%Y-%m-%d")
    team_games = df.groupby(["season", "team"])["match_id"].nunique()
    out["team_games"] = [
        team_games[(se, t)] for se, t in zip(out["season"], out["team"], strict=True)
    ]
    out["xg"] = out["xg"].round(2)
    return out.sort_values(["season", "shots"], ascending=[False, False]).to_dict("records")
