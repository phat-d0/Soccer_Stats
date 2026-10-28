"""Write a local data set for the web app, so it can be rendered without the network.

    uv run python tests/web/make_fixture.py            # inputs from origin/data-log
    uv run python tests/web/make_fixture.py --source DIR

Writes tests/fixtures/web/{data.json, players_stats.json, players_backtest.json}.

What is real and what is made up:
- Matches, ratings, the replay record and upcoming fixtures come from a synthetic
  20-team league (tests/conftest.simulate_league), run through publish.build_data.
- Team news and the fixtures' player cards are synthetic. Player lines carry the raw
  model (`p_model`) and the blend (`p`) from the real E0_players.json coefficients.
- Fixtures get `p_bet` (publish.add_match_blend) from E0_dk.json's `blend.live`, or
  from FALLBACK_MATCH_BLEND (a fit like the real one: the model gets ~no weight) if the
  data-log file has none yet.
- The portfolio comes from the real paper ledger and backtests on the data-log branch,
  run through paper.run (which also opens new paper trades on the synthetic fixtures).
- players_backtest.json is a sample of the real E0_players_detail.json; players_stats.json
  is built from that sample with player_data.season_stats (goals and xG are synthetic).

`now` is fixed, so the output only changes when the code or the data-log inputs do.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))

from conftest import simulate_league  # noqa: E402

from soccer_stats import corners_live, paper  # noqa: E402
from soccer_stats import paper_dnb as pdnb  # noqa: E402
from soccer_stats import player_calibration as cal  # noqa: E402
from soccer_stats import publish as pub  # noqa: E402
from soccer_stats import trades as tr  # noqa: E402
from soccer_stats.player_data import season_stats  # noqa: E402
from soccer_stats.players import Absence, TeamNews  # noqa: E402
from soccer_stats.publish import (  # noqa: E402
    _clean,
    add_match_blend,
    add_team_totals,
    build_data,
    markets_research,
    match_blend,
    player_gate,
    portfolio_placeholder,
)

OUT = ROOT / "tests" / "fixtures" / "web"
NOW = pd.Timestamp("2026-10-06T12:00:00Z")
DATA_LOG_FILES = [
    "paper_trades/E0_2627.jsonl",
    "backtest/E0_dk.json",
    "backtest/E0_players.json",
    "backtest/E0_players_detail.json",
    "backtest/lean_backtest.json",
    "backtest/edge12_backtest.json",
    "backtest/lean_backtest_dk.json",
    "backtest/edge12_backtest_dk.json",
    "backtest/match_blends.json",
]
TEAMS = [
    "Arsenal", "Aston Villa", "Bournemouth", "Brentford", "Brighton", "Chelsea",
    "Crystal Palace", "Everton", "Fulham", "Ipswich", "Leeds", "Liverpool", "Man City",
    "Man United", "Newcastle", "Nottingham Forest", "Sunderland", "Tottenham", "West Ham",
    "Wolves",
]  # fmt: skip
# The live ledger's open trades are on the first four: keep them so prices line up.
FIXTURES = [
    ("2026-10-10T11:30:00Z", "Arsenal", "Leeds"),
    ("2026-10-10T14:00:00Z", "Ipswich", "Fulham"),
    ("2026-10-10T14:00:00Z", "Chelsea", "Bournemouth"),
    ("2026-10-11T13:00:00Z", "Newcastle", "Tottenham"),
    ("2026-10-11T15:30:00Z", "Man City", "Liverpool"),
    ("2026-10-18T13:00:00Z", "Brighton", "Crystal Palace"),
]
# Used only when data-log's E0_dk.json has no blend.live yet: [a_draw, a_away, b, c] and
# [a_under, b, c], close to the real fit (price weight ~1, model weight ~0).
FALLBACK_MATCH_BLEND = {
    "h2h": {"coef": [0.02, -0.01, 1.04, -0.03], "matches": 2276},
    "totals": {"coef": [0.0, 1.0, 0.05], "matches": 2276},
    "generated_at": "2026-10-06T00:00:00+00:00",
}
PLAYER_BT_TRADES = 25  # Player shots (retired) backtest trades kept, newest first
DK_BT_TRADES = 20  # Moneyline DraftKings backtest trades kept, newest first (pages of 15)
LEAN_BETS = 30  # Lean backtest rows kept (the app pages them by 20)
EDGE12_BETS = 30  # Model 12% backtest rows kept (newest first; every league, the cup too)
SAMPLE_PLAYERS = 35  # players kept from the real detail file (keeps fixtures < 1 MB)


def fetch_data_log(dest: Path) -> Path:
    """Copy the data-log inputs into `dest` with `git show origin/data-log:<path>`."""
    subprocess.run(["git", "fetch", "-q", "origin", "data-log"], cwd=ROOT, check=False)
    for f in DATA_LOG_FILES:
        out = dest / f
        out.parent.mkdir(parents=True, exist_ok=True)
        blob = subprocess.run(
            ["git", "show", f"origin/data-log:{f}"], cwd=ROOT, check=True, capture_output=True
        ).stdout
        out.write_bytes(blob)
    return dest


def league_matches() -> pd.DataFrame:
    """Synthetic results with real team names, ending just before NOW, with odds."""
    df, _ = simulate_league(n_teams=20, seasons=3, seed=7)
    df = df.copy()
    names = {f"T{i:02d}": t for i, t in enumerate(TEAMS)}
    df["home"], df["away"] = df["home"].map(names), df["away"].map(names)
    df["date"] = df["date"] + (
        NOW.tz_localize(None).normalize() - pd.Timedelta(days=2) - df["date"].max()
    )
    df["league"], df["season"] = "E0", "x"
    rng = np.random.default_rng(1)
    # Bookmaker prices: Poisson chances from a noisy view of the true rates, 5% margin.
    for pre in ("odds", "close"):
        lam = df["lam"] * rng.lognormal(0, 0.08, len(df))
        mu = df["mu"] * rng.lognormal(0, 0.08, len(df))
        ph, pd_, pa = _hda(lam.to_numpy(), mu.to_numpy())
        for k, p in (("home", ph), ("draw", pd_), ("away", pa)):
            df[f"{pre}_{k}"] = np.round(1 / (p * 1.05), 2)
    return df


def _hda(lam: np.ndarray, mu: np.ndarray, n: int = 10):
    k = np.arange(n + 1)
    fact = np.array([math.factorial(i) for i in k], dtype=float)
    ph = np.exp(-lam)[:, None] * lam[:, None] ** k / fact
    pa = np.exp(-mu)[:, None] * mu[:, None] ** k / fact
    m = ph[:, :, None] * pa[:, None, :]
    home = np.tril(np.ones((n + 1, n + 1)), -1)
    return (m * home).sum((1, 2)), np.trace(m, axis1=1, axis2=2), (m * home.T).sum((1, 2))


def fixtures_frame() -> pd.DataFrame:
    rows = []
    for i, (ko, h, a) in enumerate(FIXTURES):
        priced = i != 5  # the last fixture has no odds yet (an empty state)
        rows.append(
            {
                "kickoff": pd.Timestamp(ko),
                "home": h,
                "away": a,
                "odds_home": [1.38, 2.9, 1.7, 2.05, 2.3, None][i] if priced else None,
                "odds_draw": [4.9, 3.3, 4.0, 3.5, 3.6, None][i] if priced else None,
                "odds_away": [7.5, 2.5, 4.3, 3.4, 2.9, None][i] if priced else None,
                "odds_over25": [1.6, 1.95, 1.75, 1.7, 1.55, None][i] if priced else None,
                "odds_under25": [2.3, 1.85, 2.05, 2.15, 2.45, None][i] if priced else None,
            }
        )
    return pd.DataFrame(rows)


def team_news() -> dict[str, TeamNews]:
    def ab(name, pos, status, chance, news, share=0.6, impact=0.12):
        return Absence(name, pos, status, chance, news, share, impact)

    return {
        "Arsenal": TeamNews(
            "Arsenal",
            attack_mult=0.93,
            defence_mult=1.02,
            absences=[
                ab("Bukayo Saka", "MID", "out", 0, "Hamstring injury - Expected back 25 Oct"),
                ab("Gabriel", "DEF", "doubtful", 50, "Knock - 50% chance of playing", 0.8, 0.03),
                ab("Kai Havertz", "FWD", "doubtful", 75, "Illness - 75% chance of playing"),
            ],
        ),
        "Chelsea": TeamNews(
            "Chelsea",
            attack_mult=0.97,
            absences=[ab("Cole Palmer", "MID", "doubtful", 25, "Groin injury - 25% chance")],
        ),
        "Leeds": TeamNews("Leeds"),
    }


def _over(m: float, k: int) -> float:
    """Poisson chance of at least k with mean m."""
    return 1 - sum(math.exp(-m) * m**j / math.factorial(j) for j in range(k))


def player_cards(detail: dict, fixtures: list[dict], coef: list[float] | None) -> None:
    """Attach synthetic player cards (real names from the detail file) to each fixture.
    The first three fixtures get FanDuel over-only lines, the rest chances only."""
    by_team: dict[str, list[dict]] = {}
    for p in detail["players"]:
        if p["apps"] >= 10 and p["last"] >= "2026-03-01":
            by_team.setdefault(p["team"], []).append(p)
    rng = np.random.default_rng(3)
    for i, fx in enumerate(fixtures):
        cards = []
        for team in (fx["home"], fx["away"]):
            top = sorted(by_team.get(team, []), key=lambda p: -p["exp_shots"] / p["apps"])[:4]
            for p in top:
                lam = p["exp_shots"] / p["apps"] * rng.uniform(0.85, 1.15)
                lam_ot = p["exp_sot"] / p["apps"]
                card = {
                    "player": p["player"],
                    "player_id": p["player_id"],
                    "team": team,
                    "position": p["position"],
                    "p_play": 0.75 if p["player"] == "Kai Havertz" else None,
                    "p_start": 0.9,
                    "exp_shots": round(lam, 2),
                    "exp_sot": round(lam_ot, 2),
                    "chances": {
                        f"{k}_o{line}": round(_over(m, int(line + 0.5)), 4)
                        for k, m in (("shots", lam), ("sot", lam_ot))
                        for line in (0.5, 1.5, 2.5)
                    },
                    "lines": [],
                }
                if i < 3:
                    for market, m in (("player_shots", lam), ("player_shots_on_target", lam_ot)):
                        for line in (1.0, 2.0, 3.0):
                            pr = _over(m, int(line))
                            # FanDuel: over sides only, priced with a fat margin; a few
                            # are mispriced so the model flags an edge.
                            book = min(0.97, pr * rng.uniform(0.75, 1.45))
                            odds_ = round(max(1.05, 1 / book), 2)
                            # The blend, as player_live: over-only, so implied = 1 / odds.
                            pb = float(cal.apply(coef, [1 / odds_], [pr])[0]) if coef else None
                            card["lines"].append(
                                {
                                    "market": market,
                                    "line": line,
                                    "side": "over",
                                    "odds": odds_,
                                    "p": round(pb, 4) if pb is not None else None,
                                    "p_model": round(pr, 4),
                                    "edge": round(pb * odds_ - 1, 4) if pb is not None else None,
                                    "implied": round(1 / odds_, 4),
                                    "fetched_at": (NOW - pd.Timedelta(minutes=20)).isoformat(),
                                }
                            )
                cards.append(card)
        fx["players"] = sorted(cards, key=lambda c: -c["exp_shots"])


def sample_detail(detail: dict, fixtures: list[dict]) -> dict:
    keep = {p["player_id"] for fx in fixtures for p in fx.get("players", [])}
    for p in sorted(detail["players"], key=lambda p: -p["exp_shots"]):
        if len(keep) >= SAMPLE_PLAYERS:
            break
        keep.add(p["player_id"])
    # A few low-volume players too, so sorting by "shot less than expected" has rows.
    for p in sorted(detail["players"], key=lambda p: p["apps"])[:5]:
        keep.add(p["player_id"])
    return {
        **{k: v for k, v in detail.items() if k not in ("players", "apps")},
        "players": [p for p in detail["players"] if p["player_id"] in keep],
        "apps": {k: v for k, v in detail["apps"].items() if k in keep},
    }


def stats_from_detail(detail: dict) -> dict:
    """players_stats.json via player_data.season_stats on the sample's appearances."""
    ix = {f: i for i, f in enumerate(detail["fields"])}
    info = {p["player_id"]: p for p in detail["players"]}
    rng = np.random.default_rng(5)
    rows = []
    for pid, apps in detail["apps"].items():
        p = info[pid]
        for a in apps:
            d = pd.Timestamp(a[ix["date"]])
            start = d.year if d.month >= 7 else d.year - 1
            shots = a[ix["shots"]]
            rows.append(
                {
                    "season": f"{start % 100:02d}{(start + 1) % 100:02d}",
                    "team": p["team"],
                    "player_id": pid,
                    "player": p["player"],
                    "position": p["position"],
                    "match_id": f"{a[ix['date']]}|{a[ix['opponent']]}",
                    "kickoff": d,
                    "started": bool(a[ix["started"]]),
                    "minutes": a[ix["minutes"]],
                    "shots": shots,
                    "sot": a[ix["sot"]],
                    "goals": int(rng.binomial(a[ix["sot"]], 0.32)),
                    "xg": round(shots * 0.11 * rng.uniform(0.5, 1.5), 3),
                }
            )
    stats = season_stats(pd.DataFrame(rows))
    for r in stats:
        p = info[r["player_id"]]
        r["active"] = p["last"] >= "2026-01-01"
        r["current_team"] = p["team"] if r["active"] else None
    return _clean({"seasons": sorted({r["season"] for r in stats}, reverse=True), "players": stats})


