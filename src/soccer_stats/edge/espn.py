"""Player shot counts: Understat (what the model learns) vs ESPN (an Opta-style feed).

FanDuel settles player shots on its own provider's count. If Understat counted fewer
shots than that provider, every over would look overpriced against our results even at a
fair price. This compares, player-match by player-match, Understat's shots and shots on
target with ESPN's match summaries (site.api.espn.com, reachable from GitHub Actions;
FBref blocks automated clients and FotMob's API needs signed requests).

Names are matched per match date with player_data.match_names (exact normalised name,
then a unique surname); anything ambiguous is skipped and counted.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import numpy as np
import pandas as pd
import requests

from soccer_stats.player_data import match_names

BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1"
SHOT_KEYS = ("totalShots", "shots")
SOT_KEYS = ("shotsOnTarget",)
STAT_NAMES: set[str] = set()  # every player stat name seen, for the job log


def event_ids(scoreboard: dict) -> list[str]:
    return [str(e["id"]) for e in scoreboard.get("events", []) if e.get("id")]


def parse_summary(summary: dict) -> pd.DataFrame:
    """One row per player who played: name, team, starter, shots, shots on target."""
    rows = []
    for side in summary.get("rosters", []) or []:
        team = (side.get("team") or {}).get("displayName")
        for p in side.get("roster", []) or []:
            if not (p.get("starter") or p.get("subbedIn")):
                continue
            stats = {s.get("name"): s.get("value") for s in p.get("stats", []) or []}
            shots = next((stats[k] for k in SHOT_KEYS if stats.get(k) is not None), None)
            sot = next((stats[k] for k in SOT_KEYS if stats.get(k) is not None), None)
            if shots is None:
                continue
            rows.append(
                {
                    "player": (p.get("athlete") or {}).get("displayName", ""),
                    "espn_team": team,
                    "starter": bool(p.get("starter")),
                    "espn_shots": float(shots),
                    "espn_sot": float(sot) if sot is not None else np.nan,
                }
            )
    cols = ["player", "espn_team", "starter", "espn_shots", "espn_sot"]
    return pd.DataFrame(rows, columns=cols)


def stat_names(summary: dict) -> list[str]:
    """Stat names ESPN lists for players (to diagnose a renamed field)."""
    names = set()
    for side in summary.get("rosters", []) or []:
        for p in side.get("roster", []) or []:
            names.update(s.get("name") for s in p.get("stats", []) or [] if s.get("name"))
    return sorted(names)


def fetch_day(day: pd.Timestamp, get: Callable = requests.get, pause: float = 0.3) -> pd.DataFrame:
    """ESPN player shot counts for every EPL match on `day` (empty on any failure)."""
    try:
        r = get(f"{BASE}/scoreboard", params={"dates": day.strftime("%Y%m%d")}, timeout=30)
        ids = event_ids(r.json()) if r.ok else []
    except (requests.RequestException, ValueError):
        return pd.DataFrame()
    frames = []
    for i in ids:
        try:
            r = get(f"{BASE}/summary", params={"event": i}, timeout=30)
            if r.ok:
                body = r.json()
                f = parse_summary(body)
                STAT_NAMES.update(stat_names(body))
                f["espn_event"] = i
                frames.append(f)
        except (requests.RequestException, ValueError):
            continue
        time.sleep(pause)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def join_day(apps_day: pd.DataFrame, espn_day: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Understat appearances on one date beside ESPN's rows; returns (joined, unmatched)."""
    if apps_day.empty or espn_day.empty:
        return pd.DataFrame(), 0
    cands = dict(zip(apps_day["player_id"].astype(str), apps_day["player"], strict=True))
    names = espn_day["player"].drop_duplicates().tolist()
    found, unmatched = match_names(names, "", cands)
    e = espn_day[espn_day["player"].isin(found)].copy()
    e["player_id"] = e["player"].map(found)
    e = e.drop_duplicates("player_id", keep=False)  # one id claimed twice: skip both
    a = apps_day.assign(player_id=apps_day["player_id"].astype(str))
    j = a.merge(e.drop(columns="player"), on="player_id")
    return j, len(unmatched)


def compare(joined: pd.DataFrame) -> dict:
    """Average Understat minus ESPN shots and shots on target, with exact-match rates."""
    if joined.empty:
        return {"player_matches": 0}
    j = joined.dropna(subset=["espn_shots"])
    ds = j["shots"] - j["espn_shots"]
    out = {
        "player_matches": len(j),
        "matches": int(j["match_id"].nunique()) if "match_id" in j else None,
        "shots_understat": float(j["shots"].mean()),
        "shots_espn": float(j["espn_shots"].mean()),
        "shots_diff": float(ds.mean()),
        "shots_ratio": float(j["shots"].sum() / max(j["espn_shots"].sum(), 1e-9)),
        "shots_exact": float((ds == 0).mean()),
        "shots_understat_fewer": float((ds < 0).mean()),
        "shots_understat_more": float((ds > 0).mean()),
    }
    t = j.dropna(subset=["espn_sot"])
    if len(t):
        dt = t["sot"] - t["espn_sot"]
        out.update(
            {
                "sot_understat": float(t["sot"].mean()),
                "sot_espn": float(t["espn_sot"].mean()),
                "sot_diff": float(dt.mean()),
                "sot_exact": float((dt == 0).mean()),
            }
        )
    # The rate at which "1+ shots" would settle differently between the two sources.
    out["one_plus_disagree"] = float(((j["shots"] >= 1) != (j["espn_shots"] >= 1)).mean())
    return out


def run(apps: pd.DataFrame, days: list[pd.Timestamp], get: Callable = requests.get) -> dict:
    """Compare Understat with ESPN on the given match dates."""
    a = apps.copy()
    a["day"] = pd.to_datetime(a["kickoff"]).dt.tz_convert(None).dt.normalize()
    frames, unmatched, espn_rows = [], 0, 0
    for d in days:
        e = fetch_day(d, get=get)
        espn_rows += len(e)
        j, u = join_day(a[a["day"] == d], e)
        unmatched += u
        if not j.empty:
            frames.append(j)
    joined = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    out = {"days": len(days), "espn_rows": espn_rows, "unmatched": unmatched}
    out["espn_stats"] = ",".join(sorted(STAT_NAMES))
    return {**out, **compare(joined)}
