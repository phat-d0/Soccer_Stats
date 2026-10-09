"""Build the static phone app: run the model and write `data.json` next to the web files.

    soccer-stats publish --out _site

The web app (web/) is copied as-is; everything it shows comes from data.json, so the
site needs no server and can be hosted on GitHub Pages.
"""

from __future__ import annotations

import json
import math
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from soccer_stats import dashboard, espn_news, team_totals
from soccer_stats import leagues as lgs
from soccer_stats import match_calibration as mc
from soccer_stats import trades as tr
from soccer_stats.backtest import simulate_bets
from soccer_stats.data import RAW_DIR, current_season, load_fixtures, load_matches
from soccer_stats.models import DixonColes
from soccer_stats.odds import devig_shin
from soccer_stats.odds_feed import BOOKMAKER_NAME, apply_odds, fetch_odds, parse_odds
from soccer_stats.paper import FRESH_HOURS
from soccer_stats.players import (
    TeamNews,
    align_team_names,
    fetch_fpl,
    fixture_multipliers,
    news_snapshot,
    parse_players,
    team_news,
)
from soccer_stats.trades import FILTER_PRESETS, PAPER_EDGE, STAKE
from soccer_stats.xg import LEAGUES as XG_LEAGUES
from soccer_stats.xg import TEAM_NAMES as XG_TEAM_NAMES
from soccer_stats.xg import fetch_season, load_schedule, with_xg

WEB_DIR = Path(__file__).resolve().parents[2] / "web"
TRAIN_SEASONS = 3
XG_WEIGHT = 0.7
MATRIX_GOALS = 5  # score grid shown in the app: 0..5 goals each side
GOALS_CDF = 6  # team-goal cumulative chances kept per card (team totals up to 6.5)
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


def _season_teams(matches: pd.DataFrame, season: int) -> set[str]:
    """Teams with a match (home or away) in the season so far: the names odds must match."""
    this = matches[matches["date"] >= f"{season}-07-01"]
    return set(this["home"]) | set(this["away"])


def with_draftkings(
    fixtures: pd.DataFrame,
    league: str,
    known_teams: set[str],
    now: pd.Timestamp | None = None,
    share: int = 1,
) -> tuple[pd.DataFrame, dict]:
    """Swap fixture odds for DraftKings odds when an Odds API key is configured.

    Without a key (or before the first successful download) the football-data odds stay.
    Matches DraftKings has priced but the schedule lacks are added. A "matchday" league
    (leagues.py) is told its scheduled kickoffs, so it fetches only near a match; `share`
    is the number of live leagues splitting the credits (1 for the primary league).
    """
    now = now or pd.Timestamp.now(tz="UTC")
    lg = lgs.LEAGUES.get(league)
    if lg is not None and lg.odds_policy != "always" and not fixtures.empty:
        kickoffs = list(pd.to_datetime(fixtures["kickoff"], utc=True))
        events, status = fetch_odds(league, kickoffs=kickoffs, share=share)
    else:
        events, status = fetch_odds(league, share=share)
    source = {
        "league": league,
        "name": "football-data",
        "format": "decimal",
        "fetched_at": status.fetched_at,
        "credits_left": status.credits_left,
        "last_cost": status.last_cost,
        "refresh_hours": status.refresh_hours,
        "error": status.error,
    }
    if events is None:
        return fixtures, source
    dk = align_to_schedule(parse_odds(events, known_teams), fixtures)
    out = apply_odds(fixtures, dk) if not fixtures.empty else fixtures
    have = set(zip(out.get("home", []), out.get("away", []), strict=False))
    horizon = (
        max(out["kickoff"].max(), now + pd.Timedelta(days=FIXTURE_DAYS))
        if not out.empty
        else (now + pd.Timedelta(days=FIXTURE_DAYS))
    )
    unscheduled = np.array(
        [(h, a) not in have for h, a in zip(dk["home"], dk["away"], strict=True)], dtype=bool
    )
    in_window = (dk["kickoff"] >= now - pd.Timedelta(hours=2)) & (dk["kickoff"] <= horizon)
    # A match the schedule lacks joins only when the model knows both teams. Otherwise it
    # is an unmapped spelling (a second card beside the scheduled one, with league-average
    # chances), so it is left out and listed for odds_feed.TEAM_NAMES.
    named = np.array(
        [
            not known_teams or (h in known_teams and a in known_teams)
            for h, a in zip(dk["home"], dk["away"], strict=True)
        ],
        dtype=bool,
    )
    candidates = unscheduled & in_window.to_numpy()
    source["unmatched"] = [
        f"{h} v {a} ({k:%Y-%m-%d %H:%M})"
        for h, a, k in dk.loc[candidates & ~named, ["home", "away", "kickoff"]].itertuples(
            index=False
        )
    ]
    extra = dk[candidates & named]
    if not extra.empty:
        out = (
            pd.concat([out, extra], ignore_index=True).sort_values("kickoff").reset_index(drop=True)
        )
    source.update(name=BOOKMAKER_NAME, format="american")
    return out, source