def build(src: Path, out: Path) -> dict:
    detail = json.loads((src / "backtest" / "E0_players_detail.json").read_text())
    matches = league_matches()
    fixtures = fixtures_frame()
    news = team_news()
    data = build_data(matches, fixtures, xg_error=None, news=news, now=NOW)
    data["generated_at"] = NOW.isoformat()
    data["odds_source"] = {
        "name": "DraftKings",
        "fetched_at": (NOW - pd.Timedelta(minutes=20)).isoformat(),
        "refresh_hours": 1.0,
        "credits_left": 22962,
        "last_cost": 2,
        "error": None,
    }
    add_match_blend(data, match_blend(str(src / "backtest" / "E0_dk.json")) or FALLBACK_MATCH_BLEND)
    data["portfolio"] = portfolio_placeholder()
    gate = player_gate(str(src / "backtest" / "E0_players.json"))
    player_cards(detail, data["fixtures"], gate["calibration"])
    priced = sum(1 for fx in data["fixtures"] for p in fx["players"] if p["lines"])
    data["players_status"] = {
        "gate": {"passed": True, "shots": None, "sot": None, "factors": None},
        "error": None,
        "odds": None,
        "bookmaker": "FanDuel",
        "players": sum(len(fx["players"]) for fx in data["fixtures"]),
        "priced": priced,
        "active_players": 520,
        "blend": gate["calibration"] is not None,
        "blend_note": None,
        "paper_trades": tr.PLAYER_PAPER_TRADES,
    }
    # Portfolio through the real ledger code, on a scratch copy of the data-log files.
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "log"
        shutil.copytree(src, log)
        # Player shots is retired and its backtest view pages trades by 15: keep the newest
        # PLAYER_BT_TRADES of E0_players.json's trades (keeps the fixtures under 1 MB).
        pj = log / "backtest" / "E0_players.json"
        if pj.exists():
            pd_ = json.loads(pj.read_text())
            pd_["trades"] = (pd_.get("trades") or [])[-PLAYER_BT_TRADES:]
            pj.write_text(json.dumps(pd_))
        # The same for the DraftKings backtest's trade list (newest first in the file); its
        # summary, sweep and breakdowns stay whole.
        dj = log / "backtest" / "E0_dk.json"
        if dj.exists():
            dk_ = json.loads(dj.read_text())
            dk_["trades"] = (dk_.get("trades") or [])[:DK_BT_TRADES]
            dj.write_text(json.dumps(dk_))
        results = pd.DataFrame(columns=["home", "away", "season", "date", "home_goals"])
        paper.run(data, log, results, league="E0", now=NOW)
    add_settled_live_matches(data)
    add_edge_thresholds(data)
    add_second_league(data)
    add_team_total_prices(data)
    add_espn_team_news(data)
    add_markets_research(data)
    add_research_studies(data)
    add_corners(data)
    add_lean_strategy(data)
    add_champions_league(data)
    # The real Lean backtest (data-log's lean_backtest.json via paper.run), with its bet
    # list cut to the newest LEAN_BETS rows to stay under 1 MB (aggregates untouched).
    # The same for Model 12%'s per-league replay (edge12_backtest.json), keeping a few of
    # each league's newest bets so every competition's list has rows.
    sb = data["portfolio"].get("strategy_backtests") or {}
    lb = sb.get("lean") or {}
    if lb.get("bets"):
        lb["bets"] = lb["bets"][:LEAN_BETS]
        lb["bets_shown"] = len(lb["bets"])
    eb = sb.get("edge12") or {}
    if eb.get("bets"):
        li = eb["bet_fields"].index("league")
        newest = eb["bets"][:EDGE12_BETS]
        seen = {r[li] for r in newest}
        extra = [r for r in eb["bets"][EDGE12_BETS:] if r[li] not in seen and not seen.add(r[li])]
        eb["bets"] = newest + extra
        eb["bets_shown"] = len(eb["bets"])
    # Each league's bankroll curves (flat and Kelly) at every third point, the last kept,
    # for room under 1.5 MB; the app draws whatever points come.
    # (All's curves too since the real DraftKings files joined, 28 Oct.)
    for f in (sb.get("lean") or {}, sb.get("edge12") or {}):
        for part in [f.get("all"), *(f.get("leagues") or {}).values()]:
            for block in ((part or {}).get("sizing") or {}).values():
                c = block.get("curve") if isinstance(block, dict) else None
                if isinstance(c, list) and len(c) > 20:
                    block["curve"] = c[::3] + ([c[-1]] if (len(c) - 1) % 3 else [])
    add_draftkings_backtests(sb)
    # The app never reads each player strategy's compact trade rows (Record → Player shots
    # uses the sweeps and calibration); leave them out to keep the fixture under 1 MB.
    for st in (
        ((data["portfolio"].get("player_model") or {}).get("priced") or {})
        .get("strategies", {})
        .values()
    ):
        st.pop("trades", None)

    sample = sample_detail(detail, data["fixtures"])
    stats = stats_from_detail(sample)
    data["players_backtest"] = "players_backtest.json"
    data["players_stats"] = "players_stats.json"

    out.mkdir(parents=True, exist_ok=True)

    def dump(obj) -> str:
        return json.dumps(_clean(obj), separators=(",", ":"), allow_nan=False)

    (out / "data.json").write_text(dump(data))
    (out / "players_backtest.json").write_text(dump(sample))
    (out / "players_stats.json").write_text(dump(stats))
    sp1_stats = add_teams_by_league(data)
    add_models(data, src)  # after every block, SP1's and E1's fits included
    (out / "players_stats_SP1.json").write_text(dump(sp1_stats))
    (out / "data.json").write_text(dump(data))  # again, with teams_by_league
    return data


# The Teams tab per league (round 9): La Liga with xG and Understat season stats, the
# Championship goals-only with none. Simulated results with real names, fitted by the
# same publish.team_block that builds data.json; the stats go through the real
# publish.league_season_stats on a synthetic Understat file.
SP1_CLUBS = [
    "Real Madrid", "Barcelona", "Ath Madrid", "Sevilla", "Valencia", "Betis",
    "Sociedad", "Villarreal", "Ath Bilbao", "Girona", "Celta", "Osasuna",
]  # fmt: skip
E1_CLUBS = ["Leeds", "Sheffield United", "Sunderland", "Norwich", "Hull", "Coventry",
            "Middlesbrough", "Watford", "Stoke", "Bristol City"]  # fmt: skip
SP1_PLAYERS = ["Lamine Yamal", "Vinicius Junior", "Kylian Mbappe", "Robert Lewandowski",
               "Antoine Griezmann", "Isco", "Mikel Oyarzabal", "Gerard Moreno",
               "Nico Williams", "Iago Aspas", "Ante Budimir", "Raphinha"]  # fmt: skip


# Each synthetic league's match-model fit numbers (publish.safe_fit_info), for the
# Portfolio model view (data.match_model).
FIT_INFO: dict[str, dict] = {}


def _fit_block(clubs: list[str], code: str, seed: int, with_xg: bool) -> dict:
    from soccer_stats import dashboard
    from soccer_stats.publish import safe_fit_info, team_block

    df, _ = simulate_league(n_teams=len(clubs), seasons=2, seed=seed)
    names = {f"T{i:02d}": t for i, t in enumerate(clubs)}
    df["home"], df["away"] = df["home"].map(names), df["away"].map(names)
    df["date"] = df["date"] + (NOW.tz_localize(None) - pd.Timedelta(days=2) - df["date"].max())
    rng = np.random.default_rng(seed)
    df["home_xg"] = df["home_goals"] * 0.6 + rng.gamma(2, 0.3, len(df)) if with_xg else np.nan
    df["away_xg"] = df["away_goals"] * 0.6 + rng.gamma(2, 0.3, len(df)) if with_xg else np.nan
    model = dashboard.fit_model(df, xg_weight=0.7 if with_xg else 0.0)
    FIT_INFO[code] = safe_fit_info(model, df)
    return team_block(model, df, pd.DataFrame(), code)


def add_teams_by_league(data: dict) -> dict:
    from soccer_stats.publish import league_season_stats
    from soccer_stats.xg import LEAGUES as US

    data["teams_by_league"] = {
        "E0": {
            "name": data["league"],
            "teams": data["teams"],
            "params": data["params"],
            "ratings": data["ratings"],
            "xg": bool(data["xg_weight"]),
        },
        "SP1": _fit_block(SP1_CLUBS, "SP1", 11, True),
        "E1": _fit_block(E1_CLUBS, "E1", 12, False),
    }
    rng = np.random.default_rng(5)
    players = []
    for i, name in enumerate(SP1_PLAYERS * 3):  # 36 player-seasons over 12 clubs
        games = int(rng.integers(8, 34))
        shots = int(rng.integers(5, 110))
        players.append(
            {
                "id": str(9000 + i),
                "player_name": name if i < len(SP1_PLAYERS) else f"{name} {i // len(SP1_PLAYERS)}",
                "games": str(games),
                "time": str(games * int(rng.integers(40, 90))),
                "shots": str(shots),
                "goals": str(int(shots * rng.uniform(0.05, 0.2))),
                "xG": f"{shots * rng.uniform(0.06, 0.16):.2f}",
                "position": ["F S", "M S", "D", "GK", "F M S", "M"][i % 6],
                "team_title": SP1_CLUBS[i % len(SP1_CLUBS)],
            }
        )
    with tempfile.TemporaryDirectory() as tmp:
        for year in (2025, 2026):
            (Path(tmp) / f"understat_{US['SP1']}_{year}.json").write_text(
                json.dumps({"dates": [], "players": players[: 24 if year == 2026 else 36]})
            )
        rows = league_season_stats("SP1", [2025, 2026], raw_dir=Path(tmp))
    data["players_stats_by_league"] = {"SP1": "players_stats_SP1.json"}
    return {"seasons": sorted({r["season"] for r in rows}, reverse=True), "players": rows}


# Settled live match trades with closing prices, until the real ledger has some: the
# moneyline contract (close_odds, close_fetched_at, close_minutes_before, clv_dk,
# beat_close_dk per trade; summary clv_dk, beat_close_dk, close_early). The last one is
# an older trade without close fields, so the app's "–" fallbacks are exercised.
SYNTHETIC_SETTLED = [
    # home, away, kickoff, market, odds, close odds, minutes before kickoff, score, won
    ("Arsenal", "Everton", "2026-09-27T14:00:00Z", "draw", 4.6, 4.2, 4, "1-1", True),
    ("Fulham", "Wolves", "2026-09-27T14:00:00Z", "away", 3.9, 4.1, 6, "2-0", False),
    ("Burnley", "Leeds", "2026-09-20T11:30:00Z", "home", 3.1, 2.9, 3, "0-1", False),
    ("Spurs", "Brentford", "2026-09-20T14:00:00Z", "away", 5.2, 4.8, 95, "1-2", True),
    ("Newcastle", "Chelsea", "2026-09-13T16:30:00Z", "home", 2.7, 2.75, 5, "0-0", False),
    ("Everton", "Brighton", "2026-08-30T14:00:00Z", "away", 3.4, None, None, "1-0", False),
]


