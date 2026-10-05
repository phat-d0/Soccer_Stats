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
from soccer_stats.data import RAW_DIR, current_season, load_fixtures, load_matches
from soccer_stats.models import DixonColes
from soccer_stats.odds import devig_shin
from soccer_stats.players import (
    TeamNews,
    align_team_names,
    fetch_fpl,
    fixture_multipliers,
    news_snapshot,
    parse_players,
    team_news,
)
from soccer_stats.xg import LEAGUES as XG_LEAGUES
from soccer_stats.xg import load_schedule, with_xg

WEB_DIR = Path(__file__).resolve().parents[2] / "web"
TRAIN_SEASONS = 3
XG_WEIGHT = 0.7
MATRIX_GOALS = 5  # score grid shown in the app: 0..5 goals each side
FIXTURE_DAYS = 14  # show fixtures this far ahead...
MIN_FIXTURES = 10  # ...but always at least the next round
ODDS_COLS = ["odds_home", "odds_draw", "odds_away", "odds_over25", "odds_under25"]


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


def upcoming_fixtures(
    league: str = "E0", now: pd.Timestamp | None = None, raw_dir: Path = RAW_DIR
) -> pd.DataFrame:
    """Upcoming fixtures for the next FIXTURE_DAYS (at least MIN_FIXTURES), with odds if posted.

    The schedule comes from Understat (whole season, so it works through international
    breaks); odds come from football-data's fixtures file, which only covers about the
    next week. Either source alone is used if the other is unavailable.
    """
    now = now or pd.Timestamp.now(tz="UTC")
    empty = pd.DataFrame(columns=["kickoff", "home", "away"])

    try:
        fd = load_fixtures([league], raw_dir=raw_dir)
        # football-data times are UK local time.
        fd["kickoff"] = (
            fd["kickoff"]
            .dt.tz_localize("Europe/London", ambiguous="NaT", nonexistent="shift_forward")
            .dt.tz_convert("UTC")
        )
    except Exception:
        fd = empty
    try:
        sched = load_schedule(league, current_season(), raw_dir) if league in XG_LEAGUES else empty
    except Exception:
        sched = empty

    merged = sched.merge(fd, on=["home", "away"], how="outer", suffixes=("_us", ""))
    if "kickoff_us" in merged:
        merged["kickoff"] = merged["kickoff"].fillna(merged["kickoff_us"])
        merged = merged.drop(columns="kickoff_us")
    for col in ODDS_COLS:
        if col not in merged:
            merged[col] = np.nan
    if merged.empty:
        return merged

    merged["kickoff"] = pd.to_datetime(merged["kickoff"], utc=True)
    upcoming = merged[merged["kickoff"] >= now - pd.Timedelta(hours=2)].sort_values("kickoff")
    window = upcoming[upcoming["kickoff"] <= now + pd.Timedelta(days=FIXTURE_DAYS)]
    if len(window) < MIN_FIXTURES:
        window = upcoming.head(MIN_FIXTURES)
    return window.reset_index(drop=True)


def implied_probs(r: dict) -> dict:
    """Bookmaker odds as probabilities, with the margin removed so each market sums to 1."""
    out = {k: None for k in ("home", "draw", "away", "over25", "under25")}
    h2h = [r.get("odds_home"), r.get("odds_draw"), r.get("odds_away")]
    if all(pd.notna(o) and o > 1 for o in h2h):
        out["home"], out["draw"], out["away"] = devig_shin(np.array(h2h, dtype=float))
        out["margin"] = float(sum(1 / o for o in h2h) - 1)
    ou = [r.get("odds_over25"), r.get("odds_under25")]
    if all(pd.notna(o) and o > 1 for o in ou):
        out["over25"], out["under25"] = devig_shin(np.array(ou, dtype=float))
    return out


def _news_card(news: TeamNews | None) -> dict | None:
    if news is None:
        return None
    return {
        "attack_mult": news.attack_mult,
        "defence_mult": news.defence_mult,
        "absences": [a.__dict__ for a in news.absences],
    }