def align_to_schedule(dk: pd.DataFrame, fixtures: pd.DataFrame, hours: float = 3.0) -> pd.DataFrame:
    """Give a priced match the schedule's team names when only one name differs.

    A DraftKings event whose (home, away) isn't scheduled takes the names of the one
    scheduled fixture within `hours` of its kickoff that shares its home or its away team.
    """
    if dk.empty or fixtures.empty:
        return dk
    out = dk.copy()
    have = set(zip(fixtures["home"], fixtures["away"], strict=True))
    kick = pd.to_datetime(fixtures["kickoff"], utc=True)
    for i, r in out.iterrows():
        if (r["home"], r["away"]) in have:
            continue
        near = (kick - r["kickoff"]).abs() <= pd.Timedelta(hours=hours)
        same = near & ((fixtures["home"] == r["home"]) | (fixtures["away"] == r["away"]))
        if same.sum() == 1:
            fx = fixtures[same].iloc[0]
            out.at[i, "home"], out.at[i, "away"] = fx["home"], fx["away"]
    return out


def duplicate_fixtures(fixtures: list[dict]) -> list[tuple]:
    """Cards that share (league, home, away, kickoff): should always be empty."""
    seen, dups = set(), []
    for f in fixtures:
        key = (f.get("league", lgs.PRIMARY), f.get("home"), f.get("away"), f.get("kickoff"))
        if key in seen:
            dups.append(key)
        seen.add(key)
    return dups


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


def total_goals_cdf(m: np.ndarray, k_max: int = GOALS_CDF) -> np.ndarray:
    """P(home + away goals <= k) for k = 0..k_max from a score matrix."""
    i, j = np.indices(m.shape)
    by_total = np.bincount((i + j).ravel(), weights=m.ravel(), minlength=k_max + 1)
    return np.cumsum(by_total)[: k_max + 1]


TEAM_TOTAL_BOOK = "fanduel"  # the app shows one book: the one logged in all five leagues


