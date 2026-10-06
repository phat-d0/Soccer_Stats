"""Write a local data set for the web app, so it can be rendered without the network.

    uv run python tests/web/make_fixture.py            # inputs from origin/data-log
    uv run python tests/web/make_fixture.py --source DIR

Writes tests/fixtures/web/{data.json, players_stats.json, players_backtest.json}.

What is real and what is made up:
- Matches, ratings, the replay record and upcoming fixtures come from a synthetic
  20-team league (tests/conftest.simulate_league), run through publish.build_data.
- Team news and the fixtures' player cards are synthetic.
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
from soccer_stats.player_data import season_stats  # noqa: E402
from soccer_stats.players import Absence, TeamNews  # noqa: E402
from soccer_stats.publish import _clean, build_data, portfolio_placeholder  # noqa: E402

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


def player_cards(detail: dict, fixtures: list[dict]) -> None:
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
                            card["lines"].append(
                                {
                                    "market": market,
                                    "line": line,
                                    "side": "over",
                                    "odds": odds_,
                                    "p": round(pr, 4),
                                    "edge": round(pr * odds_ - 1, 4),
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
    data["portfolio"] = portfolio_placeholder()
    player_cards(detail, data["fixtures"])
    priced = sum(1 for fx in data["fixtures"] for p in fx["players"] if p["lines"])
    data["players_status"] = {
        "gate": {"passed": True, "shots": None, "sot": None, "factors": None},
        "error": None,
        "odds": None,
        "bookmaker": "FanDuel",
        "players": sum(len(fx["players"]) for fx in data["fixtures"]),
        "priced": priced,
        "active_players": 520,
    }
    # Portfolio through the real ledger code, on a scratch copy of the data-log files.
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "log"
        shutil.copytree(src, log)
        results = pd.DataFrame(columns=["home", "away", "season", "date", "home_goals"])
        paper.run(data, log, results, league="E0", now=NOW)

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