def fixture_cards(
    model: DixonColes,
    fixtures: pd.DataFrame,
    counts: pd.Series,
    news: dict[str, TeamNews] | None = None,
    mults: dict[tuple[str, str], tuple[float, float]] | None = None,
) -> list[dict]:
    if fixtures.empty:
        return []
    preds = dashboard.predict_fixtures(model, fixtures, counts, mults)
    news = news or {}
    cards = []
    for r in preds.to_dict("records"):
        mult = r["mults"]
        m = model.score_matrix(r["home"], r["away"], *mult)
        adjusted = mult != (1.0, 1.0)
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
                "implied": implied_probs(r),
                "low_data": bool(r["low_data"]),
                # Team news: applied only to each team's next match (see players.py).
                "news_applied": adjusted,
                "news": {
                    "home": _news_card(news.get(r["home"])),
                    "away": _news_card(news.get(r["away"])),
                }
                if (r["home"] in news or r["away"] in news)
                else None,
                "xg_mult": list(mult),
                "p_base": {
                    "home": r["p_home_base"],
                    "draw": r["p_draw_base"],
                    "away": r["p_away_base"],
                    "over25": r["p_over25_base"],
                }
                if adjusted
                else None,
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


def build_data(
    matches: pd.DataFrame,
    fixtures: pd.DataFrame,
    xg_error: str | None,
    news: dict[str, TeamNews] | None = None,
    news_error: str | None = None,
    now: pd.Timestamp | None = None,
) -> dict:
    season = current_season()
    now = now or pd.Timestamp.now(tz="UTC")
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
            "fixtures": fixture_cards(
                model,
                fixtures,
                counts,
                news,
                fixture_multipliers(fixtures, news, now) if news and not fixtures.empty else None,
            ),
            "news_error": news_error,
            "team_news": {t: n.to_dict() for t, n in (news or {}).items() if t in teams},
            "ratings": ratings.to_dict("records"),
            "record": record,
        }
    )


def publish(out: Path, league: str = "E0") -> Path:
    season = current_season()
    matches = load_matches([league], range(season - TRAIN_SEASONS + 1, season + 1))
    matches, xg_error = with_xg(matches)
    fixtures = upcoming_fixtures(league)

    news, news_error, snapshot = None, None, []
    if league == "E0":  # FPL covers the Premier League only
        try:
            players = parse_players(fetch_fpl())
            this_season = matches[matches["date"] >= f"{season}-07-01"]
            games = pd.concat([this_season["home"], this_season["away"]]).value_counts().to_dict()
            news = align_team_names(team_news(players, games), sorted(set(this_season["home"])))
            snapshot = news_snapshot(players, datetime.now(UTC).isoformat(timespec="minutes"))
        except Exception as exc:  # FPL down or changed: predictions still publish
            news_error = f"Team news unavailable: {exc}"

    data = build_data(matches, fixtures, xg_error, news, news_error)
    with_odds = sum(f["implied"]["home"] is not None for f in data["fixtures"])
    print(
        f"{len(data['fixtures'])} upcoming fixtures ({with_odds} with odds), "
        f"{data['matches_fit']} matches fit, xG coverage {data['xg_coverage']:.0%}"
        + (f", xG error: {xg_error}" if xg_error else "")
    )
    adjusted = [f for f in data["fixtures"] if f["news_applied"]]
    if news:
        unmatched = sorted(set(data["teams"]) - set(news))
        if unmatched:  # an FPL spelling missing from players.TEAM_NAMES
            print(f"WARNING: no FPL team news for {unmatched}; FPL teams: {sorted(news)}")
    print(
        news_error
        or f"Team news: {sum(len(n['absences']) for n in data['team_news'].values())} notable "
        f"absences, applied to {len(adjusted)} fixtures"
    )
    out.mkdir(parents=True, exist_ok=True)
    shutil.copytree(WEB_DIR, out, dirs_exist_ok=True)
    (out / "data.json").write_text(json.dumps(data, separators=(",", ":")))
    # Picked up by the workflow and appended to the data-log branch (injury history).
    (out / "news_snapshot.json").write_text(json.dumps(snapshot, separators=(",", ":")))
    return out