def add_team_totals(cards: list[dict], dirs) -> int:
    """Each card's latest FanDuel team-total prices from the logged rows (data-log's
    `<code>_team_totals_<YYYY-MM>.jsonl`, copied into the state folder, plus this run's
    new rows.jsonl): `team_totals = {book, fetched_at, home: [{line, fair_over, over,
    under}], away: [...]}`, newest quote per side and line, taken before kickoff.
    Display only; no API calls. Returns the number of cards given prices."""
    paths = []
    for d in dirs:
        if d:
            paths += sorted(Path(d).glob("*_team_totals_*.jsonl")) + [Path(d) / "rows.jsonl"]
    rows = [r for r in team_totals._read_jsonl(paths) if r.get("book") == TEAM_TOTAL_BOOK]
    latest: dict[tuple, dict] = {}
    for r in rows:
        try:
            key = (
                r.get("league"),
                r["home"],
                r["away"],
                team_totals._ts(r["kickoff"]),
                r["side"],
                float(r["line"]),
            )
            if team_totals._ts(r["downloaded_at"]) >= key[3]:
                continue
        except (KeyError, TypeError, ValueError):
            continue
        if key not in latest or r["downloaded_at"] > latest[key]["downloaded_at"]:
            latest[key] = r
    n = 0
    for c in cards:
        try:
            ko = team_totals._ts(c["kickoff"])
        except (KeyError, TypeError, ValueError):
            continue
        out: dict = {"home": [], "away": []}
        stamps = []
        for (lg, h, a, k, side, line), r in latest.items():
            if (lg, h, a, k) != (c.get("league"), c["home"], c["away"], ko):
                continue
            out[side].append(
                {"line": line, "fair_over": r["fair_over"], "over": r["over"], "under": r["under"]}
            )
            stamps.append(r.get("fetched_at") or r["downloaded_at"])
        if stamps:
            for side in ("home", "away"):
                out[side].sort(key=lambda x: x["line"])
            c["team_totals"] = {"book": TEAM_TOTAL_BOOK, "fetched_at": max(stamps), **out}
            n += 1
    return n


def fixture_cards(
    model: DixonColes,
    fixtures: pd.DataFrame,
    counts: pd.Series,
    news: dict[str, TeamNews] | None = None,
    mults: dict[tuple[str, str], tuple[float, float]] | None = None,
    league: str = lgs.PRIMARY,
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
                "league": league,  # football-data code (leagues.LEAGUES)
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
                # When DraftKings last changed these prices (their last_update), if known.
                "odds_updated": r.get("odds_updated")
                if isinstance(r.get("odds_updated"), str)
                else None,
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
                # P(team scores <= k), k = 0..GOALS_CDF, from the full matrix: the model's
                # team-total chance (team_totals.model_over). Not shown in the app.
                "goals_cdf": {
                    "home": np.cumsum(m.sum(axis=1))[: GOALS_CDF + 1],
                    "away": np.cumsum(m.sum(axis=0))[: GOALS_CDF + 1],
                },
                # P(total goals <= k), k = 0..GOALS_CDF, from the full matrix: the app's
                # total-goals over/under (the shown matrix stops at MATRIX_GOALS a side).
                "total_goals_cdf": total_goals_cdf(m),
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
    league: str = lgs.PRIMARY,
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
            "league": lgs.name(league),
            "league_code": league,
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
                league=league,
            ),
            "news_error": news_error,
            "team_news": {t: n.to_dict() for t, n in (news or {}).items() if t in teams},
            "ratings": ratings.to_dict("records"),
            "record": record,
        }
    )


def portfolio_placeholder() -> dict:
    """Portfolio data before the ledger step (soccer-stats paper) fills it in."""
    return {
        "rule": {
            "threshold": PAPER_EDGE,
            "stake": STAKE,
            "presets": list(FILTER_PRESETS),
            "fresh_hours": FRESH_HOURS,
        },
        "live": {
            "trades": [],
            "summary": {"trades": 0},
            "error": "The paper-trade ledger wasn't updated in this build.",
        },
        "backtest": None,
    }


PLAYER_FETCH_PER_BUILD = 40  # new Understat match files per build (catches up gradually)
PLAYER_SEASONS = 3  # seasons of appearances loaded for live player lines


def player_gate(path: str | None = None) -> dict:
    """The stage-1 result (backtest-players): player odds and trades only once the model
    beats its baseline for both shots and shots on target."""
    path = path or os.environ.get("PLAYER_GATE_FILE")
    try:
        g = json.loads(Path(path).read_text()) if path else {}
    except (OSError, ValueError):
        g = {}
    gate = g.get("gate") or {}
    look = ((g.get("priced") or {}).get("calibration") or {}).get("look") or {}
    coef = look.get("coef")
    return {
        "passed": bool(gate.get("passed")),
        "shots": gate.get("shots"),
        "sot": gate.get("sot"),
        "factors": g.get("factors"),
        "sot_method": g.get("sot_method", "thin"),
        "generated_at": g.get("generated_at"),
        # the blend the live app trades on (player_calibration); None = no player trades
        "calibration": [float(v) for v in coef] if coef and len(coef) == 3 else None,
    }