def add_settled_live_matches(data: dict) -> None:
    ml = next((p for p in data["portfolio"].get("portfolios") or [] if p["id"] == "moneyline"), {})
    live = ml.get("live") or {"trades": []}
    trades = list(live.get("trades") or [])
    for home, away, ko, market, odds, close, mins, score, won in SYNTHETIC_SETTLED:
        kick = pd.Timestamp(ko)
        fetched = (kick - pd.Timedelta(minutes=mins)).isoformat() if mins is not None else None
        clv = odds / close - 1 if close else None
        trades.append(
            {
                "id": f"E0|2627|{home}|{away}",
                "source": "live",
                "bet_type": "match",
                "league": "E0",
                "season": "2627",
                "opened_at": (kick - pd.Timedelta(hours=40)).isoformat(),
                "kickoff": kick.isoformat(),
                "hours_to_kickoff": 40.0,
                "home": home,
                "away": away,
                "market": market,
                "odds": odds,
                "odds_fetched_at": (kick - pd.Timedelta(hours=40)).isoformat(),
                "model_p": round(1.15 / odds, 4),
                "model_p_base": round(1.15 / odds, 4),
                "edge": 0.15,
                "threshold": 0.12,
                "stake": 10.0,
                "news_applied": False,
                "model_ref": {"commit": "synthetic", "xg_weight": 0.7, "matches_fit": 800},
                "close_odds": close,
                "close_fetched_at": fetched,
                "close_minutes_before": mins,
                "clv_dk": round(clv, 4) if clv is not None else None,
                "beat_close_dk": (clv > 0) if clv is not None else None,
                "clv_pinnacle": None,
                "status": "won" if won else "lost",
                "score": score,
                "profit": round(10 * (odds - 1), 2) if won else -10.0,
                "settled_at": (kick + pd.Timedelta(hours=3)).isoformat(),
            }
        )
    # Rebuild the per-strategy portfolios with these trades, keeping their backtests.
    backtests = {p["id"]: p["backtest"] for p in data["portfolio"].get("portfolios") or []}
    r = data["portfolio"].get("rule") or {}
    match_rule = {
        "threshold": r.get("threshold"),
        "source": r.get("threshold_source"),
        "note": r.get("threshold_note"),
    }
    data["portfolio"]["portfolios"] = paper.portfolios_section(
        live, trades, backtests, match_rule, now=NOW
    )


# The research lab's edge_threshold contract, until data-log's backtests carry it: one
# portfolio with a recommended level and one with none (min_edge null), so the app's
# two states are both in the fixture. Synthetic numbers, marked in `method`.
_EDGE_POOLS = {
    "winning": {
        "min_edge": 0.08,
        "confidence": 0.95,
        "method": "synthetic fixture values (walk-forward edge buckets)",
        "n_bets": 1419,
        "seasons": {"development": ["2223", "2324", "2425"], "check": ["2526"]},
        "note": "Synthetic numbers for the app's tests.",
        "by_bucket": [
            {
                "edge_lo": 0.02,
                "edge_hi": 0.05,
                "n": 420,
                "implied": 0.36,
                "model": 0.38,
                "realized": 0.35,
                "realized_lo": 0.31,
                "realized_hi": 0.40,
                "roi": -0.028,
                "roi_lo": -0.148,
                "roi_hi": 0.092,
            },
            {
                "edge_lo": 0.05,
                "edge_hi": 0.08,
                "n": 360,
                "implied": 0.33,
                "model": 0.36,
                "realized": 0.32,
                "realized_lo": 0.27,
                "realized_hi": 0.37,
                "roi": -0.03,
                "roi_lo": -0.15,
                "roi_hi": 0.09,
            },
            {
                "edge_lo": 0.08,
                "edge_hi": 0.12,
                "n": 290,
                "implied": 0.30,
                "model": 0.34,
                "realized": 0.31,
                "realized_lo": 0.26,
                "realized_hi": 0.36,
                "roi": 0.033,
                "roi_lo": -0.087,
                "roi_hi": 0.153,
            },
            {
                "edge_lo": 0.12,
                "edge_hi": None,
                "n": 349,
                "implied": 0.24,
                "model": 0.31,
                "realized": 0.25,
                "realized_lo": 0.21,
                "realized_hi": 0.30,
                "roi": 0.042,
                "roi_lo": -0.078,
                "roi_hi": 0.162,
            },
        ],
    },
    "losing": {
        "min_edge": None,
        "confidence": 0.95,
        "method": "synthetic fixture values (walk-forward edge buckets)",
        "n_bets": 1012,
        "seasons": {"development": ["2526 first half"], "check": ["2526 second half"]},
        "note": "No minimum edge works. At every claimed edge from 0% to 30%, past bets lost "
        "money after FanDuel's margin.",
        "by_bucket": [
            {
                "edge_lo": 0.02,
                "edge_hi": 0.08,
                "n": 410,
                "implied": 0.33,
                "model": 0.25,
                "realized": 0.24,
                "realized_lo": 0.20,
                "realized_hi": 0.28,
                "roi": -0.273,
                "roi_lo": -0.393,
                "roi_hi": -0.153,
            },
            {
                "edge_lo": 0.08,
                "edge_hi": 0.15,
                "n": 380,
                "implied": 0.30,
                "model": 0.24,
                "realized": 0.22,
                "realized_lo": 0.18,
                "realized_hi": 0.27,
                "roi": -0.267,
                "roi_lo": -0.387,
                "roi_hi": -0.147,
            },
            {
                "edge_lo": 0.15,
                "edge_hi": None,
                "n": 222,
                "implied": 0.21,
                "model": 0.19,
                "realized": 0.16,
                "realized_lo": 0.11,
                "realized_hi": 0.21,
                "roi": -0.238,
                "roi_lo": -0.358,
                "roi_hi": -0.118,
            },
        ],
    },
}


# Per portfolio, the backtest keys to fill. Moneyline is as the real data came back on
# 7 Oct: no level and no buckets in its own (blend) pool, so the chart falls back to the
# Pinnacle pool. Player shots carries a synthetic recommended level, so the app's
# "only flag bets with at least ..." state is tested too.
SYNTHETIC_EDGE = {
    "moneyline": {
        "edge_threshold": {
            "min_edge": None,
            "confidence": 0.95,
            "method": "synthetic fixture values (walk-forward edge buckets)",
            "n_bets": 20,
            "seasons": {},
            "note": "Too few settled match bets with a positive edge (20) to learn a minimum edge.",
            "by_bucket": [],
        },
        "edge_threshold_pinnacle": {**_EDGE_POOLS["losing"], "n_bets": 1280},
    },
    "player_shots": {"edge_threshold": _EDGE_POOLS["winning"]},
}


def add_edge_thresholds(data: dict) -> None:
    for p in data["portfolio"].get("portfolios") or []:
        bt = p.get("backtest")
        if bt is None:
            continue
        for key, value in SYNTHETIC_EDGE.get(p["id"], {}).items():
            if bt.get(key) is None:  # the real field wins once data-log carries it
                bt[key] = value


# A second competition (La Liga, SP1), until the multi-league pipeline's data reaches the
# fixture: two upcoming SP1 matches without odds yet (more than 48 hours out), a few
# settled SP1 Moneyline trades, live and backtest, and a per-league minimum edge, so the
# app's competition filter, per-league summaries and per-league edge panel are all tested.
SP1_TEAMS = [("Real Madrid", "Sevilla"), ("Barcelona", "Valencia")]
SP1_TRADES = [
    # live?, home, away, kickoff, market, odds, won
    (True, "Real Madrid", "Getafe", "2026-09-28T19:00:00Z", "draw", 5.0, False),
    (True, "Villarreal", "Betis", "2026-09-21T14:15:00Z", "away", 3.2, True),
    (False, "Barcelona", "Girona", "2026-03-15T20:00:00Z", "home", 1.6, True),
    (False, "Atletico Madrid", "Osasuna", "2026-02-08T15:15:00Z", "away", 7.0, False),
    (False, "Sevilla", "Celta Vigo", "2026-01-18T17:30:00Z", "draw", 3.6, False),
]
SP1_EDGE = {
    "min_edge": 0.06,
    "confidence": 0.95,
    "method": "synthetic fixture values (walk-forward edge buckets)",
    "n_bets": 512,
    "seasons": {"development": ["2324", "2425"], "check": ["2526"]},
    "note": "Synthetic numbers for the app's tests.",
    "by_bucket": [],
}


# FanDuel team totals for the first match, through publish.add_team_totals, until the real
# log's matches reach the fixture (its synthetic clubs never match data-log's rows): the
# home side at 1.5, the away side at 0.5 and 1.5, a look and a close (the close wins).
def add_team_total_prices(data: dict) -> None:
    fx = data["fixtures"][0]
    ko = pd.Timestamp(fx["kickoff"])
    rows = []
    for snap, before, shift in (("look", 24 * 60, 0.03), ("close", 20, 0.01)):
        at = (ko - pd.Timedelta(minutes=before)).isoformat()
        for side, line in (("home", 1.5), ("away", 0.5), ("away", 1.5)):
            fair = round(1 - fx["goals_cdf"][side][int(line)] - shift, 4)
            over, under = round(1 / (fair * 1.03), 2), round(1 / ((1 - fair) * 1.03), 2)
            rows.append(
                {"league": fx.get("league", "E0"), "home": fx["home"], "away": fx["away"]}
                | {"kickoff": ko.isoformat(), "book": "fanduel", "side": side, "line": line}
                | {"snapshot": snap, "over": over, "under": under, "fair_over": fair}
                | {"fetched_at": at, "downloaded_at": at}
            )
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "E0_team_totals_2026-10.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n"
        )
        add_team_totals(data["fixtures"][:1], [tmp])


# ESPN's card-level team news (espn_news, Moneyline PR #29): the first match has confirmed
# lineups (and one ESPN injury beside FPL's list, plus one FPL already has, which the app
# drops), the second has none yet. ESPN's soccer feed has no injuries today; the fixture
# keeps two so the display is tested.
def add_espn_team_news(data: dict) -> None:
    def xi(fx: dict, side: str) -> list[str]:
        out = {a["name"] for a in ((fx.get("news") or {}).get(side) or {}).get("absences", [])}
        names = [
            p["player"]
            for p in fx.get("players", [])
            if p.get("team") == fx[side] and p["player"] not in out
        ]
        return (names + [f"{fx[side]} player {i}" for i in range(1, 12)])[:11]

    first, second = data["fixtures"][0], data["fixtures"][1]
    ko = pd.Timestamp(first["kickoff"])
    at = (ko - pd.Timedelta(minutes=50)).isoformat()
    fpl = ((first.get("news") or {}).get("home") or {}).get("absences") or []
    first["team_news"] = {
        "source": "ESPN",
        "event_id": "401879268",
        "fetched_at": at,
        "updated": None,
        "injuries": {
            "home": [{"name": fpl[0]["name"], "status": "Out", "detail": "Hamstring"}]
            if fpl
            else [],
            "away": [{"name": "Jaka Bijol", "status": "Doubtful", "detail": "Knock"}],
        },
        "lineup": {
            "confirmed": True,
            "fetched_at": at,
            "first_confirmed_at": (ko - pd.Timedelta(minutes=65)).isoformat(),
            "home": {
                "starters": xi(first, "home"),
                "subs": [f"{first['home']} sub {i}" for i in range(1, 8)],
            },
            "away": {
                "starters": xi(first, "away"),
                "subs": [f"{first['away']} sub {i}" for i in range(1, 8)],
            },
        },
    }
    second["team_news"] = {
        "source": "ESPN",
        "event_id": "401879269",
        "fetched_at": (pd.Timestamp(second["kickoff"]) - pd.Timedelta(hours=20)).isoformat(),
        "updated": None,
        "injuries": {"home": [], "away": []},
        "lineup": {
            "confirmed": False,
            "fetched_at": (pd.Timestamp(second["kickoff"]) - pd.Timedelta(hours=20)).isoformat(),
        },
    }


# Record > Goals & corners: the committed research file through publish.markets_research,
# with the live team-total test's progress counted from synthetic logged rows (fixed, so
# the fixture is reproducible; the real log grows every build): 12 matches with FanDuel's
# early price, 8 with both prices, 5 of them with a result.
def add_markets_research(data: dict) -> None:
    rows, results = [], []
    for i in range(12):
        ko = NOW + pd.Timedelta(days=i - 8)
        base = {"league": "E0", "home": f"Home {i}", "away": f"Away {i}", "book": "fanduel"}
        base |= {"kickoff": ko.isoformat(), "team": f"Home {i}", "side": "home", "line": 1.5}
        base |= {"over": 2.0, "under": 1.8, "fair_over": 0.48, "fair_under": 0.52}
        base |= {"p_model_over": 0.5, "minutes_before": 20.0}
        snaps = ("look", "close") if i < 8 else ("look",)
        for snap in snaps:
            at = (ko - pd.Timedelta(hours=24 if snap == "look" else 0.3)).isoformat()
            rows.append(base | {"snapshot": snap, "downloaded_at": at, "fetched_at": at})
        if i < 5:
            results.append(
                {"league": "E0", "home": f"Home {i}", "away": f"Away {i}", "date": ko.normalize()}
                | {"home_goals": i % 3, "away_goals": 1}
            )
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "E0_team_totals_2026-10.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n"
        )
        data["markets_research"] = _clean(markets_research([tmp], pd.DataFrame(results)))


