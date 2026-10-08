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

from soccer_stats import paper  # noqa: E402
from soccer_stats import player_calibration as cal  # noqa: E402
from soccer_stats import trades as tr  # noqa: E402
from soccer_stats.player_data import season_stats  # noqa: E402
from soccer_stats.players import Absence, TeamNews  # noqa: E402
from soccer_stats.publish import (  # noqa: E402
    _clean,
    add_match_blend,
    build_data,
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
SAMPLE_PLAYERS = 70  # players kept from the real detail file (keeps fixtures < 1 MB)


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
        results = pd.DataFrame(columns=["home", "away", "season", "date", "home_goals"])
        paper.run(data, log, results, league="E0", now=NOW)
    add_settled_live_matches(data)
    add_edge_thresholds(data)
    add_second_league(data)
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
    return data


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
    live = data["portfolio"].get("live") or {"trades": []}
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
    data["portfolio"]["live"] = {**live, **paper.portfolio_section(trades)}
    # Rebuild the per-strategy portfolios with these trades, keeping their backtests.
    backtests = {p["id"]: p["backtest"] for p in data["portfolio"].get("portfolios") or []}
    r = data["portfolio"].get("rule") or {}
    match_rule = {
        "threshold": r.get("threshold"),
        "source": r.get("threshold_source"),
        "note": r.get("threshold_note"),
    }
    data["portfolio"]["portfolios"] = paper.portfolios_section(live, trades, backtests, match_rule)
    # The app reads only `portfolios`: leave the old top-level copies out of the fixture
    # (production keeps them for one release), so the smoke test proves nothing needs them.
    data["portfolio"].pop("live", None)
    data["portfolio"].pop("backtest", None)


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
    r = data["portfolio"].get("rule") or {}
    data["portfolio"]["rules"] = {
        "E0": {
            "threshold": r.get("threshold"),
            "source": r.get("threshold_source"),
            "note": r.get("threshold_note"),
        },
        "SP1": {"threshold": SP1_EDGE["min_edge"], "source": "history", "note": SP1_EDGE["note"]},
    }
    live = {"error": ml["live"].get("error"), "note": ml["live"].get("note")}
    data["portfolio"]["portfolios"] = paper.portfolios_section(
        live, live_trades, backtests, ml["live"].get("rule")
    )


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
