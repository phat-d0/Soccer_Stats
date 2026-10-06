"""Player shot lines for upcoming matches: model chances beside FanDuel prices.

For each upcoming fixture, the candidates are the players who appeared for either team
in its last CANDIDATE_MATCHES matches. Their features are built exactly as in the
backtest (a placeholder appearance is appended after their history, so only earlier
matches count), with the match model's expected goals and game state, and team-mates'
absences from FPL. Prices are conditional on playing (bets on non-players are void);
FPL's chance of playing is shown beside them.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from soccer_stats import trades as tr
from soccer_stats.factors import REGULAR_SHARE, build_features
from soccer_stats.models.player_counts import PlayerShotModel, prob_over
from soccer_stats.player_data import match_in_fixture, match_names, norm
from soccer_stats.player_odds import parse_event

CANDIDATE_MATCHES = 5
TRAIN_DAYS = 730
SHOW_PER_TEAM = 6  # players shown per team when there are no prices
MARKET_COUNT = {"player_shots": "shots", "player_shots_on_target": "sot"}


def _candidates(apps: pd.DataFrame, team: str) -> pd.DataFrame:
    recent = apps[apps["team"] == team].drop_duplicates("match_id").tail(CANDIDATE_MATCHES)
    sub = apps[(apps["team"] == team) & apps["match_id"].isin(recent["match_id"])]
    out = (
        sub.groupby("player_id")
        .agg(
            player=("player", "last"),
            position=("position", "last"),
            apps=("match_id", "nunique"),
            shots=("shots", "sum"),
        )
        .reset_index()
    )
    out["share"] = out["shots"] / max(sub["shots"].sum(), 1)
    out["recent_rate"] = out["apps"] / max(len(recent), 1)
    return out


def _fpl_chances(fpl: pd.DataFrame | None, team: str, cands: pd.DataFrame) -> dict[str, float]:
    """Understat player id -> FPL chance of playing (unmatched names are left out)."""
    if fpl is None or fpl.empty:
        return {}
    squad = fpl[fpl["team"] == team]
    got, _ = match_names(
        squad["name"], team, dict(zip(cands["player_id"], cands["player"], strict=True))
    )
    by_name = dict(zip(squad["name"], squad["p_play"], strict=True))
    return {pid: float(by_name[n]) for n, pid in got.items()}


def player_cards(
    apps: pd.DataFrame,
    fixtures: list[dict],
    fpl: pd.DataFrame | None = None,
    odds_events: list[dict] | None = None,
    factors: list[str] | None = None,
    sot_method: str = "thin",
    now: pd.Timestamp | None = None,
) -> tuple[dict[tuple[str, str], list[dict]], dict]:
    """(home, away) -> player rows for the app, plus a status dict."""
    now = now or pd.Timestamp.now(tz="UTC")
    status = {"players": 0, "priced": 0, "unmatched_odds": 0}
    if apps.empty or not fixtures:
        return {}, status
    upcoming = [c for c in fixtures if pd.Timestamp(c["kickoff"]) > now - pd.Timedelta(hours=2)]
    rows, info, seen = [], [], set()
    cands_by_team, chances = {}, {}
    for c in upcoming:
        for team, opp, home in ((c["home"], c["away"], True), (c["away"], c["home"], False)):
            if team in seen:  # only each team's next match
                continue
            seen.add(team)
            cands = _candidates(apps, team)
            if cands.empty:
                continue
            cands_by_team[(c["home"], c["away"], team)] = cands
            ch = _fpl_chances(fpl, team, cands)
            chances.update(ch)
            mid = f"next|{c['home']}|{c['away']}"
            absent = sum(
                s * (1 - ch.get(pid, 1.0))
                for pid, s in zip(cands["player_id"], cands["share"], strict=True)
                if s >= REGULAR_SHARE
            )
            p = c.get("p", {})
            info.append(
                {
                    "match_id": mid,
                    "team": team,
                    "team_xg": c["xg"][0 if home else 1] if c.get("xg") else np.nan,
                    "game_state": (p.get("home", 0) - p.get("away", 0)) * (1 if home else -1),
                    "absent": absent,
                }
            )
            for r in cands.itertuples(index=False):
                rows.append(
                    {
                        "match_id": mid,
                        "season": tr.season_label(pd.Timestamp(c["kickoff"])),
                        "kickoff": pd.Timestamp(c["kickoff"]),
                        "team": team,
                        "opponent": opp,
                        "home": home,
                        "player_id": r.player_id,
                        "player": r.player,
                        "position": r.position,
                        "minutes": 0,
                        "started": False,
                        "shots": 0,
                        "sot": 0,
                        "penalties": 0,
                        "team_shots": np.nan,
                        "opp_shots": np.nan,
                        "_placeholder": True,
                    }
                )
    if not rows:
        return {}, status
    hist = apps[apps["kickoff"] >= now - pd.Timedelta(days=TRAIN_DAYS)].assign(_placeholder=False)
    allr = pd.concat([hist, pd.DataFrame(rows)], ignore_index=True)
    mi = pd.DataFrame(info)
    feats = build_features(allr, mi[["match_id", "team", "team_xg", "game_state"]])
    train, live = feats[~feats["_placeholder"]], feats[feats["_placeholder"]].copy()
    live["absent_share"] = (
        live.set_index(["match_id", "team"])
        .index.map(mi.set_index(["match_id", "team"])["absent"])
        .to_numpy()
    )
    model = PlayerShotModel(factors, sot_method=sot_method).fit(train)
    d = model.distributions(live, lineup_known=False)
    live = live.reset_index(drop=True)

    odds = (
        pd.concat(
            [parse_event(e).assign(fetched_at=e.get("fetched_at")) for e in odds_events],
            ignore_index=True,
        )
        if odds_events
        else pd.DataFrame()
    )
    out: dict[tuple[str, str], list[dict]] = {}
    for i, r in live.iterrows():
        home, away = r["match_id"].split("|")[1:]
        pmfs = {"shots": d["shots"][i], "sot": d["sot"][i]}
        row = {
            "player": r["player"],
            "player_id": r["player_id"],
            "team": r["team"],
            "position": r["position"],
            "p_play": chances.get(r["player_id"]),
            "p_start": round(float(d["means"]["p_start"].iloc[i]), 3),
            "exp_shots": round(float((pmfs["shots"] * np.arange(len(pmfs["shots"]))).sum()), 2),
            "exp_sot": round(float((pmfs["sot"] * np.arange(len(pmfs["sot"]))).sum()), 2),
            "chances": {
                f"{k}_o{line}": round(float(prob_over(pmfs[k][None, :], line)[0]), 4)
                for k in ("shots", "sot")
                for line in (0.5, 1.5, 2.5)
            },
            "lines": [],
        }
        out.setdefault((home, away), []).append((row, pmfs))
    # Attach FanDuel lines by name, within the match's two teams.
    for (home, away), plist in out.items():
        if odds.empty:
            continue
        g = odds[(odds["home"] == home) & (odds["away"] == away)]
        if g.empty:
            continue
        rosters = {
            team: {row["player_id"]: row["player"] for row, _ in plist if row["team"] == team}
            for team in (home, away)
        }
        found, missed = match_in_fixture(g["player"].unique(), rosters)
        ids = {name: [pid] for name, (pid, _team) in found.items()}
        status["unmatched_odds"] += len(missed)
        by_pid = {row["player_id"]: (row, pmfs) for row, pmfs in plist}
        for (name, market, line), sides in g.groupby(["player", "market", "line"]):
            hit = ids.get(name, [])
            if len(hit) != 1:
                continue
            row, pmfs = by_pid[hit[0]]
            p_over = float(prob_over(pmfs[MARKET_COUNT[market]][None, :], line)[0])
            prices = dict(zip(sides["side"], sides["odds"], strict=False))
            fetched = sides["fetched_at"].iloc[0]
            pair = tr.devig_pair(prices.get("over"), prices.get("under"))
            for side, odds_ in prices.items():
                p = p_over if side == "over" else 1 - p_over
                row["lines"].append(
                    {
                        "market": market,
                        "line": line,
                        "side": side,
                        "odds": odds_,
                        "p": round(p, 4),
                        "edge": round(p * odds_ - 1, 4),
                        "implied": round(pair[0] if side == "over" else pair[1], 4)
                        if pair
                        else round(1 / odds_, 4),  # over-only books: price incl. margin
                        "fetched_at": fetched,
                    }
                )
    final: dict[tuple[str, str], list[dict]] = {}
    for key, plist in out.items():
        rows_ = [row for row, _ in plist]
        priced = [r for r in rows_ if r["lines"]]
        status["players"] += len(rows_)
        status["priced"] += len(priced)
        if priced:
            final[key] = sorted(priced, key=lambda r: -r["exp_shots"])
        else:
            top = []
            for team in key:
                top += sorted(
                    [r for r in rows_ if r["team"] == team], key=lambda r: -r["exp_shots"]
                )[:SHOW_PER_TEAM]
            final[key] = top
    return final, status


def fpl_players(players: pd.DataFrame) -> pd.DataFrame:
    """players.parse_players output -> name, team, p_play for name matching."""
    return players.assign(name=players["name"].map(str))[["name", "team", "p_play"]].assign(
        key=lambda d: d["name"].map(norm)
    )