# Portfolio's research studies (owner, 28 Oct: every tested strategy shows in Portfolio):
# the real committed comeback study through publish.research_studies, every bet kept, plus
# synthetic studies for the states the real files don't have yet: one running (pre-registered,
# prices being bought), one in-play that passed with cash-out returns and a generic Why, and
# one pre-match that failed (Moneyline's Archive). The fixture stays under 1.5 MB.
def _study_bets(n, start, seed, design="A", cashout=False):
    rng = np.random.default_rng(seed)
    clubs = [("Arsenal", "Leeds"), ("Chelsea", "Fulham"), ("Betis", "Celta"), ("Lyon", "Nice")]
    out = []
    for i in range(n):
        home, away = clubs[i % len(clubs)]
        odds = round(float(rng.uniform(1.6, 4.5)), 2)
        won = bool(rng.random() < 1 / odds + (0.03 if cashout else -0.02))
        b = {"date": (pd.Timestamp(start) + pd.Timedelta(days=3 * i)).strftime("%Y-%m-%d")}
        b |= {"lg": ["E0", "SP1", "F1"][i % 3], "home": home, "away": away, "odds": odds}
        b |= {"implied": round(1 / odds, 4), "won": won, "design": design}
        if cashout:
            b["pick"] = f"{home} to win, cashed out at half-time"
            b["profit"] = round(float(rng.uniform(-0.6, 0.9)), 3)
            b["result_text"] = (
                f"Cashed out {'+' if b['profit'] >= 0 else '−'}${abs(b['profit']):.2f}"
            )
        out.append(b)
    return out


def _roi_range(g):
    roi = round(sum(b.get("profit", b["odds"] - 1 if b["won"] else -1) for b in g) / len(g), 4)
    return {"roi": roi, "roi_ci95": [round(roi - 0.3, 4), round(roi + 0.3, 4)]}


def _design(bets, role, label, band_noun=None):
    won = sum(b["won"] for b in bets)
    profit = sum(b.get("profit", b["odds"] - 1 if b["won"] else -1) for b in bets)
    implied = sum(b["implied"] for b in bets) / len(bets)
    bands = []
    for lo, hi in ((0.2, 0.4), (0.4, 0.6), (0.6, 1.0)):
        g = [b for b in bets if lo <= b["implied"] < hi]
        if g:
            gw = sum(b["won"] for b in g)
            gp = sum(b.get("profit", b["odds"] - 1 if b["won"] else -1) for b in g)
            bands.append({"lo": lo, "hi": hi, "bets": len(g), "wins": gw})
            bands[-1] |= {"win_rate": round(gw / len(g), 4)}
            bands[-1] |= {"implied": round(sum(b["implied"] for b in g) / len(g), 4)}
            bands[-1] |= {"roi": round(gp / len(g), 4)}
            bands[-1] |= {"roi_ci95": [round(gp / len(g) - 0.4, 4), round(gp / len(g) + 0.4, 4)]}
    roi = round(profit / len(bets), 4)
    d = {"role": role, "label": label, "bets": len(bets), "wins": won}
    d |= {"win_rate": round(won / len(bets), 4), "implied": round(implied, 4)}
    d |= {"implied_matches": len(bets), "gap": round(won / len(bets) - implied, 4)}
    d |= {"gap_ci95": [-0.08, 0.06], "roi": roi, "roi_ci95": [roi - 0.15, roi + 0.15]}
    by_lg = {}
    for b in bets:
        by_lg.setdefault(b["lg"], []).append(b)
    d["by_league"] = [
        {"code": lg, "bets": len(g), "wins": sum(b["won"] for b in g)}
        | {"win_rate": round(sum(b["won"] for b in g) / len(g), 4), "implied": 0.4}
        | _roi_range(g)
        for lg, g in by_lg.items()
    ]
    d["by_band"] = bands
    return d


# The cash-out test (lab/cashout_test.py) as its app file will carry it: the analysis output
# (variants per threshold and margin with growth per stake, calibration of the in-play chance,
# by league and season) under a judged design, plus bankroll curves and the decisions. Made up.
def _cashout_variant(name, mean, base_mean, decisions=None, t=None, m=None, s=None):
    v = {
        "name": name,
        "mean": mean,
        "sd": 1.9,
        "ci95": [round(mean - 0.11, 5), round(mean + 0.11, 5)],
    }
    diff = round(mean - base_mean, 5)
    v["minus_hold"] = None if name == "hold" else [round(diff - 0.03, 5), round(diff + 0.03, 5)]
    v["decisions"] = decisions
    v["growth"] = {}
    for stake in ("0.01", "0.02", "0.05"):
        g = round(diff * float(stake) * 0.9, 6)
        v["growth"][stake] = {
            "mean": round(-0.0004 * float(stake) * 50, 6),
            "ci95": [-0.002, 0.001],
        }
        v["growth"][stake] |= {"minus_hold": g, "drawdown": round(0.2 + float(stake) * 4, 4)}
        v["growth"][stake]["minus_hold_ci95"] = (
            None if name == "hold" else [round(g - 0.0006, 6), round(g + 0.0004, 6)]
        )
    if t is not None:
        v |= {
            "t": t,
            "m": m,
            "at_ht": int(decisions * 0.6),
            "at_m70": decisions - int(decisions * 0.6),
        }
    if s is not None:
        v |= {"m": m, "s": s}
    return v


def _cashout_band(positions, base, role, label):
    variants = [_cashout_variant("hold", base, base)]
    for t in (0.4, 0.5, 0.6):
        for m in (0.0, 0.05, 0.1):
            dec = int(positions * (0.16 - (t - 0.4) * 0.3))
            mean = round(base - 0.25 * m * (dec / positions) - (t - 0.5) * 0.01, 5)
            variants.append(_cashout_variant(f"cash T={t:.2f} m={m:.2f}", mean, base, dec, t, m))
    for m in (0.0, 0.05, 0.1):
        for stake in (0.01, 0.02, 0.05):
            mean = round(base - 0.1 * m, 5)
            variants.append(
                _cashout_variant(f"kelly m={m:.2f} s={stake:.2f}", mean, base, None, s=stake, m=m)
            )
    calib = []
    for moment in ("ht", "m70"):
        for lo, hi, won, q in (
            (0.0, 0.4, 0.21, 0.23),
            (0.4, 0.55, 0.47, 0.48),
            (0.55, 1.0, 0.71, 0.69),
        ):
            n = 140 if moment == "ht" else 95
            calib.append({"moment": moment, "lo": lo, "hi": hi, "n": n, "won": won, "implied": q})
            calib[-1] |= {
                "gap": round(won - q, 4),
                "gap_ci95": [round(won - q - 0.07, 4), round(won - q + 0.07, 4)],
            }
    return {"role": role, "label": label, "positions": positions, "won": 0.19, "avg_entry": 6.1} | {
        "quoted_ht": int(positions * 0.8),
        "quoted_m70": int(positions * 0.78),
        "variants": variants,
        "calibration": calib,
    }


