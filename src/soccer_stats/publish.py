"""Build the static phone app: run the model and write `data.json` next to the web files.

    soccer-stats publish --out _site

The web app (web/) is copied as-is; everything it shows comes from data.json, so the
site needs no server and can be hosted on GitHub Pages.
"""

from __future__ import annotations

import json
import math
import shutil
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from soccer_stats import dashboard
from soccer_stats.backtest import simulate_bets
from soccer_stats.data import current_season, load_fixtures, load_matches
from soccer_stats.models import DixonColes
from soccer_stats.xg import with_xg

WEB_DIR = Path(__file__).resolve().parents[2] / "web"
TRAIN_SEASONS = 3
XG_WEIGHT = 0.7
MATRIX_GOALS = 5  # score grid shown in the app: 0..5 goals each side


def _clean(obj):
    """Make numpy/pandas values JSON-safe (NaN -> null, rounded floats)."""
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_clean(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return _clean(obj.tolist())
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, float | np.floating):
        return None if math.isnan(obj) else round(float(obj), 4)
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()
    return obj


def model_params(model: DixonColes) -> dict:
    """Everything the app needs to recompute any matchup in the browser."""
    return {
        "intercept": model.params["intercept"],
        "home_adv": model.params["home_adv"],
        "rho": model.params["rho"],
        "attack": model.attack,
        "defence": model.defence,
    }


def fixture_cards(model: DixonColes, fixtures: pd.DataFrame, counts: pd.Series) -> list[dict]:
    if fixtures.empty:
        return []
    preds = dashboard.predict_fixtures(model, fixtures, counts)
    cards = []
    for r in preds.to_dict("records"):
        m = model.score_matrix(r["home"], r["away"])
        cards.append(
            {
                "kickoff": r["kickoff"],
                "home": r["home"],
                "away": r["away"],
                "xg": [r["xg_home"], r["xg_away"]],
                "p": {
                    "home": r["p_home"],
                    "draw": r["p_draw"],
                    "away": r["p_away"],
                    "over25": r["p_over25"],
                    "under25": r["p_under25"],
                    "btts": r["p_btts"],
                },
                "odds": {
                    "home": r["odds_home"],
                    "draw": r["odds_draw"],
                    "away": r["odds_away"],
                    "over25": r["odds_over25"],
                    "under25": r["odds_under25"],
                },
                "low_data": bool(r["low_data"]),
                "top_scores": dashboard.top_scorelines(m),
                "matrix": m[: MATRIX_GOALS + 1, : MATRIX_GOALS + 1],
            }
        )
    return cards


def bets_rows(preds: pd.DataFrame) -> list[list]:
    """Every bet with positive edge, compact; the app filters by its own edge threshold."""
    if preds.empty:
        return []
    bets = simulate_bets(preds, min_edge=0.0)
    return [
        [d.strftime("%Y-%m-%d"), h, a, pick, price, edge, clv, profit]
        for d, h, a, pick, price, edge, clv, profit in bets[
            ["date", "home", "away", "pick", "price", "edge", "clv", "profit"]
        ].itertuples(index=False)
    ]


def build_data(matches: pd.DataFrame, fixtures: pd.DataFrame, xg_error: str | None) -> dict:
    season = current_season()
    has_xg = bool(matches["home_xg"].notna().any())
    weight = XG_WEIGHT if has_xg else 0.0
    model = dashboard.fit_model(matches, xg_weight=weight)

    season_matches = matches[matches["date"] >= f"{season}-07-01"]
    teams = sorted(
        set(season_matches["home"]) | set(fixtures.get("home", [])) | set(fixtures.get("away", []))
    ) or sorted(model.teams)
    counts = dashboard.match_counts(matches, teams)

    ratings = dashboard.team_ratings(model, counts, teams)
    sxg = dashboard.season_xg(matches, f"{season}-07-01")
    if not sxg.empty:
        ratings = ratings.join(sxg, on="team")

    start = pd.Timestamp(f"{season - 1}-08-01")
    record = {}
    for name, w in [("model", weight)] + ([("goals_only", 0.0)] if has_xg else []):
        preds = dashboard.replay(matches, start, xg_weight=w)
        scores = dashboard.track_record(preds, 0.0)["scores"]
        record[name] = {
            "log_loss": scores.loc["model", "log_loss"] if not scores.empty else None,
            "market_log_loss": scores.loc["market", "log_loss"] if not scores.empty else None,
            "matches": int(scores.loc["model", "n"]) if not scores.empty else 0,
            "bets": bets_rows(preds),
        }

    return _clean(
        {
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "league": "Premier League",
            "season": f"{season}/{(season + 1) % 100:02d}",
            "last_result": matches["date"].max(),
            "matches_fit": len(matches),
            "xg_weight": weight,
            "xg_coverage": float(matches["home_xg"].notna().mean()),
            "xg_error": xg_error,
            "record_start": start,
            "teams": teams,
            "params": model_params(model),
            "fixtures": fixture_cards(model, fixtures, counts),
            "ratings": ratings.to_dict("records"),
            "record": record,
        }
    )


def publish(out: Path, league: str = "E0") -> Path:
    season = current_season()
    matches = load_matches([league], range(season - TRAIN_SEASONS + 1, season + 1))
    matches, xg_error = with_xg(matches)
    try:
        fixtures = load_fixtures([league])
    except Exception:
        fixtures = pd.DataFrame(columns=["kickoff", "home", "away"])

    data = build_data(matches, fixtures, xg_error)
    out.mkdir(parents=True, exist_ok=True)
    shutil.copytree(WEB_DIR, out, dirs_exist_ok=True)
    (out / "data.json").write_text(json.dumps(data, separators=(",", ":")))
    return out
