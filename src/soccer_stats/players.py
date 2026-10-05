"""Player availability from the Fantasy Premier League (FPL) API, turned into team adjustments.

FPL's public bootstrap feed lists every Premier League player with injury/suspension
status, a "chance of playing next round", news text, minutes, and season expected goals
(xG) and expected assists (xA). From that we estimate how much of a team's usual
attacking output (and defensive solidity) is missing for its next match:

* Attack: each player's xG+xA per 90 (shrunk toward his position's league average, so a
  few lucky minutes don't make a star) weighted by his usual share of the team's minutes.
  An absent player is replaced by a below-average player of the same position. The
  ratio of "expected output with this squad" to "usual output" scales the team's
  expected goals.
* Defence: missing regular goalkeepers and defenders raise expected goals conceded by
  fixed, modest amounts (FPL has no clean per-player defensive measure).

Using each player's share of minutes *this season* as the baseline means long-term
absentees are already reflected in the team's ratings and barely move the adjustment;
fresh absences of regulars move it the most.

These adjustments can't be backtested yet: FPL doesn't publish past injury news. The
publish job logs each snapshot so a history builds up from now on.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pandas as pd

from soccer_stats.data import RAW_DIR, _fetch

FPL_URL = "https://fantasy.premierleague.com/api/bootstrap-static/"
FPL_HEADERS = {"User-Agent": "Mozilla/5.0 (soccer-stats)"}

# FPL team name -> football-data team name, where they differ.
TEAM_NAMES = {
    "Man Utd": "Man United",
    "Spurs": "Tottenham",
    "Sheffield Utd": "Sheffield United",
    "Coventry City": "Coventry",
    "Hull City": "Hull",
    "Ipswich Town": "Ipswich",
    "Nott'm Forest": "Nott'm Forest",
}
POSITIONS = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}

PRIOR_MINUTES = 600  # shrink per-90 rates toward the position average by this many minutes
REPLACEMENT = 0.6  # a stand-in produces this fraction of the position-average rate
GK_PENALTY = 0.08  # +8% goals conceded without the first-choice keeper
DEF_PENALTY = 0.03  # +3% per missing regular defender
MAX_DEF_PENALTY = 0.15
MIN_ATTACK_MULT = 0.7
REGULAR_SHARE = 0.15  # list absentees who usually play at least this share of minutes


@dataclass
class Absence:
    name: str
    position: str
    status: str  # "out", "doubtful"
    chance: int  # chance of playing, 0-100
    news: str
    minutes_share: float  # share of the team's minutes this season
    attack_impact: float  # share of the team's attacking output lost


@dataclass
class TeamNews:
    team: str
    attack_mult: float = 1.0
    defence_mult: float = 1.0
    absences: list[Absence] = field(default_factory=list)
    key_players: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def fetch_fpl(raw_dir: Path = RAW_DIR, max_age_hours: float = 0.25) -> dict:
    path = _fetch(FPL_URL, raw_dir / "fpl_bootstrap.json", max_age_hours, headers=FPL_HEADERS)
    return json.loads(path.read_text())


def chance_of_playing(status: str, chance: float | None) -> float:
    """FPL status a/d/i/s/u/n plus optional percentage -> probability of playing."""
    if chance is not None and not pd.isna(chance):
        return float(chance) / 100
    return {"a": 1.0, "d": 0.5}.get(status, 0.0)


def parse_players(data: dict) -> pd.DataFrame:
    teams = {t["id"]: TEAM_NAMES.get(t["name"], t["name"]) for t in data["teams"]}
    rows = []
    for e in data["elements"]:
        rows.append(
            {
                "name": e.get("web_name") or e.get("second_name", ""),
                "team": teams.get(e["team"], str(e["team"])),
                "position": POSITIONS.get(e["element_type"], "MID"),
                "status": e.get("status", "a"),
                "chance": e.get("chance_of_playing_next_round"),
                "news": e.get("news") or "",
                "minutes": float(e.get("minutes") or 0),
                "xg": float(e.get("expected_goals") or 0),
                "xa": float(e.get("expected_assists") or 0),
            }
        )
    df = pd.DataFrame(rows)
    df["p_play"] = [
        chance_of_playing(s, c) for s, c in zip(df["status"], df["chance"], strict=True)
    ]
    return df


def team_news(players: pd.DataFrame, games_by_team: dict[str, int]) -> dict[str, TeamNews]:
    """Attack/defence multipliers and notable absences for every team."""
    played = players[players["minutes"] > 0]
    pos_rate = (played.groupby("position")[["xg", "xa"]].sum().sum(axis=1)) / (
        played.groupby("position")["minutes"].sum() / 90
    )
    out = {}
    for team, grp in players.groupby("team"):
        games = games_by_team.get(team) or max(1.0, grp["minutes"].max() / 90)
        g = grp.copy()
        g["share"] = (g["minutes"] / (games * 90)).clip(0, 1)
        avg = g["position"].map(pos_rate).fillna(pos_rate.mean())
        g["rate"] = (g["xg"] + g["xa"] + avg * PRIOR_MINUTES / 90) / (
            (g["minutes"] + PRIOR_MINUTES) / 90
        )
        g["rep"] = (avg * REPLACEMENT).clip(upper=g["rate"])

        usual = (g["share"] * g["rate"]).sum()
        expected = (g["share"] * (g["p_play"] * g["rate"] + (1 - g["p_play"]) * g["rep"])).sum()
        attack = max(MIN_ATTACK_MULT, min(1.0, expected / usual)) if usual > 0 else 1.0

        missing = g["share"] * (1 - g["p_play"])
        penalty = (missing[g["position"] == "GK"] * GK_PENALTY).sum() + (
            missing[g["position"] == "DEF"] * DEF_PENALTY
        ).sum()
        defence = 1 + min(MAX_DEF_PENALTY, penalty)

        g["impact"] = missing * (g["rate"] - g["rep"]) / usual if usual > 0 else 0.0
        absent = g[(g["p_play"] < 1) & (g["share"] >= REGULAR_SHARE)].sort_values(
            ["impact", "share"], ascending=False
        )
        absences = [
            Absence(
                name=r.name,
                position=r.position,
                status="out" if r.p_play == 0 else "doubtful",
                chance=int(round(r.p_play * 100)),
                news=r.news,
                minutes_share=round(r.share, 3),
                attack_impact=round(float(r.impact), 4),
            )
            for r in absent.itertuples()
        ]
        key = g.assign(value=g["share"] * g["rate"]).nlargest(6, "value")
        key_players = [
            {
                "name": r.name,
                "position": r.position,
                "xg_xa_per90": round(float(r.rate), 2),
                "minutes_share": round(float(r.share), 2),
                "chance": int(round(r.p_play * 100)),
            }
            for r in key.itertuples()
        ]
        out[team] = TeamNews(team, round(attack, 4), round(defence, 4), absences, key_players)
    return out


def align_team_names(news: dict[str, TeamNews], teams: list[str]) -> dict[str, TeamNews]:
    """Rename FPL teams that still don't match football-data names (e.g. 'Hull City' -> 'Hull').

    Only unmatched names are touched, and only when exactly one team is a prefix match.
    """
    out = {}
    for name, n in news.items():
        if name not in teams:
            candidates = [t for t in teams if name.startswith(t + " ") or t.startswith(name + " ")]
            if len(candidates) == 1:
                name = candidates[0]
                n.team = name
        out[name] = n
    return out


def fixture_multipliers(
    fixtures: pd.DataFrame, news: dict[str, TeamNews], now: pd.Timestamp, days: int = 21
) -> dict[tuple[str, str], tuple[float, float]]:
    """(home, away) -> expected-goals multipliers, for each team's next fixture only.

    FPL's chance of playing refers to the next round, so later fixtures are left alone.
    """
    out = {}
    seen: set[str] = set()
    for fx in fixtures.sort_values("kickoff").itertuples(index=False):
        if pd.Timestamp(fx.kickoff) > now + pd.Timedelta(days=days):
            break
        h = news.get(fx.home) if fx.home not in seen else None
        a = news.get(fx.away) if fx.away not in seen else None
        seen.update([fx.home, fx.away])
        if not h and not a:
            continue
        h_att, h_def = (h.attack_mult, h.defence_mult) if h else (1.0, 1.0)
        a_att, a_def = (a.attack_mult, a.defence_mult) if a else (1.0, 1.0)
        out[(fx.home, fx.away)] = (h_att * a_def, a_att * h_def)
    return out


def news_snapshot(players: pd.DataFrame, at: str) -> list[dict]:
    """Compact record of everyone not fully available, for building a history."""
    flagged = players[players["p_play"] < 1]
    return [
        {
            "at": at,
            "team": r.team,
            "name": r.name,
            "status": r.status,
            "chance": int(round(r.p_play * 100)),
            "news": r.news,
        }
        for r in flagged.itertuples()
    ]


def append_news_log(snapshot: list[dict], log_dir: Path, at: str | None = None) -> int:
    """Append players whose status, chance or news changed since the last logged state.

    Writes changes to `<log_dir>/<YYYY-MM>.jsonl` and keeps the current state in
    `latest.json`; a player dropping off the flagged list is logged as available.
    Returns how many changes were written.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    latest_path = log_dir / "latest.json"
    latest = json.loads(latest_path.read_text()) if latest_path.exists() else {}
    at = at or (
        snapshot[0]["at"] if snapshot else pd.Timestamp.now(tz="UTC").isoformat(timespec="minutes")
    )
    current = {f"{r['team']}|{r['name']}": r for r in snapshot}

    changes = []
    for key, r in current.items():
        old = latest.get(key)
        if not old or (old["status"], old["chance"], old["news"]) != (
            r["status"],
            r["chance"],
            r["news"],
        ):
            changes.append(r)
    for key, old in latest.items():
        if key not in current:
            changes.append({**old, "at": at, "status": "a", "chance": 100, "news": ""})

    if changes:  # nothing changed -> leave the files alone so there's nothing to commit
        with open(log_dir / f"{at[:7]}.jsonl", "a") as f:
            for r in changes:
                f.write(json.dumps(r) + "\n")
        latest_path.write_text(json.dumps(current, indent=0))
    return len(changes)