def _cashout_study():
    primary = _cashout_band(2400, -0.041, "judged", "Underdogs at 4.0 and up, 2024/25 onward")
    primary["counts"] = {"matches": 4100, "not_priced": 310, "unmatched": 42, "no_result": 3}
    primary["by_league"] = {
        lg: {"positions": n, "decisions": n // 7, "hold": h, "cash": round(h - 0.012, 4)}
        | {"growth_minus_hold": round(-0.00012 * (i + 1), 6)}
        for i, (lg, n, h) in enumerate((("E0", 300, -0.02), ("SP1", 280, -0.06), ("D2", 220, 0.03)))
    }
    primary["by_season"] = {
        se: {"positions": n, "decisions": n // 7, "hold": h, "cash": round(h - 0.01, 4)}
        | {"growth_minus_hold": -0.0002}
        for se, n, h in (("2024/25", 1500, -0.05), ("2025/26", 900, -0.03))
    }
    second = _cashout_band(1700, -0.03, "descriptive", "Underdogs at 3.0 to 4.0")
    # (the app reads only hold and the judged rule from a descriptive band)
    second["variants"] = [
        v for v in second["variants"] if v["name"] in ("hold", "cash T=0.50 m=0.05")
    ]
    second.pop("calibration")
    rng = np.random.default_rng(5)
    hold, cash, points = 1000.0, 1000.0, []
    for k in range(60):
        r = float(rng.normal(-0.0008, 0.02))
        hold *= 1 + r
        cash *= 1 + r * 0.6 - 0.0003
        day = (pd.Timestamp("2024-08-17") + pd.Timedelta(days=7 * k)).strftime("%Y-%m-%d")
        points.append({"date": day, "hold": round(hold, 2), "cash": round(cash, 2)})
    bets = []
    for k in range(40):
        q = round(float(rng.uniform(0.5, 0.8)), 3)
        entry = round(float(rng.uniform(4.0, 9.0)), 2)
        won = bool(rng.random() < q - 0.02)
        day = (pd.Timestamp("2024-08-17") + pd.Timedelta(days=9 * k)).strftime("%Y-%m-%d")
        bets.append(
            {"date": day, "lg": ["E0", "SP1", "D2"][k % 3], "home": "Leeds", "away": "Hull"}
        )
        bets[-1] |= {"odds": entry, "won": won, "moment": "ht" if k % 3 else "m70", "q": q}
        bets[-1] |= {"hold": round(entry - 1 if won else -1.0, 3)}
        bets[-1] |= {"cash": round(entry * q * 0.95 - 1, 3), "design": "primary"}
    return {
        "id": "cashout_ht",
        "portfolio": "comebacks",
        "name": "Cash out a heavy underdog once it reaches 50%",
        "sample_note": "Matches already bought for the comeback tests (designs B, C and H) are "
        "left out, so no outcome is read twice. Two descriptive samples put them back.",
        "kind": "cashout",
        "title": "Cash-out test: heavy underdogs at every score state",
        "question": "Does cashing out a heavy-underdog bet once its in-play chance reaches 50% "
        "raise long-run bankroll growth compared with holding to the end?",
        "rule": "$1 on the underdog at 4.0+ at the close; cash out at half-time or the 70th "
        "minute when DraftKings' in-play chance is 50% or more, at a 5% cash-out margin.",
        "verdict": "fail",
        "verdict_text": "No: cashing out at 50% lowered bankroll growth at a 2% stake.",
        "closed_at": "2026-10-04",
        "preregistered": {"X": "b88f192"},
        "cost": {"total": 148200, "design_x": 148200},
        "primary_rule": {"t": 0.5, "m": 0.05, "s": 0.02},
        "test_verdict": {"verdict": "fail", "decisions": 336, "growth_minus_hold": -0.00031}
        | {"ci95": [-0.0007, 0.00005], "below_zero": False},
        "designs": {
            "primary": primary,
            "band_3_4": second,
            # Research's file adds wider samples as descriptive designs (lead, 28 Oct); one
            # with the judged rule's numbers, one with its size alone.
            "with_bc": {
                "role": "descriptive",
                "label": "With B and C put back (already read, half-time only)",
                "positions": 2900,
                "variants": [v for v in second["variants"]],
            },
            "season_2526": {
                "role": "descriptive",
                "label": "2025/26 only (nothing excluded)",
                "positions": 1100,
            },
        },
        "curves": {"stake": 0.02, "start": 1000, "points": points},
        "bets": bets,
        "why": [
            {
                "title": "1. The idea",
                "text": "A fair in-play price makes cashing out a wash on average; the margin "
                "makes it a cost. It can only help if the price undervalues the leading underdog.",
            },
            {"title": "2. What it means", "text": "Made-up study for the fixture."},
        ],
        "notes": ["Made-up study for the fixture, in the cash-out test's shape."],
        "runs": {"fetch": "1", "analysis": "2"},
    }


def _passed_study():
    """A made-up study that passed, for the Passed pill, cash-out style returns per bet
    (`profit`, `result_text`) and a Why with rows."""
    bets = _study_bets(36, "2024-08-17", 7, cashout=True)
    return {
        "id": "fixture_pass",
        "portfolio": "comebacks",
        "name": "Made-up study that passed",
        "title": "A made-up passing study",
        "question": "Does cashing out a pre-match bet at half-time beat holding it?",
        "rule": "$1 on the pre-match favourite, cashed out at half-time at the in-play price.",
        "verdict": "pass",
        "verdict_text": "Yes on this sample: cashing out returned more than holding.",
        "closed_at": "2026-10-03",
        "preregistered": {"A": "f64d618"},
        "cost": {"total": 640, "design_a": 600, "probes": 40},
        "price_name": "Pre-match price",
        "band_title": "Won vs the price, by pre-match chance",
        "band_note": "Rows group the bets by the pre-match price's chance.",
        "designs": {"A": _design(bets, "judged", "Design A: three leagues, 2024/25")},
        "bets": bets,
        "why": [
            {
                "title": "1. The idea",
                "text": "Holding to full time adds risk the price doesn't pay for.",
                "ref_name": "Price",
                "rows": [
                    {"label": "Ahead at half-time", "n": 120, "won": 0.71, "ref": 0.68},
                    {"label": "Level at half-time", "n": 90, "won": 0.44, "ref": 0.47},
                ],
            },
            {"title": "2. What it means", "text": "Made-up study for the fixture."},
        ],
        "notes": ["Made-up study for the fixture."],
        "runs": {"fetch": "1", "analysis": "2"},
    }


# The same replays at DraftKings' own historical prices (lead brief, 28 Oct; Moneyline's
# strategy_backtests.lean_dk / .edge12_dk, kind "draftkings", the Pinnacle files' shape): the
# real files from data-log when they are there (trimmed), else made up from the Pinnacle
# replay's own numbers (ROI 2 points and CLV 1 point worse, 2020/21 to 2024/25), the Premier
# League and All only, to stay under 1.5 MB.
DK_SEASONS = ["2020/21", "2021/22", "2022/23", "2023/24", "2024/25"]


def _dk_metrics(m: dict | None) -> dict | None:
    if not m:
        return m
    out = dict(m)
    for k, shift in (("roi", -0.02), ("clv", -0.01)):
        if isinstance(out.get(k), (int, float)):
            out[k] = round(out[k] + shift, 4)
        ci = out.get(f"{k}_ci95")
        if isinstance(ci, list) and len(ci) == 2 and all(isinstance(v, (int, float)) for v in ci):
            out[f"{k}_ci95"] = [round(v + shift, 4) for v in ci]
    return out


def _dk_part(p: dict, metric: tuple, name: str | None = None) -> dict:
    out = {"kind": "draftkings", "seasons_included": DK_SEASONS}
    out |= {k: _dk_metrics(p.get(k)) for k in metric if p.get(k)}
    out["by_odds_band"] = [_dk_metrics(b) for b in p.get("by_odds_band") or []]
    return out | ({"name": name} if name else {})


DK_BETS = 20  # real DraftKings-price backtest rows kept


def _trim_dk(f: dict) -> None:
    """The real file, smaller: its newest DK_BETS bets, no calibration (the Model view reads
    the Pinnacle files'), bankroll curves only under All and the Premier League, each at
    every eighth point."""
    if f.get("bets"):
        f["bets"] = f["bets"][:DK_BETS]
        f["bets_shown"] = len(f["bets"])
    for code, part in [("", f.get("all")), *(f.get("leagues") or {}).items()]:
        if not isinstance(part, dict):
            continue
        part.pop("calibration", None)
        if code not in ("", "E0"):
            part.pop("sizing", None)
        (part.get("pinnacle_same_matches") or {}).pop("by_season", None)
        for block in (part.get("sizing") or {}).values():
            c = block.get("curve") if isinstance(block, dict) else None
            if isinstance(c, list) and len(c) > 20:
                block["curve"] = c[::8] + ([c[-1]] if (len(c) - 1) % 8 else [])


def add_draftkings_backtests(sb: dict) -> None:
    for key, metric in (("lean", ("lean", "strong")), ("edge12", ("edge12",))):
        if (sb.get(f"{key}_dk") or {}).get("available"):  # Moneyline's real file (28 Oct)
            _trim_dk(sb[f"{key}_dk"])
            continue
        src = sb.get(key) or {}
        if not src.get("available"):
            continue

        e0 = (src.get("leagues") or {}).get("E0")
        if not e0 or e0.get("error"):
            continue
        sb[f"{key}_dk"] = {
            "available": True,
            "kind": src.get("kind"),
            "book": "draftkings",
            "coverage_note": "Made up for the fixture: DraftKings priced the match 48 hours "
            "before kickoff, and Pinnacle's close was in the same snapshot.",
            "title": f"{'Lean' if key == 'lean' else 'Model 12%'} replayed on DraftKings' "
            "historical prices",
            "note": "Made up for the fixture from the Pinnacle replay until Moneyline's file "
            "lands.",
            "generated_at": src.get("generated_at"),
            "rule": src.get("rule"),
            "leagues": {"E0": _dk_part(e0, metric, e0.get("name"))},
            "all": _dk_part(src.get("all") or {}, metric),
            "bet_fields": src.get("bet_fields"),
            "bets": (src.get("bets") or [])[:3],
            "bets_shown": min(3, len(src.get("bets") or [])),
            "bets_total": min(3, len(src.get("bets") or [])),
        }


def _trim_cashout(st: dict) -> None:
    pr = st.get("primary_rule") or {"t": 0.5, "m": 0.05}
    keep = ("hold", f"cash T={pr['t']:.2f} m={pr['m']:.2f}")
    for d in (st.get("designs") or {}).values():
        if d.get("role") == "judged":
            continue
        d["variants"] = [v for v in d.get("variants") or [] if v.get("name") in keep]
        for k in ("calibration", "by_league", "by_season"):
            d.pop(k, None)
    for c in (st.get("curves") or {}, (st.get("curves") or {}).get("flat") or {}):
        pts = c.get("points") or []
        if len(pts) > 60:
            step = len(pts) // 60 + 1
            c["points"] = pts[::step] + ([pts[-1]] if (len(pts) - 1) % step else [])
    bets = st.get("bets") or []
    st["bets"] = bets[:: max(1, len(bets) // 40)][:40]


def add_research_studies(data: dict) -> None:
    studies = _clean(pub.research_studies())
    # The real strategy search (Research, #131) is ~100 KB: the fixture keeps 60 of its best
    # rule's bets (spread over the seasons) and the 12 best rules of each list.
    for st in studies:
        # (the app shows each study bet's `date`; its `kickoff` only adds bytes here)
        for b in st.get("bets") or []:
            b.pop("kickoff", None)
        if st["id"] == "strategy_search":
            bets = st.get("bets") or []
            step = max(1, len(bets) // 60)
            st["bets"] = bets[::step][:60]
            for d in (st.get("designs") or {}).values():
                for k in ("top_clv", "top_roi"):
                    if isinstance(d.get(k), list):
                        d[k] = d[k][:12]
    synthetic = [
        {
            "id": "heavy_fav_ht",
            "portfolio": "comebacks",
            "name": "$1 on a 75%+ favourite trailing at half-time",
            "title": "Heavy favourites coming back",
            "question": "Does a favourite priced 75% or more before kickoff, behind at "
            "half-time, win more often than its in-play price implies?",
            "rule": "$1 on the 75%+ favourite trailing at half-time, at DraftKings' in-play price.",
            "verdict": "pending",
            "verdict_text": "Running: pre-registered, the in-play prices are being bought.",
            "preregistered_at": "2026-10-04",
            "preregistered": {"H": "a291256"},
            "planned": {"bets": 410, "matches": 430, "credits": 4300}
            | {"note": "Design H: eight leagues 2025/26 plus the cups, one snapshot per slot."},
            "cost": {"total": 0},
            "designs": {},
            "bets": [],
            "notes": ["Synthetic study for the fixture."],
        },
        _passed_study(),
    ]
    # The cash-out test: Research's real file (lab/cashout_research.json, #136) when it is
    # committed, trimmed to fit (the judged design whole; hold and the judged rule for the
    # other samples; 60 points a curve; 40 cash-outs); else a made-up one in its shape.
    real = next((x for x in studies if x["id"] == "cashout_ht"), None)
    if real:
        _trim_cashout(real)
    else:
        synthetic.insert(1, _cashout_study())
    data["research_studies"] = studies + synthetic


# Team corners (owner's live test, PR #41) until the real ledger's matches reach the fixture
# (its synthetic clubs never match data-log's rows): a model (f) on every card (an NB2 from
# the card's xG, through the real cdf shape), Pinnacle team-corner quotes on the first three
# Premier League matches through corners_live.add_quotes (one whole line, one live-test pick),
# corners_model per league (one failed fit), and corner trades: one open on the first card's
# pick, then won, lost and a push on earlier matches, in two leagues.
def _corner_cdf(mean: float, r: float = 12.0) -> list[float]:
    from scipy.stats import nbinom

    k = np.arange(corners_live.CDF_MAX + 1)
    return [round(float(x), 5) for x in nbinom.cdf(k, r, r / (r + mean))]


def add_corners(data: dict) -> None:
    cards = [c for c in data["fixtures"] if c.get("league") in ("E0", "SP1")]
    for c in cards:
        mh, ma = round(3.4 + 1.5 * c["xg"][0], 3), round(2.9 + 1.4 * c["xg"][1], 3)
        c["corners"] = {
            "model": "f",
            "home": {"mean": mh, "cdf": _corner_cdf(mh)},
            "away": {"mean": ma, "cdf": _corner_cdf(ma)},
        }
    data["corners_model"] = {
        "E0": {"model": "f", "matches": 731, "window_from": "2024-10-06", "window_to": "2026-10-05"}
        | {"k_for": 9.4, "k_against": 12.1, "mean_home": 5.62, "mean_away": 4.57}
        | {"alpha_home": 0.061, "alpha_away": 0.074}
        | {"cards": sum(c["league"] == "E0" for c in cards)},
        "SP1": {
            "model": "f",
            "matches": 742,
            "window_from": "2024-10-06",
            "window_to": "2026-10-05",
        }
        | {"k_for": 7.8, "k_against": 10.5, "mean_home": 5.31, "mean_away": 4.12}
        | {"alpha_home": 0.055, "alpha_away": 0.069}
        | {"cards": sum(c["league"] == "SP1" for c in cards)},
        "D1": {"error": "ValueError: 120 matches with corner counts in the window (need 300)"},
    }
    e0 = [c for c in cards if c["league"] == "E0"][:3]
    rows = []
    # (card, side, line, shift of Pinnacle's fair over chance from the model's): the first
    # card's home 4.5 is priced far below the model (a 12%+ pick), the rest close to it.
    plan = [(0, "home", 4.5, -0.16), (0, "home", 5.0, -0.02), (0, "away", 3.5, 0.03)]
    plan += [(1, "home", 4.5, 0.01), (1, "away", 4.5, -0.03), (2, "home", 5.5, 0.02)]
    at = (NOW - pd.Timedelta(hours=2)).isoformat()
    for i, side, line, shift in plan:
        c = e0[i]
        probs = corners_live.side_probs(c["corners"][side]["cdf"], line)
        q = min(0.9, max(0.1, probs["over"] / max(1e-9, 1 - probs["push"]) + shift))
        over, under = round(1 / (q * 1.035), 2), round(1 / ((1 - q) * 1.035), 2)
        fair = 1 / over / (1 / over + 1 / under)
        rows.append(
            {"market": corners_live.ROW_MARKET, "league": "E0", "home": c["home"]}
            | {"away": c["away"], "kickoff": pd.Timestamp(c["kickoff"]).isoformat()}
            | {"event_id": f"fx{i}", "snapshot": "look", "book": "pinnacle", "side": side}
            | {"team": c[side], "line": line, "over": over, "under": under}
            | {"fair_over": round(fair, 6), "fair_under": round(1 - fair, 6)}
            | {"margin": round(1 / over + 1 / under - 1, 6), "fetched_at": at}
            | {"time_source": "download", "downloaded_at": at, "minutes_before": 5000.0}
        )
    # DraftKings beside Pinnacle (owner, 20 Oct): the same file, book "draftkings".
    # (card, side, line, shift, sides): card 0 home 4.5 is near the model at DraftKings
    # (no pick there, though Pinnacle's trade is open on that line); card 1 home 4.5 is a
    # DraftKings pick with its own open trade (Pinnacle's line has none); card 0 away 3.5
    # is one-sided (over only); card 2 away 4.5 is quoted at DraftKings only.
    dk_plan = [(0, "home", 4.5, 0.01, "both"), (0, "away", 3.5, 0.02, "over")]
    dk_plan += [(1, "home", 4.5, -0.17, "both"), (2, "away", 4.5, 0.0, "both")]
    for i, side, line, shift, sides in dk_plan:
        c = e0[i]
        probs = corners_live.side_probs(c["corners"][side]["cdf"], line)
        q = min(0.9, max(0.1, probs["over"] / max(1e-9, 1 - probs["push"]) + shift))
        over, under = round(1 / (q * 1.04), 2), round(1 / ((1 - q) * 1.04), 2)
        if sides == "over":
            under = None
        fair = 1 / over / (1 / over + 1 / under) if under else None
        rows.append(
            {"market": corners_live.ROW_MARKET, "league": "E0", "home": c["home"]}
            | {"away": c["away"], "kickoff": pd.Timestamp(c["kickoff"]).isoformat()}
            | {"event_id": f"fx{i}", "snapshot": "look", "book": "draftkings", "side": side}
            | {"team": c[side], "line": line, "over": over, "under": under}
            | {"fair_over": round(fair, 6) if fair else None}
            | {"fair_under": round(1 - fair, 6) if fair else None}
            | {"margin": round(1 / over + 1 / under - 1, 6) if under else None}
            | {"fetched_at": at, "time_source": "download", "downloaded_at": at}
            | {"minutes_before": 5000.0}
        )
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "E0_corners_2026-10.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n"
        )
        corners_live.add_quotes(cards, [tmp])
        corners_live.add_quotes(cards, [tmp], book=corners_live.DK)

    def trade(c, side, line, pick, at, snapshot="look", book="pinnacle"):
        return tr.new_corner_trade(
            pick,
            league=c["league"],
            home=c["home"],
            away=c["away"],
            team_side=side,
            line=line,
            kickoff=pd.Timestamp(c["kickoff"]),
            opened_at=pd.Timestamp(at),
            odds_fetched_at=pd.Timestamp(at).isoformat(),
            snapshot=snapshot,
            book=book,
        ) | {"model_mean": c.get("corners", {}).get(side, {}).get("mean")}

    first = e0[0]
    ln = next(x for x in first["corners"]["pinnacle"]["home"] if x["pick"])
    open_t = trade(first, "home", ln["line"], ln["pick"], at)
    open_t |= {
        "close_odds": ln["pick"]["odds"],
        "close_prices": {"over": ln["over"], "under": ln["under"]},
    }
    open_t |= {"close_fetched_at": at, "close_snapshot": "look"}
    open_t |= {
        "close_minutes_before": round(
            (pd.Timestamp(first["kickoff"]) - pd.Timestamp(at)).total_seconds() / 60, 1
        )
    }
    open_t["clv_pinnacle"] = tr.corner_clv(open_t["odds"], open_t["side"], ln["over"], ln["under"])
    open_t["beat_close_pinnacle"] = open_t["clv_pinnacle"] > 0
    settled = []
    # league, home, away, kickoff, side, line, pick side, odds, model_p, push, count, close o/u
    past = [
        (
            "E0",
            "Chelsea",
            "Brentford",
            "2026-09-27T14:00:00Z",
            "home",
            5.5,
            "over",
            2.05,
            0.58,
            0.0,
            8,
            1.88,
            1.96,
        ),
        (
            "SP1",
            "Sevilla",
            "Getafe",
            "2026-09-28T19:00:00Z",
            "away",
            3.5,
            "under",
            2.2,
            0.55,
            0.0,
            6,
            2.30,
            1.65,
        ),
        (
            "E0",
            "Everton",
            "Fulham",
            "2026-10-04T14:00:00Z",
            "away",
            5.0,
            "over",
            2.1,
            0.5,
            0.12,
            5,
            2.05,
            1.80,
        ),
    ]
    past += corner_history()
    for lg, h, a, ko, side, line, ps, odds, mp, push, count, co, cu in past:
        c = {"league": lg, "home": h, "away": a, "kickoff": ko}
        pk = {"side": ps, "odds": odds, "model_p": mp, "p_push": push}
        pk["edge"] = round(mp * odds + push - 1, 4)
        kick = pd.Timestamp(ko)
        t = trade(c, side, line, pk, kick - pd.Timedelta(hours=26))
        t |= {"model_mean": round(line + (0.9 if ps == "over" else -0.8), 2)}
        clv = tr.corner_clv(odds, ps, co, cu)
        t |= {"close_odds": co if ps == "over" else cu, "close_prices": {"over": co, "under": cu}}
        t |= {"close_fetched_at": (kick - pd.Timedelta(minutes=20)).isoformat()}
        t |= {"close_minutes_before": 20.0, "close_snapshot": "close"}
        t |= {"clv_pinnacle": clv, "beat_close_pinnacle": clv > 0}
        hc, ac = (count, 4) if side == "home" else (6, count)
        t |= tr.settle_corner(t, count) | {"score": f"{hc}-{ac} corners"}
        t["settled_at"] = (kick + pd.Timedelta(hours=3)).isoformat()
        settled.append(t)
    # A finished match whose corner count hasn't arrived yet (open, awaiting its result).
    kick = NOW - pd.Timedelta(hours=6)
    c = {"league": "E0", "home": "Crystal Palace", "away": "Brentford", "kickoff": kick}
    pk = {"side": "over", "odds": 1.95, "model_p": 0.6, "p_push": 0.0, "edge": 0.17}
    waiting = trade(c, "away", 4.5, pk, kick - pd.Timedelta(hours=25))
    waiting |= {
        "model_mean": 5.3,
        "close_odds": 1.85,
        "close_prices": {"over": 1.85, "under": 1.97},
    }
    waiting |= {"close_fetched_at": (kick - pd.Timedelta(minutes=15)).isoformat()}
    waiting |= {"close_minutes_before": 15.0, "close_snapshot": "close"}
    clv = tr.corner_clv(1.95, "over", 1.85, 1.97)
    waiting |= {"clv_pinnacle": clv, "beat_close_pinnacle": clv > 0}
    corner_trades = [open_t, waiting, *settled, *dk_corner_trades(e0, trade, at)]
    pfs = {p["id"]: p for p in data["portfolio"]["portfolios"]}
    live_trades = [
        t for p in pfs.values() for t in p["live"]["trades"] if tr.portfolio_of(t) != "corners"
    ]
    backtests = {pid: p["backtest"] for pid, p in pfs.items()}
    if backtests.get("corners") is None:
        backtests["corners"] = paper.corners_research()
    ml = pfs["moneyline"]
    live = {"error": ml["live"].get("error"), "note": ml["live"].get("note")}
    data["portfolio"]["portfolios"] = paper.portfolios_section(
        live,
        live_trades + corner_trades,
        backtests,
        ml["live"].get("rule"),
        {
            "corners": {"rule": tr.corners_rule(), "note": tr.CORNERS_NOTE, "error": None}
            | {"strategies": paper.corner_strategies(corner_trades, NOW)}
        },
        now=NOW,
    )


# Six weeks of settled Pinnacle corner trades (seeded), so "Profit over time" has a real
# shape and the below-60% variant differs from its parent: model
# chances 48% to 70%, over or under, half lines, one a day or so from 1 Sep.
CORNER_PAIRS = [
    ("Liverpool", "Wolves"), ("Spurs", "Burnley"), ("Newcastle", "Bournemouth"),
    ("Man City", "Brighton"), ("West Ham", "Everton"), ("Aston Villa", "Leeds"),
    ("Arsenal", "Nott'm Forest"), ("Man United", "Sunderland"), ("Fulham", "Crystal Palace"),
]  # fmt: skip


def corner_history() -> list[tuple]:
    rng = np.random.default_rng(23)
    rows = []
    for i, day in enumerate(
        pd.date_range("2026-09-01T14:00:00Z", "2026-10-03T14:00:00Z", freq="2D")
    ):
        h, a = CORNER_PAIRS[i % len(CORNER_PAIRS)]
        mp = round(float(rng.uniform(0.48, 0.70)), 3)
        ps = "over" if rng.random() < 0.6 else "under"
        line = float(rng.choice([3.5, 4.5, 5.5]))
        odds = round((1.14 + 0.02 * rng.random()) / mp, 2)
        won = rng.random() < (mp - 0.08 if mp < 0.6 else mp - 0.2)
        count = int(line + 1.5) if (ps == "over") == won else int(line - 0.5)
        co, cu = round(odds * (0.94 + 0.08 * rng.random()), 2), round(1.95 + 0.2 * rng.random(), 2)
        if ps == "under":
            co, cu = cu, co
        side = "home" if i % 2 else "away"
        rows.append(("E0", h, a, day.isoformat(), side, line, ps, odds, mp, 0.0, count, co, cu))
    return rows


# Shadow-filter variants (owner, 23 Oct): paper.corner_strategies adds each track's
# below-60% variant (tr.CORNER_VARIANTS). Its real defined_at (23 Oct) is after this
# fixture's NOW, so nothing would be judged; the fixture moves it to 20 Sep so the
# "Judged from" box and the chart's marker have trades on both sides.
FIXTURE_DEFINED_AT = "2026-09-20T12:00:00+00:00"
tr.CORNER_VARIANTS = {
    k: [v | {"defined_at": FIXTURE_DEFINED_AT} for v in vs] for k, vs in tr.CORNER_VARIANTS.items()
}


def dk_corner_trades(e0, trade, at) -> list[dict]:
    """DraftKings corner trades through paper's own close code: one open (card 1 home
    4.5, DraftKings' pick; Pinnacle's quote at that line is its second yardstick), one
    settled with a two-sided close and one settled with a one-sided close (no DraftKings
    CLV, Pinnacle's CLV only)."""
    out = []
    c = e0[1]
    ln = next(x for x in c["corners"]["draftkings"]["home"] if x["pick"])
    t = trade(c, "home", ln["line"], ln["pick"], at, book="draftkings")
    q = {"over": ln["over"], "under": ln["under"], "downloaded_at": ln["downloaded_at"]}
    t |= paper._dk_close(t, q | {"snapshot": "look"}) or {}
    pin = next(x for x in c["corners"]["pinnacle"]["home"] if x["line"] == ln["line"])
    t |= paper._pinnacle_ref(t, {**pin, "downloaded_at": pin["downloaded_at"]})
    out.append(t)
    # league, home, away, kickoff, side, line, pick side, odds, model_p, count, DK close
    # over/under (under None = one-sided), Pinnacle close over/under.
    past = [
        ("E0", "Brighton", "Wolves", "2026-09-27T14:00:00Z", "home", 5.5, "over")
        + (2.2, 0.55, 7, 1.95, 1.80, 1.90, 1.95),
        ("E0", "Aston Villa", "Fulham", "2026-10-04T16:30:00Z", "away", 4.5, "under")
        + (1.9, 0.62, 6, None, 1.75, 2.02, 1.84),
    ]
    for lg, h, a, ko, side, line, ps, odds, mp, count, dko, dku, po, pu in past:
        c = {"league": lg, "home": h, "away": a, "kickoff": ko}
        pk = {"side": ps, "odds": odds, "model_p": mp, "p_push": 0.0}
        pk["edge"] = round(mp * odds - 1, 4)
        kick = pd.Timestamp(ko)
        t = trade(c, side, line, pk, kick - pd.Timedelta(hours=26), book="draftkings")
        t |= {"model_mean": round(line + (0.9 if ps == "over" else -0.8), 2)}
        got = (kick - pd.Timedelta(minutes=20)).isoformat()
        t |= paper._dk_close(t, {"over": dko, "under": dku, "downloaded_at": got}) or {}
        t |= {"close_snapshot": "close"}
        t |= paper._pinnacle_ref(t, {"over": po, "under": pu, "downloaded_at": got})
        hc, ac = (count, 5) if side == "home" else (3, count)
        t |= tr.settle_corner(t, count) | {"score": f"{hc}-{ac} corners"}
        t["settled_at"] = (kick + pd.Timedelta(hours=3)).isoformat()
        out.append(t)
    return out


# Blend Lean (round 14), the second Moneyline strategy, until data-log has
# backtest/match_blends.json: a synthetic E0 fit with more model weight than the real one
# (c 0.4, sigma 1.5 points) so the fixture has Lean and Strong picks, run through the real
# publish.add_lean. Arsenal v Leeds then holds both strategies on one match: its 12%
# trade (draw, from the real ledger) and a Lean trade on its Strong away pick. Newcastle v
# Tottenham has a Lean pick with no Lean trade (only its 12% one). SP1 has a sigma but no
# odds. Settled Lean trades: two E0 and one SP1, so Lean has tiles and by_league.
SYNTHETIC_BLENDS = {
    "generated_at": "2026-10-06T06:00:00+00:00",
    "leagues": {
        "E0": {"coef": [0.02, -0.016, 0.7, 0.4], "sigma": 0.015, "matches": 1308}
        | {"fit_from": "2022-08-05", "fit_to": "2026-09-20", "fit_date": "2026-10-06"}
        | {"refit": "synthetic"},
        "SP1": {"coef": [0.076, 0.058, 1.073, 0.106], "sigma": 0.019, "matches": 1287}
        | {"fit_from": "2022-08-12", "fit_to": "2026-09-21", "fit_date": "2026-10-06"}
        | {"refit": "synthetic"},
    },
}
LEAN_SETTLED = [
    # league, home, away, kickoff, market, odds, z, z without the model's term, close odds,
    # score, won (driver: market when the second z is still >= 1, else model)
    (
        "E0",
        "Spurs",
        "Brentford",
        "2026-09-20T14:00:00Z",
        "home",
        1.95,
        1.21,
        1.12,
        1.88,
        "2-1",
        True,
    ),
    ("E0", "Wolves", "Everton", "2026-09-27T14:00:00Z", "away", 2.9, 2.15, 0.4, 3.0, "1-1", False),
    ("SP1", "Betis", "Celta", "2026-09-21T19:00:00Z", "home", 1.72, 1.04, 0.71, 1.70, "2-0", True),
]
# Open trades whose matches have started, relative to NOW (the app's trade states, 18 Oct):
# in play (kicked off under ~2 h 15 min ago) and finished, awaiting the result. Prices
# after kickoff aren't logged, so each carries its close (or none) and that CLV is final.
# strategy, league, home, away, minutes since kickoff, market, odds, z (Lean), close odds
STARTED_OPEN = [
    ("lean", "E0", "Aston Villa", "Brentford", 50, "home", 2.1, 1.18, 2.0),
    ("lean", "SP1", "Sevilla", "Valencia", 300, "away", 2.85, 1.07, 3.0),
    ("edge12", "E0", "Bournemouth", "Wolves", 240, "draw", 3.7, None, 3.5),
    ("edge12", "E0", "Brighton", "Everton", 30, "away", 4.2, None, None),
]


# The Lean odds range's start in the fixture: the real one (tr.LEAN_RANGE_FROM, 24 Oct) is
# after the fixture's NOW, so nothing would be judged; paper reads it at call time.
FIXTURE_LEAN_RANGE_FROM = "2026-09-27T00:00:00+00:00"
tr.LEAN_RANGE_FROM = FIXTURE_LEAN_RANGE_FROM
# Out-of-range Lean picks after the start: shadow entries (league, home, away, kickoff,
# market, odds, z, close odds, won).
LEAN_SHADOWS = [
    ("E0", "Burnley", "Man City", "2026-09-28T14:00:00Z", "home", 6.5, 1.3, 6.8, False),
    ("E0", "Liverpool", "Sunderland", "2026-10-02T19:00:00Z", "home", 1.38, 1.1, 1.36, True),
    ("SP1", "Getafe", "Barcelona", "2026-10-04T16:00:00Z", "home", 4.8, 1.2, 4.6, True),
]


# The draw no bet tracks (owner, 26 Oct; paper_dnb): their real start (tr.DNB_FROM) is after
# the fixture's NOW, so it moves to 27 Sep (paper_dnb reads it at call time). Model 12%'s
# track gets synthetic trades: a win, a loss, a draw (stake back), a second league's win and
# one open; Lean's track has none (no sigma_dnb in the fits, as live until the refit).
# league, home, away, kickoff, market, entry 1X2, the model's chance without the draw, the
# close 1X2 (DraftKings), Pinnacle's close 1X2, score (None = open)
FIXTURE_DNB_FROM = "2026-09-27T00:00:00+00:00"
tr.DNB_FROM = FIXTURE_DNB_FROM
# Every new track starts on the original portfolio's start, backcast from logged prices (owner
# via the lead, 28 Oct; Moneyline's backcast block): here 14 Sep, so the first two trades below
# (opened before FIXTURE_DNB_FROM) are backcast ones.
FIXTURE_DNB_STARTED = "2026-09-14T00:00:00+00:00"
DNB_TRADES = [
    ("E0", "Everton", "Wolves", "2026-09-18T19:00:00Z", "home", (2.1, 3.4, 3.6), 0.7)
    + ((2.05, 3.4, 3.7), (2.06, 3.45, 3.75), "1-0"),
    ("SP1", "Sevilla", "Getafe", "2026-09-21T16:00:00Z", "home", (1.8, 3.5, 4.8), 0.86)
    + ((1.8, 3.6, 4.9), (1.82, 3.6, 5.0), "0-1"),
    ("E0", "Chelsea", "Fulham", "2026-09-28T14:00:00Z", "home", (1.9, 3.6, 4.2), 0.84)
    + ((1.85, 3.7, 4.4), (1.86, 3.75, 4.5), "2-0"),
    ("E0", "West Ham", "Arsenal", "2026-09-29T19:00:00Z", "home", (4.5, 3.9, 1.75), 0.42)
    + ((4.8, 4.0, 1.7), (4.9, 4.1, 1.72), "0-2"),
    ("E0", "Leeds", "Crystal Palace", "2026-10-03T14:00:00Z", "away", (2.6, 3.3, 2.8), 0.6)
    + ((2.6, 3.2, 2.75), (2.62, 3.25, 2.8), "1-1"),
    ("SP1", "Villarreal", "Girona", "2026-10-04T19:00:00Z", "home", (1.95, 3.5, 3.9), 0.82)
    + ((1.9, 3.6, 4.1), (1.92, 3.6, 4.2), "3-1"),
    ("E0", "Man United", "Brighton", "2026-10-08T14:00:00Z", "home", (2.2, 3.5, 3.2), 0.74)
    + ((2.2, 3.5, 3.2), None, None),
]


def dnb_trades(rules: dict) -> list[dict]:
    """The synthetic Model 12% draw no bet trades, through paper_dnb's own record and close."""
    ref = {"commit": "synthetic", "xg_weight": 0.7, "matches_fit": 800}
    out = []
    for lg, home, away, ko, market, entry, q, close, pin, score in DNB_TRADES:
        kick = pd.Timestamp(ko)
        other = "away" if market == "home" else "home"
        c = {"home": home, "away": away, "odds": dict(zip(tr.LEAN_MARKETS, entry, strict=True))}
        c["p"] = {market: 0.75 * q, other: 0.75 * (1 - q), "draw": 0.25}
        c["p_bet"] = {market: 0.75 * (q - 0.12), other: 0.75 * (1.12 - q), "draw": 0.25}
        o = tr.dnb_odds(c["odds"][market], c["odds"]["draw"])
        pick = {"market": market, "odds": o, "model_p": q, "edge": q * o - 1, "z": None}
        rule = tr.dnb_rule(rules[lg], "edge12_dnb")
        opened = kick - pd.Timedelta(hours=30)
        fetched = (opened - pd.Timedelta(minutes=20)).isoformat()
        t = pdnb.new_trade(c, pick, "edge12_dnb", rule, kick, opened, fetched, lg, ref)
        quoted = (kick - pd.Timedelta(minutes=10)).isoformat()
        t.update(
            pdnb._close_fields(
                t, dict(zip(tr.LEAN_MARKETS, close, strict=True)), quoted, kick.isoformat()
            )
        )
        if opened < pd.Timestamp(FIXTURE_DNB_FROM):
            t.update(backcast=True, backcast_from="odds_log")
        if score:
            h, a = (int(v) for v in score.split("-"))
            t.update(tr.settle_dnb(t, h, a), result_source="football-data")
            t["settled_at"] = (kick + pd.Timedelta(hours=3)).isoformat()
            c_pin = tr.dnb_clv(t["odds"], market, dict(zip(tr.LEAN_MARKETS, pin, strict=True)))
            t.update(clv_pinnacle=c_pin[0], clv_pinnacle_staked=c_pin[1])
        out.append(t)
    return out


def add_lean_strategy(data: dict) -> None:
    pub.add_lean(data, SYNTHETIC_BLENDS)
    codes = list(data["portfolio"]["rules"])
    leans = {lg: tr.lean_rule(data["match_blends"], lg) for lg in codes}
    for lg in codes:
        data["portfolio"]["rules"][lg] |= {"sigma": leans[lg]["sigma"], "lean": leans[lg]}
    data["portfolio"]["strategy_order"] = list(tr.STRATEGY_ORDER)
    pfs = {p["id"]: p for p in data["portfolio"]["portfolios"]}
    trades = [t for p in pfs.values() for t in p["live"]["trades"]]
    for t in trades:
        if t.get("bet_type", "match") == "match" and t.get("portfolio", "moneyline") == "moneyline":
            t.setdefault("strategy", "edge12")
    ref = {"commit": "synthetic", "xg_weight": 0.7, "matches_fit": 800, "probs": "blend"}

    def lean_trade(lg, home, away, kick, pick, opened):
        t = tr.new_trade(
            pick, source="live", league=lg, home=home, away=away, kickoff=kick,
            opened_at=opened, odds_fetched_at=(opened - pd.Timedelta(minutes=20)).isoformat(),
            threshold=tr.LEAN_Z, model_ref=ref, rule=tr.LEAN_RULE, p_source="blend",
            strategy="lean",
        )  # fmt: skip
        return t | {
            "z": round(pick["z"], 3),
            "tier": pick["tier"],
            "sigma": pick["sigma"],
            "p_bet": round(pick["p_bet"], 4),
            "p_model": round(pick["p_bet"] - 0.02, 4),
            "z_market": pick.get("z_market"),
            "driver": pick.get("driver"),
        }

    fx = next(c for c in data["fixtures"] if (c["home"], c["away"]) == ("Arsenal", "Leeds"))
    t = lean_trade("E0", fx["home"], fx["away"], pd.Timestamp(fx["kickoff"]), fx["lean_pick"], NOW)
    t |= {"close_odds": fx["odds"][t["market"]], "close_fetched_at": t["odds_fetched_at"]}
    lean = [t]
    for lg, home, away, ko, market, odds, z, zm, close, score, won in LEAN_SETTLED:
        kick = pd.Timestamp(ko)
        sigma = SYNTHETIC_BLENDS["leagues"][lg]["sigma"]
        p = 1 / odds + z * sigma
        pick = {"market": market, "odds": odds, "p_bet": p, "model_p": p, "edge": p * odds - 1}
        pick |= {"z": z, "tier": "strong" if z >= tr.STRONG_Z else "lean", "sigma": sigma}
        pick |= {"z_market": zm, "driver": tr.lean_driver(zm)}
        t = lean_trade(lg, home, away, kick, pick, kick - pd.Timedelta(hours=30))
        clv = odds / close - 1
        t |= {"close_odds": close, "close_fetched_at": (kick - pd.Timedelta(minutes=8)).isoformat()}
        t |= {"close_minutes_before": 8, "clv_dk": round(clv, 4), "beat_close_dk": clv > 0}
        t |= {"status": "won" if won else "lost", "score": score}
        t |= {"profit": round(10 * (odds - 1), 2) if won else -10.0}
        t["settled_at"] = (kick + pd.Timedelta(hours=3)).isoformat()
        lean.append(t)
    for strat, lg, home, away, mins, market, odds, z, close in STARTED_OPEN:
        kick = NOW - pd.Timedelta(minutes=mins)
        opened = kick - pd.Timedelta(hours=20)
        if strat == "lean":
            sigma = SYNTHETIC_BLENDS["leagues"][lg]["sigma"]
            p = 1 / odds + z * sigma
            pick = {"market": market, "odds": odds, "p_bet": p, "model_p": p, "edge": p * odds - 1}
            pick |= {"z": z, "tier": "lean", "sigma": sigma, "z_market": 0.6, "driver": "model"}
            t = lean_trade(lg, home, away, kick, pick, opened)
        else:
            p = 1.16 / odds
            pick = {"market": market, "odds": odds, "model_p": p, "edge": p * odds - 1}
            t = tr.new_trade(
                pick, source="live", league=lg, home=home, away=away, kickoff=kick,
                opened_at=opened, odds_fetched_at=(opened - pd.Timedelta(minutes=20)).isoformat(),
                threshold=0.12, model_p_base=p, model_ref=ref | {"probs": "raw"},
                rule="fixed_raw", p_source="model", strategy="edge12",
            )  # fmt: skip
        if close:
            clv = odds / close - 1
            t |= {"close_odds": close, "close_minutes_before": 12, "clv_dk": round(clv, 4)}
            t |= {"close_fetched_at": (kick - pd.Timedelta(minutes=12)).isoformat()}
            t |= {"beat_close_dk": clv > 0}
        (lean if strat == "lean" else trades).append(t)
    # Blend Lean's odds range (owner, 24 Oct): from FIXTURE_LEAN_RANGE_FROM a Lean trade in
    # range is rule version 2; the out-of-range picks are shadow entries (what the old rule
    # would have bet). Wolves v Everton and Sevilla v Valencia, out of range after the start,
    # stay version 1: trades the old code opened in that window (since_defined's old_code).
    start = pd.Timestamp(FIXTURE_LEAN_RANGE_FROM)
    for t in lean:
        if pd.Timestamp(t["opened_at"]) >= start and tr.in_lean_range(t["odds"]):
            t |= {"rule_version": tr.LEAN_RULE_VERSION, "odds_range": list(tr.LEAN_ODDS_RANGE)}
    shadows = []
    for lg, home, away, ko, market, odds, z, close, won in LEAN_SHADOWS:
        kick = pd.Timestamp(ko)
        sigma = SYNTHETIC_BLENDS["leagues"][lg]["sigma"]
        p = 1 / odds + z * sigma
        pick = {"market": market, "odds": odds, "p_bet": p, "model_p": p, "edge": p * odds - 1}
        pick |= {"z": z, "tier": "lean", "sigma": sigma, "z_market": 1.1, "driver": "market"}
        t = lean_trade(lg, home, away, kick, pick, kick - pd.Timedelta(hours=26))
        clv = odds / close - 1
        t |= {"id": t["id"] + tr.SHADOW_SUFFIX, "shadow": True, "excluded_by": "odds_range"}
        t |= {"close_odds": close, "close_fetched_at": (kick - pd.Timedelta(minutes=9)).isoformat()}
        t |= {"close_minutes_before": 9, "clv_dk": round(clv, 4), "beat_close_dk": clv > 0}
        t |= {"status": "won" if won else "lost", "score": "2-1" if won else "0-1"}
        t |= {"profit": round(10 * (odds - 1), 2) if won else -10.0}
        t["settled_at"] = (kick + pd.Timedelta(hours=3)).isoformat()
        shadows.append(t)
    ml = pfs["moneyline"]
    live = {"error": ml["live"].get("error"), "note": ml["live"].get("note")}
    rule = data["portfolio"]["rules"]["E0"]  # paper.run: rules[league], sigma and lean added
    data["portfolio"]["portfolios"] = paper.portfolios_section(
        live,
        trades + lean,
        {pid: p["backtest"] for pid, p in pfs.items()},
        rule,
        {
            "corners": {"rule": tr.corners_rule(), "note": tr.CORNERS_NOTE, "error": None}
            | {"strategies": paper.corner_strategies(pfs["corners"]["live"]["trades"], NOW)}
        },
        paper.match_strategies(leans, rule),
        now=NOW,
        shadow_trades=shadows,
        dnb_trades=dnb_trades(data["portfolio"]["rules"]),
        dnb_rules={
            track: pdnb.overview(
                {
                    lg: tr.dnb_rule(
                        leans[lg] if track == "lean_dnb" else data["portfolio"]["rules"][lg], track
                    )
                    for lg in codes
                },
                track,
            )
            for track in tr.DNB_TRACKS.values()
        },
    )
    add_dnb_backcast(data)


def add_dnb_backcast(data: dict) -> None:
    """The draw no bet tracks' `backcast` blocks start at FIXTURE_DNB_STARTED: paper_dnb builds
    them (backcast.block) with the real 14 Oct start, which is after the fixture's moved
    DNB_FROM. An older paper.py without blocks gets Model 12%'s track's built here."""
    ml = next(p for p in data["portfolio"]["portfolios"] if p["id"] == "moneyline")
    for sec in ml["live"].get("strategies") or []:
        if sec.get("adds") == "dnb" and (sec.get("backcast") or {}).get("applied"):
            sec["backcast"]["started"] = FIXTURE_DNB_STARTED
    sec = next(
        (x for x in ml["live"].get("strategies") or [] if x.get("key") == "edge12_dnb"), None
    )
    if not sec or (sec.get("backcast") or {}).get("applied"):
        return
    bc = [t for t in ml["live"].get("dnb_trades") or [] if t.get("backcast")]
    bc = [t for t in bc if t.get("strategy") == "edge12_dnb"]
    opened = sorted(t["opened_at"] for t in bc)
    sec["backcast"] = {
        "applied": bool(bc),
        "started": FIXTURE_DNB_STARTED,
        "until": FIXTURE_DNB_FROM,
        "defined_at": FIXTURE_DNB_FROM,
        "trades": len(bc),
        "settled": sum(t["status"] in ("won", "lost") or bool(t.get("push")) for t in bc),
        "from": opened[0] if opened else None,
        "to": opened[-1] if opened else None,
        "note": "Backcast from the logged DraftKings prices: the trades this track would have "
        "opened from the portfolio's start, before it was defined. Descriptive only.",
        "descriptive": True,
        "backcast_from": "odds_log",
    }


def add_second_league(data: dict) -> None:
    for fx in data["fixtures"]:
        fx.setdefault("league", "E0")
    base = data["fixtures"][: len(SP1_TEAMS)]
    for fx, (home, away) in zip(base, SP1_TEAMS, strict=True):
        sp = {
            **fx,
            "league": "SP1",
            "home": home,
            "away": away,
            "kickoff": (pd.Timestamp(fx["kickoff"]) + pd.Timedelta(hours=4)).isoformat(),
            "odds": None,
            "implied": None,
            "p_bet": None,
            "odds_updated": None,
            "news": None,
            "news_applied": False,
            "players": [],
            "team_totals": None,
            "team_news": None,
        }
        data["fixtures"].append(sp)
    data["fixtures"].sort(key=lambda f: f["kickoff"])

    pfs = {p["id"]: p for p in data["portfolio"]["portfolios"]}
    ml = pfs["moneyline"]
    template = next(t for t in ml["live"]["trades"] if t.get("bet_type", "match") == "match")
    live_trades = [t for p in pfs.values() for t in p["live"]["trades"]]
    backtests = {pid: p["backtest"] for pid, p in pfs.items()}
    for t in live_trades + (backtests["moneyline"] or {}).get("trades", []):
        t.setdefault("league", "E0")
    bt_trades = []
    for live, home, away, ko, market, odds, won in SP1_TRADES:
        kick = pd.Timestamp(ko)
        t = {
            **template,
            "id": f"SP1|2627|{home}|{away}",
            "league": "SP1",
            "source": "live" if live else "backtest",
            "season": tr.season_label(kick),
            "kickoff": kick.isoformat(),
            "opened_at": (kick - pd.Timedelta(hours=30)).isoformat(),
            "home": home,
            "away": away,
            "market": market,
            "odds": odds,
            "status": "won" if won else "lost",
            "profit": round(10 * (odds - 1), 2) if won else -10.0,
            "score": "1-1" if market == "draw" and won else None,
            "close_odds": None,
            "close_fetched_at": None,
            "close_minutes_before": None,
            "clv_dk": None,
            "beat_close_dk": None,
            "settled_at": (kick + pd.Timedelta(hours=3)).isoformat(),
        }
        (live_trades if live else bt_trades).append(t)
    if backtests["moneyline"] is not None:
        bt = backtests["moneyline"]
        bt["trades"] = sorted(bt.get("trades", []) + bt_trades, key=lambda t: t["kickoff"])
    # Moneyline's multi-league contract: the leagues in this build, and a minimum per league.
    data["leagues"] = [
        {"code": "E0", "name": "Premier League", "live": True, "fixtures": 0, "odds": "DraftKings"},
        {"code": "SP1", "name": "La Liga", "live": True, "fixtures": 0, "odds": None},
    ]
    for lg in data["leagues"]:
        lg["fixtures"] = sum(f["league"] == lg["code"] for f in data["fixtures"])
    # The live paper rule per league (trades.paper_threshold: under PAPER_RULE "fixed_raw"
    # the owner's 12% on the raw model everywhere), and SP1's learned level as research
    # data in the backtest's by_league, where paper.run puts it with 2+ leagues in play.
    data["portfolio"]["rules"] = {lg: tr.paper_threshold(None, lg) for lg in ("E0", "SP1")}
    if backtests["moneyline"] is not None:
        et = backtests["moneyline"].setdefault("edge_threshold", {})
        et.setdefault("by_league", {})["SP1"] = SP1_EDGE
    live = {"error": ml["live"].get("error"), "note": ml["live"].get("note")}
    data["portfolio"]["portfolios"] = paper.portfolios_section(
        live, live_trades, backtests, ml["live"].get("rule"), now=NOW
    )


# The Champions League (owner, 13 Oct): one card with a model chance from the real
# cross-league fit (cup_model, on a small synthetic Europe through publish.fixture_cards)
# and one odds-only card for a club no football-data league covers (cup_model.no_model_card).
UCL_CLUBS = {"E0": ["Arsenal", "Chelsea", "Leeds", "Burnley"],
             "P1": ["Benfica", "Porto", "Braga", "Arouca"]}  # fmt: skip


def add_champions_league(data: dict) -> None:
    from soccer_stats import cup_model as cm
    from soccer_stats.publish import fixture_cards, implied_probs, safe_fit_info

    rng = np.random.default_rng(13)
    rows = []
    for d in pd.date_range("2025-08-02", "2026-10-04", freq="7D"):
        for code, clubs in UCL_CLUBS.items():
            perm = rng.permutation(clubs)
            for h, a in ((perm[0], perm[1]), (perm[2], perm[3])):
                rows.append({"league": code, "date": d, "home": h, "away": a,
                             "home_goals": int(rng.poisson(1.5)),
                             "away_goals": int(rng.poisson(1.1))})  # fmt: skip
    dom = pd.DataFrame(rows)
    euro = [
        {"id": str(i), "kickoff": pd.Timestamp(d, tz="UTC"), "home": h, "away": a,
         "home_id": h, "away_id": a, "completed": True, "score90": [2, 1], "cup": "UCL"}
        for i, (d, h, a) in enumerate([("2025-10-21 19:00", "Arsenal", "Benfica"),
                                       ("2025-11-05 20:00", "FC Porto", "Chelsea"),
                                       ("2026-02-18 20:00", "Benfica", "Leeds United")])
    ]  # fmt: skip
    mapping = cm.team_map([e[s] for e in euro for s in ("home", "away")] + ["FC Porto"],
                          cm.teams_by_league(dom))  # fmt: skip
    fitted = cm.fit_matches(dom, euro, mapping)
    model = cm.fit(fitted, NOW)
    FIT_INFO["UCL"] = safe_fit_info(model, fitted, "cross_league")
    k = pd.Timestamp("2026-10-21T16:45:00Z")  # after every other card: indexes unchanged
    keys = {"Arsenal": mapping["Arsenal"], "FC Porto": mapping["FC Porto"]}
    row = {"kickoff": k, "home": "Arsenal", "away": "FC Porto", "odds_home": 1.53,
           "odds_draw": 4.3, "odds_away": 6.2, "odds_over25": np.nan, "odds_under25": np.nan,
           "odds_updated": (NOW - pd.Timedelta(minutes=30)).isoformat()}  # fmt: skip
    card = fixture_cards(cm.Named(model, keys), pd.DataFrame([row]),
                         pd.Series({"Arsenal": 60, "FC Porto": 58}), league="UCL")[0]  # fmt: skip
    plain = {"id": "u2", "kickoff": k + pd.Timedelta(hours=2, minutes=15), "home": "FK Qarabag",
             "away": "Chelsea"}  # fmt: skip
    odds = {"odds_home": 6.0, "odds_draw": 4.4, "odds_away": 1.5}
    why = ("No model chance: FK Qarabag is not in the football-data leagues the model rates, "
           "so this match shows DraftKings' odds only.")  # fmt: skip
    extra = {"players": [], "team_totals": None, "team_news": None, "p_bet": None, "z": None,
             "z_market": None, "lean_pick": None}  # fmt: skip
    cards = [{**card, **extra}, {**cm.no_model_card(plain, odds, why, implied_probs), **extra}]
    data["fixtures"] = sorted(data["fixtures"] + _clean(cards), key=lambda f: f["kickoff"])
    data["leagues"].append(
        {
            "code": "UCL",
            "name": "Champions League",
            "live": True,
            "fixtures": 2,
            "odds": "DraftKings",
        }  # fmt: skip
    )


def add_models(data: dict, src: Path) -> None:
    """Portfolio's model view: every synthetic league's match fit (FIT_INFO), the equation
    section rebuilt once every block is in, with data-log's real match_blends.json for the
    blend's per-league numbers (the cards keep SYNTHETIC_BLENDS), and the corners live
    section's quote calibration on synthetic half lines (the fixture logs no quotes)."""
    from soccer_stats import calibration as cal
    from soccer_stats import corners_live as cl
    from soccer_stats import equations as eq

    data.setdefault("match_model", {}).update(FIT_INFO)
    mb = src / "backtest" / "match_blends.json"
    blends = json.loads(mb.read_text()) if mb.exists() else data.get("match_blends")
    data["portfolio"]["models"] = eq.section({**data, "match_blends": blends})
    rng = np.random.default_rng(31)
    qc = {"note": cl.QUOTE_CAL_NOTE}
    for book, n, shift in (("pinnacle", 260, 0.0), ("draftkings", 140, 0.03)):
        model = rng.uniform(0.25, 0.8, n)
        price = np.clip(0.5 + 0.7 * (model - 0.5) + rng.normal(0, 0.04, n) - shift, 0.05, 0.95)
        won = (rng.uniform(size=n) < price).astype(float).tolist()
        group = [f"m{i // 2}" for i in range(n)]
        qc[book] = {
            "quotes": n + 12, "settled": n, "unsettled": 12, "whole_lines": 0, "pushes": 0,
            "matches": len(set(group)),
            "model": cal.reliability(model.tolist(), won, price.tolist(), groups=group),
            "price": cal.reliability(price.tolist(), won, groups=group),
        }  # fmt: skip
    for p in data["portfolio"]["portfolios"]:
        if p["id"] == "corners" and p.get("live"):
            p["live"]["quote_calibration"] = qc


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", help="folder with the data-log files (default: git show)")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args(argv)
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(args.source) if args.source else fetch_data_log(Path(tmp))
        build(src, Path(args.out))
    for f in sorted(Path(args.out).glob("*.json")):
        print(f"{f.relative_to(ROOT)}: {f.stat().st_size / 1024:.0f} KB")


if __name__ == "__main__":
    main()