def match_blend(path: str | None = None) -> dict | None:
    """The live blend coefficients saved by backtest-dk (E0_dk.json -> blend.live), or
    None when there's no file or no fit."""
    path = path or os.environ.get("DK_BACKTEST_FILE")
    try:
        d = json.loads(Path(path).read_text()) if path else {}
    except (OSError, ValueError):
        d = {}
    live = (d.get("blend") or {}).get("live") or {}
    if not all((live.get(g) or {}).get("coef") for g in mc.GROUPS):
        return None
    return {**live, "generated_at": d.get("generated_at")}


def add_match_blend(data: dict, blend: dict | None) -> dict:
    """Give each fixture `p_bet`: the chance its value pick and paper trade use.

    With a blend fit it's the model blended with DraftKings' margin-free price
    (match_calibration); without one, the fixture has no p_bet and the model's own
    chance (`p`) is used, as before. The app's bestPick reads the same field.
    """
    data["match_blend"] = {
        "live": blend is not None,
        "generated_at": (blend or {}).get("generated_at"),
        **{g: (blend or {}).get(g) for g in mc.GROUPS},
    }
    if blend is None:
        return data
    for fx in data.get("fixtures", []):
        p = mc.blend_card(blend, fx["p"], fx.get("implied") or {})
        if p and any(v is not None for v in p.values()):
            fx["p_bet"] = p
    return data


def add_players(data: dict, league: str, fpl_df, credits_left) -> tuple[dict, list[dict]]:
    """Player shot lines for each upcoming fixture, plus season shooting stats per
    player and club (never raises)."""
    from soccer_stats.player_data import active_players, load_appearances, season_stats
    from soccer_stats.player_live import fpl_players, player_cards
    from soccer_stats.player_odds import fetch_live

    season = current_season()
    gate = player_gate()
    from soccer_stats.player_odds import PLAYER_BOOKMAKER_NAME

    status = {
        "gate": gate,
        "error": None,
        "odds": None,
        "bookmaker": PLAYER_BOOKMAKER_NAME,
        "paper_trades": tr.PLAYER_PAPER_TRADES,  # the app says when player trades are off
    }
    stats: list[dict] = []
    try:
        # Three seasons, so the live model trains on up to two years (TRAIN_DAYS) as in
        # the backtest, even at a season's start; the season stats show the last two.
        apps, missing = load_appearances(
            league, range(season - PLAYER_SEASONS + 1, season + 1), max_new=PLAYER_FETCH_PER_BUILD
        )
        status["missing_matches"] = missing
        recent = {f"{y % 100:02d}{(y + 1) % 100:02d}" for y in (season - 1, season)}
        stats = [r for r in season_stats(apps) if r["season"] in recent]
        active = active_players(apps, fpl_df, f"{season % 100:02d}{(season + 1) % 100:02d}")
        for r in stats:
            a = active.get(r["player_id"], {})
            r["active"] = bool(a.get("active"))
            r["current_team"] = a.get("team")
        status["active_players"] = sum(v["active"] for v in active.values())
        events = []
        if gate["passed"]:
            events, status["odds"] = fetch_live(league, credits_left=credits_left)
        # Player lines are Premier League only: other leagues' cards stay out.
        own = [fx for fx in data["fixtures"] if fx.get("league", league) == league]
        cards, st = player_cards(
            apps,
            own,
            fpl_players(fpl_df) if fpl_df is not None else None,
            events,
            factors=gate["factors"],
            sot_method=gate["sot_method"],
            calibration=gate["calibration"],
            active=active,
        )
        status.update(st)
        for fx in data["fixtures"]:
            fx["players"] = cards.get((fx["home"], fx["away"]), [])
    except Exception as exc:  # Understat down or changed: matches still publish
        status["error"] = f"Player lines unavailable ({type(exc).__name__}: {exc})"[:200]
    return status, stats


def team_block(
    model: DixonColes, matches: pd.DataFrame, fixtures: pd.DataFrame, league: str
) -> dict:
    """One league's Teams tab: its teams, model parameters and ratings (with this
    season's xG per game where Understat covers the league), as the top-level E0 fields."""
    season = current_season()
    this = matches[matches["date"] >= f"{season}-07-01"]
    teams = sorted(
        set(this["home"]) | set(fixtures.get("home", [])) | set(fixtures.get("away", []))
    ) or sorted(model.teams)
    ratings = dashboard.team_ratings(model, dashboard.match_counts(matches, teams), teams)
    sxg = dashboard.season_xg(matches, f"{season}-07-01")
    if not sxg.empty:
        ratings = ratings.join(sxg, on="team")
    return {
        "name": lgs.name(league),
        "teams": teams,
        "params": model_params(model),
        "ratings": ratings.to_dict("records"),
        "xg": bool(matches["home_xg"].notna().any()),
    }


# Understat position codes -> the app's four groups ("F M S" = forward, sub appearances).
def _position(code: str) -> str:
    parts = (code or "").split()
    if "GK" in parts:
        return "GK"
    return {"F": "FWD", "M": "MID", "D": "DEF"}.get(parts[0] if parts else "", "")


def league_season_stats(league: str, years: list[int], raw_dir: Path = RAW_DIR) -> list[dict]:
    """Season shooting stats per player for a league other than E0, from the league-season
    file `with_xg` already downloads (Understat's `players` list): games, minutes, shots,
    goals and xG. No shots on target, starts or per-club split (a player who moved has
    one row, at his last club). Empty when Understat doesn't cover the league; never
    raises."""
    if league not in XG_LEAGUES:
        return []
    rows = []
    for year in years:
        try:
            data = json.loads(fetch_season(league, year, raw_dir).read_text())
        except Exception:  # noqa: BLE001  (a missing season leaves its rows out)
            continue
        players = data.get("players", []) if isinstance(data, dict) else []
        code = f"{year % 100:02d}{(year + 1) % 100:02d}"
        for p in players:
            team = (p.get("team_title") or "").split(",")[-1].strip()
            rows.append(
                {
                    "season": code,
                    "team": XG_TEAM_NAMES.get(team, team),
                    "player_id": str(p.get("id")),
                    "player": p.get("player_name"),
                    "position": _position(p.get("position", "")),
                    "apps": int(p.get("games") or 0),
                    "minutes": int(p.get("time") or 0),
                    "shots": int(p.get("shots") or 0),
                    "goals": int(p.get("goals") or 0),
                    "xg": round(float(p.get("xG") or 0), 2),
                    "sot": None,
                    "starts": None,
                }
            )
    return sorted(rows, key=lambda r: (r["season"], r["shots"]), reverse=True)


def league_fixtures(league: str, share: int = 1, now: pd.Timestamp | None = None) -> tuple:
    """Fixture cards and odds source for a league other than the primary one.

    Its own Dixon-Coles fit (same settings as the primary league), its schedule and its
    DraftKings odds, plus the fit's Teams-tab block (`team_block`). No blend (p_bet) and
    no team news: those exist for E0 only. Never raises: a league that fails to load
    returns no cards, no block, and says why.
    """
    season = current_season()
    try:
        matches = load_matches([league], range(season - TRAIN_SEASONS + 1, season + 1))
        matches, _ = with_xg(matches)
        fixtures = upcoming_fixtures(league, now=now)
        known = _season_teams(matches, season)
        fixtures, source = with_draftkings(fixtures, league, known, now=now, share=share)
        weight = XG_WEIGHT if matches["home_xg"].notna().any() else 0.0
        model = dashboard.fit_model(matches, xg_weight=weight)
        teams = sorted(set(fixtures.get("home", [])) | set(fixtures.get("away", [])))
        counts = dashboard.match_counts(matches, teams or sorted(model.teams))
        cards = fixture_cards(model, fixtures, counts, league=league)
    except Exception as exc:  # one league failing never stops the site
        return [], {"league": league, "name": None, "error": f"{type(exc).__name__}: {exc}"}, None
    try:  # a Teams-tab failure costs only this league's Teams block, never its cards
        block = team_block(model, matches, fixtures, league)
    except Exception as exc:  # noqa: BLE001
        print(f"{lgs.name(league)}: Teams block unavailable ({type(exc).__name__}: {exc})")
        block = None
    return cards, source, block


def league_list(fixtures: list[dict], sources: dict[str, dict]) -> list[dict]:
    """Every registry league for data.json: code, name, live flag, fixtures, odds source."""
    return [
        {
            "code": code,
            "name": lg.name,
            "live": lg.live,
            "fixtures": sum(f.get("league") == code for f in fixtures),
            "odds": (sources.get(code) or {}).get("name"),
        }
        for code, lg in lgs.LEAGUES.items()
    ]


def publish(out: Path, league: str = lgs.PRIMARY) -> Path:
    season = current_season()
    live = lgs.live_codes()
    share = max(len(live), 1)
    matches = load_matches([league], range(season - TRAIN_SEASONS + 1, season + 1))
    matches, xg_error = with_xg(matches)
    fixtures = upcoming_fixtures(league)
    known = _season_teams(matches, season)
    # The primary league budgets as if alone (share 1): the others never slow it down.
    fixtures, odds_source = with_draftkings(fixtures, league, known)

    news, news_error, snapshot, players, stats = None, None, [], None, []
    if league == "E0":  # FPL covers the Premier League only
        try:
            players = parse_players(fetch_fpl())
            this_season = matches[matches["date"] >= f"{season}-07-01"]
            games = pd.concat([this_season["home"], this_season["away"]]).value_counts().to_dict()
            news = align_team_names(team_news(players, games), sorted(set(this_season["home"])))
            snapshot = news_snapshot(players, datetime.now(UTC).isoformat(timespec="minutes"))
        except Exception as exc:  # FPL down or changed: predictions still publish
            news_error = f"Team news unavailable: {exc}"

    data = build_data(matches, fixtures, xg_error, news, news_error, league=league)
    data["odds_source"] = odds_source
    add_match_blend(data, match_blend())
    # Other live leagues: their fixtures join the list, each card tagged with its league.
    sources = {league: odds_source}
    # Teams tab per league: the primary league's block from its top-level fields.
    teams_by_league = {
        league: {
            "name": data["league"],
            "teams": data["teams"],
            "params": data["params"],
            "ratings": data["ratings"],
            "xg": bool(data["xg_weight"]),
        }
    }
    for code in live:
        if code == league:
            continue
        cards, sources[code], block = league_fixtures(code, share)
        if block is not None:
            teams_by_league[code] = _clean(block)
        data["fixtures"] = sorted(data["fixtures"] + _clean(cards), key=lambda f: f["kickoff"])
        print(f"{lgs.name(code)}: {len(cards)} fixtures, odds {sources[code].get('name')}")
    for code, src in sources.items():
        if src.get("unmatched"):  # priced matches that joined no fixture
            print(
                f"{lgs.name(code)}: {len(src['unmatched'])} odds events unmatched: "
                + "; ".join(src["unmatched"])
            )
    dups = duplicate_fixtures(data["fixtures"])
    print(f"Duplicate fixtures: {len(dups)}" + (f" {dups}" if dups else ""))
    data["odds_sources"] = _clean(sources)
    data["teams_by_league"] = teams_by_league
    data["leagues"] = league_list(data["fixtures"], sources)
    tt_dir = os.environ.get("TEAM_TOTALS_DIR")
    if tt_dir:  # set by publish.yml on the default branch only (data-log logs the rows)
        state = os.environ.get("TEAM_TOTALS_STATE")
        try:  # a team-total bug must never stop the site build; the type only (no URL/key)
            s = team_totals.run(data["fixtures"], Path(tt_dir), [state])
            print(team_totals.summary_line(s))
        except Exception as exc:  # noqa: BLE001
            print(f"Team totals: failed ({type(exc).__name__}); nothing fetched after it")
    try:  # the latest logged FanDuel prices beside the model on the match sheet (no calls)
        n_tt = add_team_totals(data["fixtures"], [os.environ.get("TEAM_TOTALS_STATE"), tt_dir])
        print(f"Team totals shown: {n_tt} fixtures with FanDuel prices")
    except Exception as exc:  # noqa: BLE001
        print(f"Team totals shown: none ({type(exc).__name__})")
    # After the paid team-total fetch, so a slow ESPN can never delay a close snapshot.
    try:  # free ESPN injuries and lineups; ESPN is unofficial, so it never stops the build
        print(espn_news.summary_line(espn_news.add(data["fixtures"])))
    except Exception as exc:  # noqa: BLE001
        print(f"Team news (ESPN): failed ({type(exc).__name__}); no team news this run")
    data["portfolio"] = portfolio_placeholder()
    if league == "E0":
        status, stats = add_players(data, league, players, odds_source["credits_left"])
        data["players_status"] = _clean(status)
        ps = data["players_status"]
        print(
            ps["error"]
            or f"Player lines: {ps.get('players', 0)} players, {ps.get('priced', 0)} priced, "
            f"{ps.get('active_players', 0)} active in current squads"
            + (
                f", {ps['unmatched_odds']} {ps.get('bookmaker')} names unmatched "
                f"(e.g. {', '.join(ps.get('unmatched_names', [])[:5])})"
                if ps.get("unmatched_odds")
                else ""
            )
            + (
                f", no history for {', '.join(ps['teams_without_history'])}"
                if ps.get("teams_without_history")
                else ""
            )
            + (
                f", {ps['missing_matches']} Understat matches still to download"
                if ps.get("missing_matches")
                else ""
            )
            + (
                ""
                if ps["gate"]["passed"]
                else " (player odds off until the model beats its baseline)"
            )
            + (f" ({ps['blend_note']})" if ps.get("blend_note") else "")
        )
    with_odds = sum(f["implied"]["home"] is not None for f in data["fixtures"])
    print(
        f"Odds: {odds_source['name']}"
        + (
            f" (credits left: {odds_source['credits_left']}, last call cost "
            f"{odds_source['last_cost']}, refreshing every ~{odds_source['refresh_hours']}h)"
            if odds_source["credits_left"] is not None
            else ""
        )
        + (f" - {odds_source['error']}" if odds_source["error"] else "")
    )
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
    if league == "E0" and stats:  # the Players view loads this on demand
        (out / "players_stats.json").write_text(
            json.dumps(
                _clean(
                    {
                        "seasons": sorted({r["season"] for r in stats}, reverse=True),
                        "players": stats,
                    }
                ),
                separators=(",", ":"),
            )
        )
        data["players_stats"] = "players_stats.json"
    # Season stats for the other Understat leagues, one file each, loaded when the Players
    # view picks that league (no extra downloads: with_xg cached these files).
    by_league_stats = {}
    for code in live:
        if code == league:
            continue
        rows = league_season_stats(code, [season - 1, season])
        if rows:
            name = f"players_stats_{code}.json"
            (out / name).write_text(
                json.dumps(
                    _clean(
                        {
                            "seasons": sorted({r["season"] for r in rows}, reverse=True),
                            "players": rows,
                        }
                    ),
                    separators=(",", ":"),
                )
            )
            by_league_stats[code] = name
            print(f"{lgs.name(code)}: season stats for {len(rows)} player-seasons ({name})")
    data["players_stats_by_league"] = by_league_stats
    (out / "data.json").write_text(json.dumps(_clean(data), separators=(",", ":")))
    # Picked up by the workflow and appended to the data-log branch (injury history).
    (out / "news_snapshot.json").write_text(json.dumps(snapshot, separators=(",", ":")))
    return out
