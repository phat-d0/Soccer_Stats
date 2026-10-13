"""The Champions League match model: one fit across Europe's leagues (owner decision,
13 Oct 2026: the Champions League goes live with both match paper strategies;
docs/leagues.md "Champions League live").

The domestic fits rate clubs only against their own league, so they can't price Arsenal
against Benfica. This module fits one goals-only Dixon-Coles model over

* the domestic matches of the leagues Champions League clubs come from: football-data's
  top divisions (`DOMESTIC_MAIN`) and its European "extra leagues" (`DOMESTIC_EXTRA`),
  each club's name prefixed with its league code ("P1:Benfica"), so two clubs that
  share a name in different countries can never merge;
* every finished European match of the three UEFA cups, qualifying rounds included, from
  ESPN's free scoreboards (cups.season_calendar; a club football-data doesn't cover keeps
  its own "UEFA:<ESPN id>" name, so its matches still link the leagues it plays).

Each league's clubs are tied to each other by their domestic matches; the European matches
set how strong one league is against another (in Dixon-Coles terms: a league's average
attack minus its average defence is fixed only by its clubs' European results). Matches
count less with age (time decay `XI`, as the domestic model), only the `WINDOW_DAYS`
before the prediction time are used, and a small ridge (`RIDGE`) keeps a club with a few
matches finite. A neutral venue (the final) gets no home advantage. Extra-time matches
count with their 90-minute score when ESPN gives it, else they are left out.

No look-ahead: every fit uses matches before its own time only (domestic by date,
European by kickoff), the walk-forward check refits weekly, and tests check both.

A club no football-data file covers (unmatched by cups.match_team) gets no model chance:
its cards show DraftKings' odds only, marked `no_model` and `low_data`, so no paper trade
opens; publish's log lists those clubs.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from soccer_stats import cups, dashboard
from soccer_stats.data import RAW_DIR, _fetch, download, load_csv, season_code
from soccer_stats.models import DixonColes

CUP = "UCL"
SLUG = cups.CUPS[CUP]
# football-data's top divisions in UEFA countries (main files, one per season) ...
DOMESTIC_MAIN = ("E0", "SP1", "D1", "I1", "F1", "SC0", "N1", "B1", "P1", "T1", "G1")
# ... and its European "extra leagues" (one all-seasons file each; Russia left out: no
# Russian club has played in Europe since 2022).
DOMESTIC_EXTRA = ("AUT", "SWZ", "DNK", "NOR", "SWE", "POL", "ROU", "FIN", "IRL")
DOMESTIC = DOMESTIC_MAIN + DOMESTIC_EXTRA
EXTRA_DIR = "fd_extra"  # under RAW_DIR
EXTRA_MAX_AGE_HOURS = 12  # the extra files hold the season in progress
UEFA = "UEFA"  # name prefix for a club no football-data file covers
WINDOW_DAYS = 730
XI = 0.0019  # time decay per day, as the domestic model
RIDGE = 0.1  # on each team rating; negligible for a club with a season of matches
MIN_MATCHES = dashboard.LOW_DATA_MATCHES  # fewer in the window = low_data
FIXTURE_DAYS = 14
# One publish run's ESPN budget for the cup (the domestic team-news feed has its own).
RUN_SECONDS = 90
RUN_FAILURES = 3
REFIT_WEEKDAY = 0  # the walk-forward refits every Monday 00:00 UTC


# ---------- domestic matches (football-data) ----------


def _naive(t) -> pd.Timestamp:
    t = pd.Timestamp(t)
    return t.tz_convert("UTC").tz_localize(None) if t.tz is not None else t


def season_of(t) -> int:
    t = pd.Timestamp(t)
    return t.year if t.month >= 7 else t.year - 1


def years_between(start, end) -> list[int]:
    """Season start years overlapping [start, end]."""
    return list(range(season_of(start), season_of(end) + 1))


def read_extra(path: Path) -> pd.DataFrame:
    """date, home, away, home_goals, away_goals from a football-data extra-league file
    (Date dd/mm/yyyy, Home, Away, HG, AG); unplayed rows dropped."""
    raw = pd.read_csv(path, encoding="latin-1", on_bad_lines="skip")
    df = pd.DataFrame(
        {
            "date": pd.to_datetime(raw.get("Date"), dayfirst=True, format="mixed", errors="coerce"),
            "home": raw.get("Home"),
            "away": raw.get("Away"),
            "home_goals": pd.to_numeric(raw.get("HG"), errors="coerce"),
            "away_goals": pd.to_numeric(raw.get("AG"), errors="coerce"),
        }
    ).dropna()
    df[["home_goals", "away_goals"]] = df[["home_goals", "away_goals"]].astype(int)
    return df


def load_domestic(
    start,
    end,
    raw_dir: Path = RAW_DIR,
    leagues=DOMESTIC,
    main_path: Callable | None = None,
    extra_path: Callable | None = None,
) -> tuple[pd.DataFrame, dict]:
    """Every played domestic match dated in [start, end] in `leagues`: league, date (naive),
    home, away (plain football-data names), home_goals, away_goals. Main divisions come
    from their season files (data.download: past seasons cached for good, the current one
    refreshed every 12 hours), extra leagues from their all-seasons file. A file that
    can't be had is skipped and named in the second return value."""
    start, end = _naive(start), _naive(end)
    main_path = main_path or (lambda code, y: download(code, y, raw_dir=raw_dir))
    extra_path = extra_path or (
        lambda code: _fetch(
            cups.FD_EXTRA_URL.format(code=code),
            Path(raw_dir) / EXTRA_DIR / f"{code}.csv",
            EXTRA_MAX_AGE_HOURS,
            stale_ok=True,
        )
    )
    frames, errors = [], {}
    for code in leagues:
        try:
            if code in DOMESTIC_EXTRA:
                df = read_extra(extra_path(code))
            else:
                parts = []
                for y in years_between(start, end):
                    try:
                        parts.append(
                            load_csv(main_path(code, y), league=code, season=season_code(y))
                        )
                    except Exception as exc:  # noqa: BLE001  a season not posted yet
                        errors[f"{code} {season_code(y)}"] = type(exc).__name__
                if not parts:
                    continue
                df = pd.concat(parts, ignore_index=True)
                df = df[["date", "home", "away", "home_goals", "away_goals"]]
        except Exception as exc:  # noqa: BLE001
            errors[code] = type(exc).__name__
            continue
        df = df[(df["date"] >= start) & (df["date"] <= end)].copy()
        df.insert(0, "league", code)
        frames.append(df)
    cols = ["league", "date", "home", "away", "home_goals", "away_goals"]
    out = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=cols)
    return out[cols].sort_values("date").reset_index(drop=True), errors


def teams_by_league(domestic: pd.DataFrame) -> dict[str, set]:
    """{league code: club names} in the loaded domestic matches (cups.match_team's input)."""
    out: dict[str, set] = {}
    for code, g in domestic.groupby("league"):
        out[code] = set(g["home"]) | set(g["away"])
    return out


# ---------- European matches (ESPN) ----------


class Budget:
    """A `get` for ESPN that stops one run's cup requests after RUN_SECONDS or
    RUN_FAILURES failed responses: it then marks the shared stats so cups._get_json makes
    no further call (cached days are still read)."""

    def __init__(
        self,
        get: Callable = requests.get,
        seconds: float = RUN_SECONDS,
        failures: int = RUN_FAILURES,
        clock: Callable = time.monotonic,
    ):
        self._get, self.seconds, self.max_failures = get, seconds, failures
        self.clock = clock
        self.start = clock()
        self.failed = 0
        self.stats: dict = {}

    def _stop(self, why: str) -> None:
        self.stats["failures"] = max(self.stats.get("failures", 0), cups.MAX_FAILURES)
        self.stats["stopped"] = True
        self.stats.setdefault("why", why)

    def __call__(self, url, params=None, timeout=None):
        if self.clock() - self.start > self.seconds:
            self._stop(f"ESPN time budget ({self.seconds:g} s) used up")
            raise requests.ConnectionError("cup ESPN budget used up")
        try:
            r = self._get(url, params=params, timeout=timeout)
        except requests.RequestException:
            self._failed()
            raise
        if getattr(r, "status_code", 200) >= 500:
            self._failed()
        return r

    def _failed(self) -> None:
        self.failed += 1
        if self.failed >= self.max_failures:
            self._stop(f"{self.failed} failed ESPN requests")


def european_events(
    years,
    raw_dir: Path = RAW_DIR,
    get: Callable = requests.get,
    today: pd.Timestamp | None = None,
    until: pd.Timestamp | None = None,
    stats: dict | None = None,
    cups_map: dict | None = None,
) -> tuple[list[dict], dict]:
    """Every ESPN event of the three UEFA cups (main and qualifying slugs) kicking off
    before `until` in the seasons given, each with `cup` and `slug`. The report says how
    many requests were made and whether the set is complete: no failed request and no
    budget stop (a 4xx, ESPN having nothing for a slug and date, counts as complete)."""
    today = today or pd.Timestamp.now(tz="UTC")
    until = cups._ts(until) if until is not None else today
    stats = {} if stats is None else stats
    cups_map = cups_map or cups.CUPS
    events: dict[str, dict] = {}
    calendars = []
    for code, slug in cups_map.items():
        for y in years:
            for s, (ws, we) in (
                (slug, cups.season_window(y)),
                (cups.QUAL[code], cups.qual_window(y)),
            ):
                if ws >= until:
                    continue
                cal = cups.season_calendar(
                    s, ws, min(we, until), raw_dir, get, today=today, stats=stats
                )
                calendars.append(
                    {"slug": s, "season": y, "mode": cal["mode"], "events": len(cal["events"])}
                )
                for e in cal["events"]:
                    events[e["id"]] = {**e, "cup": code, "slug": s}
    report = {
        "requests": stats.get("requests", 0),
        "failures": stats.get("failures", 0),
        "stopped": bool(stats.get("stopped")),
        "why": stats.get("why"),
        "calendars": calendars,
    }
    report["complete"] = not report["failures"] and not report["stopped"]
    return sorted(events.values(), key=lambda e: (e["kickoff"], e["id"])), report


def results(events: list[dict]) -> list[dict]:
    """Finished events with a 90-minute score (extra-time matches without one are left
    out: their 90-minute result is unknown)."""
    return [e for e in events if e.get("completed") and e.get("score90")]


# ---------- names ----------


def team_map(names, teams: dict[str, set]) -> dict[str, str | None]:
    """ESPN name -> "<league>:<football-data name>", or None (not covered)."""
    out = {}
    for n in sorted(set(names)):
        m = cups.match_team(n, teams)
        out[n] = f"{m['league']}:{m['name']}" if m else None
    return out


def espn_key(e: dict, side: str, mapping: dict[str, str | None]) -> str:
    """The fit's name for one side of an ESPN event: its football-data club, else its own
    UEFA name (by ESPN id, so a renamed club stays one team)."""
    return mapping.get(e[side]) or f"{UEFA}:{e.get(f'{side}_id') or e[side]}"


def fit_matches(domestic: pd.DataFrame, european: list[dict], mapping: dict) -> pd.DataFrame:
    """The fit's rows: date (naive; European = kickoff in UTC), home, away (prefixed),
    home_goals, away_goals, neutral, source ("domestic" or the cup code)."""
    dom = pd.DataFrame(
        {
            "date": pd.to_datetime(domestic["date"]),
            "home": domestic["league"] + ":" + domestic["home"].astype(str),
            "away": domestic["league"] + ":" + domestic["away"].astype(str),
            "home_goals": domestic["home_goals"].astype(int),
            "away_goals": domestic["away_goals"].astype(int),
            "neutral": 0.0,
            "source": "domestic",
        }
    )
    rows = [
        {
            "date": _naive(e["kickoff"]),
            "home": espn_key(e, "home", mapping),
            "away": espn_key(e, "away", mapping),
            "home_goals": int(e["score90"][0]),
            "away_goals": int(e["score90"][1]),
            "neutral": 1.0 if e.get("neutral") else 0.0,
            "source": e.get("cup", CUP),
        }
        for e in results(european)
    ]
    eur = pd.DataFrame(rows, columns=dom.columns)
    out = pd.concat([dom, eur], ignore_index=True) if len(eur) else dom
    return out.sort_values("date").reset_index(drop=True)


# ---------- the fit ----------


def window(matches: pd.DataFrame, as_of) -> pd.DataFrame:
    """Matches played before `as_of` (naive UTC) and within WINDOW_DAYS of it."""
    as_of = _naive(as_of)
    d = pd.to_datetime(matches["date"])
    return matches[(d < as_of) & (d >= as_of - pd.Timedelta(days=WINDOW_DAYS))]


def fit(matches: pd.DataFrame, as_of) -> DixonColes:
    """One goals-only fit on the window before `as_of`."""
    as_of = _naive(as_of)
    rows = window(matches, as_of)
    return DixonColes(xi=XI, ridge=RIDGE).fit(rows, as_of=as_of)


def match_counts(matches: pd.DataFrame) -> pd.Series:
    return pd.concat([matches["home"], matches["away"]]).value_counts()


class Named:
    """The fitted model under the cards' (ESPN) names, for publish.fixture_cards:
    `keys` maps each ESPN name to the fit's name, `neutral` lists (home, away) pairs
    played at a neutral venue."""

    def __init__(self, model: DixonColes, keys: dict[str, str], neutral=()):
        self.model, self.keys, self.neutral = model, keys, set(neutral)
        self.teams = [n for n, k in keys.items() if k in model.attack]

    def expected_goals(self, home: str, away: str) -> tuple[float, float]:
        return self.model.expected_goals(
            self.keys[home], self.keys[away], (home, away) in self.neutral
        )

    def score_matrix(self, home, away, home_mult=1.0, away_mult=1.0):
        return self.model.score_matrix(
            self.keys[home], self.keys[away], home_mult, away_mult, (home, away) in self.neutral
        )


def coverage(names, mapping: dict, counts: pd.Series) -> dict:
    """Per ESPN name: its football-data club and matches in the window, or why it has no
    model chance ("not covered": no football-data file has it)."""
    out = {}
    for n in sorted(set(names)):
        key = mapping.get(n)
        out[n] = {"key": key, "matches": int(counts.get(key, 0)) if key else 0}
    return out


# ---------- walk-forward (the evaluation and the blend's training rows) ----------


def block_start(t) -> pd.Timestamp:
    """The Monday 00:00 (naive UTC) on or before `t`: each week's refit time."""
    t = _naive(t).normalize()
    return t - pd.Timedelta(days=(t.weekday() - REFIT_WEEKDAY) % 7)


def walk_forward(
    targets: list[dict], matches: pd.DataFrame, mapping: dict, fitter: Callable = fit
) -> pd.DataFrame:
    """Model chances for each target ESPN event, from a fit at the Monday 00:00 UTC
    before its kickoff on matches played before then (weekly refits, no look-ahead).
    Rows: espn_id, date (kickoff, naive UTC), season, home, away, p_home/draw/away (NaN
    when a club isn't covered), covered, low_data, fit_at, fit_to (last match in the fit),
    home_goals/away_goals (90 minutes; NaN if unknown)."""
    rows = []
    by_week: dict[pd.Timestamp, list[dict]] = {}
    for e in targets:
        by_week.setdefault(block_start(e["kickoff"]), []).append(e)
    for lo in sorted(by_week):
        train = window(matches, lo)
        model = fitter(matches, lo) if len(train) else None
        counts = match_counts(train)
        fit_to = pd.to_datetime(train["date"]).max() if len(train) else None
        for e in by_week[lo]:
            kh, ka = mapping.get(e["home"]), mapping.get(e["away"])
            ok = model is not None and kh in (model.attack or {}) and ka in (model.attack or {})
            p = [np.nan] * 3
            if ok:
                m = model.score_matrix(kh, ka, neutral=bool(e.get("neutral")))
                p = [float(np.tril(m, -1).sum()), float(np.trace(m)), float(np.triu(m, 1).sum())]
            s90 = e.get("score90") if e.get("completed") else None
            rows.append(
                {
                    "espn_id": str(e["id"]),
                    "date": _naive(e["kickoff"]),
                    "season": season_code(season_of(e["kickoff"])),
                    "home": e["home"],
                    "away": e["away"],
                    "p_home": p[0],
                    "p_draw": p[1],
                    "p_away": p[2],
                    "covered": bool(ok),
                    "low_data": bool(
                        ok and min(counts.get(kh, 0), counts.get(ka, 0)) < MIN_MATCHES
                    ),
                    "fit_at": lo,
                    "fit_to": fit_to,
                    "home_goals": s90[0] if s90 else np.nan,
                    "away_goals": s90[1] if s90 else np.nan,
                }
            )
    return pd.DataFrame(rows)


# ---------- Pinnacle's bought closes (data-log odds_history/UCL_closes.jsonl) ----------


def load_closes(path: Path | str | None, book: str = "pinnacle") -> pd.DataFrame:
    """espn_id, close_home, close_draw, close_away for one book (decimal, ESPN's home
    and away: cup_odds wrote each row in ESPN's order). Empty without the file."""
    cols = ["espn_id", "close_home", "close_draw", "close_away"]
    if not path or not Path(path).exists():
        return pd.DataFrame(columns=cols)
    rows = []
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("book") != book:
            continue
        rows.append(
            {
                "espn_id": str(r.get("espn_id")),
                "close_home": r.get("price_home"),
                "close_draw": r.get("price_draw"),
                "close_away": r.get("price_away"),
            }
        )
    return pd.DataFrame(rows, columns=cols).drop_duplicates("espn_id")


def blend_preds(preds: pd.DataFrame, closes: pd.DataFrame) -> pd.DataFrame:
    """Walk-forward chances beside a book's close (for match_calibration.training_rows)."""
    if preds.empty:
        return preds
    return preds.merge(closes, on="espn_id", how="left")


def _ll(p: np.ndarray, y: np.ndarray) -> float:
    return round(float(-np.log(np.clip(p[np.arange(len(y)), y], 1e-12, 1)).mean()), 5)


def evaluate(
    preds: pd.DataFrame,
    pinnacle: pd.DataFrame,
    draftkings: pd.DataFrame | None = None,
    research_rows: int = 150,
) -> dict:
    """The out-of-sample check, all on matches before each fit:

    * `model_vs_pinnacle`: log loss of the model's walk-forward chances and Pinnacle's
      Shin-fair close on every match with both (per season and pooled), plus DraftKings'
      close on the matches it prices too (descriptive);
    * `blend`: match_calibration.league_fit (the live blend's own walk-forward check,
      MIN_ROWS earlier matches before any blend fit, so only the latest matches);
    * `season_split` (evaluation only, not used live): the blend fitted on the first
      season's matches (at least `research_rows`) and scored on the next season.
    """
    from soccer_stats import match_calibration as mc

    df = blend_preds(preds, pinnacle)
    close = ["close_home", "close_draw", "close_away"]
    out: dict = {
        "targets": int(len(preds)),
        "covered": int(preds["covered"].sum()) if len(preds) else 0,
        "with_pinnacle": int(df[close].notna().all(axis=1).sum()) if len(df) else 0,
        "scored": 0,
    }
    if df.empty:
        return out
    keep = df.dropna(subset=["p_home", "p_draw", "p_away", *close, "home_goals"])
    mk = mc._devig_rows(keep, close)
    keep = keep[np.isfinite(mk).all(axis=1)].reset_index(drop=True)
    out["scored"] = int(len(keep))
    if keep.empty:
        return out
    mk = mc._devig_rows(keep, close)
    md = keep[["p_home", "p_draw", "p_away"]].to_numpy(dtype=float)
    y = mc.outcome_index(keep["home_goals"], keep["away_goals"], "h2h").astype(int)
    per = {}
    for s in sorted(keep["season"].unique()):
        mask = (keep["season"] == s).to_numpy()
        per[str(s)] = {
            "matches": int(mask.sum()),
            "model": _ll(md[mask], y[mask]),
            "pinnacle": _ll(mk[mask], y[mask]),
        }
    out["model_vs_pinnacle"] = {
        "matches": int(len(y)),
        "model": _ll(md, y),
        "pinnacle": _ll(mk, y),
        "by_season": per,
    }
    if draftkings is not None and len(draftkings):
        dk = keep[["espn_id"]].merge(draftkings, on="espn_id", how="left")
        ok = dk[["close_home", "close_draw", "close_away"]].notna().all(axis=1).to_numpy()
        if ok.any():
            dkp = mc._devig_rows(dk[ok], ["close_home", "close_draw", "close_away"])
            out["model_vs_pinnacle"]["draftkings"] = {
                "matches": int(ok.sum()),
                "draftkings": _ll(dkp, y[ok]),
                "pinnacle": _ll(mk[ok], y[ok]),
                "model": _ll(md[ok], y[ok]),
            }
    first = sorted(keep["season"].unique())
    if len(first) >= 2:
        a, b = (keep["season"] == first[0]).to_numpy(), (keep["season"] == first[1]).to_numpy()
        coef = mc.fit(mk[a], md[a], y[a], min_rows=research_rows)
        if coef is not None:
            pb = mc.apply(coef, mk[b], md[b])
            out["season_split"] = {
                "fit_season": str(first[0]),
                "fit_rows": int(a.sum()),
                "score_season": str(first[1]),
                "matches": int(b.sum()),
                "coef": coef,
                "model": _ll(md[b], y[b]),
                "blend": _ll(pb, y[b]),
                "pinnacle": _ll(mk[b], y[b]),
                "min_rows": research_rows,
                "note": "evaluation only (not used live): fewer rows than the live blend's "
                f"{mc.MIN_ROWS}-match minimum",
            }
    return out


EVAL_SEASONS = (2024, 2025)  # the seasons with bought closes (cup_odds)


def blend_entry(
    closes_path: Path | str | None,
    fit_date,
    raw_dir: Path = RAW_DIR,
    get: Callable = requests.get,
    seasons=EVAL_SEASONS,
    today: pd.Timestamp | None = None,
    domestic_loader: Callable | None = None,
) -> dict:
    """fit-match-blends' Champions League entry: the model's walk-forward chances for every
    finished Champions League match of `seasons` (weekly refits on earlier matches only)
    beside Pinnacle's bought close (data-log odds_history/UCL_closes.jsonl), fitted with
    match_calibration.league_fit at `fit_date` (its MIN_ROWS guard unchanged), plus
    `evaluation` (model / blend / Pinnacle log loss, DraftKings' close beside them),
    `coverage` per season, the clubs without a model chance and this season's clubs'
    coverage (`current_clubs`). The European results must be complete, or there is no fit
    (an incomplete history would rate the leagues on part of the evidence)."""
    from soccer_stats import match_calibration as mc

    today = today or pd.Timestamp.now(tz="UTC")
    fit_date = _naive(fit_date)
    first = min(seasons)
    start = cups.season_window(first)[0] - pd.Timedelta(days=WINDOW_DAYS)
    years = list(range(season_of(start), season_of(today) + 1))
    stats: dict = {}
    euro, rep = european_events(years, raw_dir, get, today=today, until=today, stats=stats)
    out: dict = {
        "model": "cross-league goals-only Dixon-Coles (cup_model.py)",
        "xg_weight": 0.0,
        "european": {
            "matches": len(results(euro)),
            "requests": rep["requests"],
            "complete": rep["complete"],
            "why": rep["why"],
        },
    }
    if not rep["complete"]:
        return {
            **out,
            "coef": None,
            "sigma": None,
            "error": f"European results incomplete ({rep['why'] or 'failed requests'})",
        }
    loader = domestic_loader or (lambda a, b: load_domestic(a, b, raw_dir))
    domestic, errors = loader(start, today)
    out["domestic"] = {
        "matches": int(len(domestic)),
        "errors": errors,
        "leagues": sorted(domestic["league"].unique().tolist()),
    }
    names = [e[s] for e in euro for s in ("home", "away")]
    now_season = season_of(today)
    cur = cups.season_calendar(
        SLUG, *cups.season_window(now_season), raw_dir, get, today=today, stats=stats
    )
    names += [e[s] for e in cur["events"] for s in ("home", "away")]
    mapping = team_map(names, teams_by_league(domestic))
    matches = fit_matches(domestic, euro, mapping)
    targets = [
        e
        for e in results(euro)
        if e.get("slug") == SLUG and season_of(e["kickoff"]) in set(seasons)
    ]
    preds = walk_forward(targets, matches, mapping)
    pin, dk = load_closes(closes_path, "pinnacle"), load_closes(closes_path, "draftkings")
    joined = blend_preds(preds, pin)
    rows = mc.training_rows(joined, "h2h")
    fit = mc.league_fit(rows, fit_date)
    priced = joined[["close_home", "close_draw", "close_away"]].notna().all(axis=1)
    cov = [
        {
            "season": str(s),
            "matches": int(len(g)),
            "predicted": int(g["covered"].sum()),
            "pinnacle": int(priced[g.index].sum()),
            "rows": int((priced[g.index] & g["covered"]).sum()),
        }
        for s, g in joined.groupby("season")
    ]
    uncovered = sorted(
        {e[s] for e in targets for s in ("home", "away") if mapping.get(e[s]) is None}
    )
    clubs = sorted({e[s] for e in cur["events"] for s in ("home", "away")})
    out.update(
        {
            **fit,
            "fit_rows": int(len(rows)),
            "min_rows": mc.MIN_ROWS,
            "coverage": cov,
            "no_model": uncovered,
            "evaluation": evaluate(preds, pin, dk),
            "current_clubs": {
                "season": season_code(now_season),
                "clubs": {c: mapping.get(c) for c in clubs if not PLACEHOLDER.match(c)},
            },
        }
    )
    if fit.get("coef") is None:
        out["note"] = (
            f"{len(rows)} matches with a model chance and a Pinnacle close, under the "
            f"{mc.MIN_ROWS}-match minimum: no live blend, so no Blend Lean picks here"
        )
    return out


def blend_report(entry: dict) -> list[str]:
    """The Champions League entry in plain lines for the job log."""
    lines = []
    eu = entry.get("european") or {}
    lines.append(
        f"  European results: {eu.get('matches')} matches ({eu.get('requests')} ESPN requests, "
        f"{'complete' if eu.get('complete') else 'incomplete: ' + str(eu.get('why'))})"
    )
    dom = entry.get("domestic") or {}
    if dom:
        lines.append(
            f"  Domestic matches: {dom['matches']} in {len(dom['leagues'])} leagues "
            f"{dom['leagues']}" + (f"; unavailable {dom['errors']}" if dom.get("errors") else "")
        )
    for c in entry.get("coverage") or []:
        lines.append(
            f"  {c['season']}: {c['matches']} matches, {c['predicted']} with a model chance, "
            f"{c['pinnacle']} with Pinnacle's close, {c['rows']} fit rows"
        )
    if entry.get("no_model"):
        lines.append(f"  No model chance (not on football-data): {entry['no_model']}")
    ev = entry.get("evaluation") or {}
    mp = ev.get("model_vs_pinnacle")
    if mp:
        lines.append(
            f"  Walk-forward log loss, {mp['matches']} matches: model {mp['model']:.4f}, "
            f"Pinnacle close {mp['pinnacle']:.4f}"
            + "".join(
                f"; {s}: model {v['model']:.4f}, Pinnacle {v['pinnacle']:.4f} ({v['matches']})"
                for s, v in mp["by_season"].items()
            )
        )
        d = mp.get("draftkings")
        if d:
            lines.append(
                f"  Same matches with DraftKings' close ({d['matches']}): model {d['model']:.4f}, "
                f"Pinnacle {d['pinnacle']:.4f}, DraftKings {d['draftkings']:.4f}"
            )
    ss = ev.get("season_split")
    if ss:
        lines.append(
            f"  Evaluation only: blend fitted on {ss['fit_season']} ({ss['fit_rows']} rows), "
            f"scored on {ss['score_season']} ({ss['matches']}): model {ss['model']:.4f}, blend "
            f"{ss['blend']:.4f}, Pinnacle {ss['pinnacle']:.4f}; coef {ss['coef']}"
        )
    cc = (entry.get("current_clubs") or {}).get("clubs") or {}
    if cc:
        lines.append(
            f"  {entry['current_clubs']['season']} clubs: "
            + "; ".join(f"{k} -> {v or 'not covered'}" for k, v in cc.items())
        )
    if entry.get("note"):
        lines.append(f"  {entry['note']}")
    return lines


# ---------- live: fixtures, odds, cards, results ----------


def upcoming(
    now: pd.Timestamp,
    raw_dir: Path = RAW_DIR,
    get: Callable = requests.get,
    stats: dict | None = None,
    days: int = FIXTURE_DAYS,
) -> list[dict]:
    """ESPN's Champions League matches (league phase and knockouts) from two hours ago to
    `days` ahead, not yet finished."""
    start = (now - pd.Timedelta(hours=2)).normalize()
    cal = cups.season_calendar(
        SLUG, start, now + pd.Timedelta(days=days), raw_dir, get, today=now, stats=stats
    )
    return [
        e
        for e in cal["events"]
        if e["kickoff"] >= now - pd.Timedelta(hours=2)
        and not e.get("completed")
        and not (PLACEHOLDER.match(e["home"]) or PLACEHOLDER.match(e["away"]))
    ]


# A knockout tie ESPN lists before its teams are known ("TBD", "Winner Match 3").
PLACEHOLDER = re.compile(r"^\s*(tbd|tbc|winner|loser|to be (decided|determined))\b", re.I)


def pair_odds(fixtures: list[dict], odds_events: list[dict] | None) -> tuple[dict, list[str]]:
    """DraftKings' h2h per ESPN fixture id ({odds_home, odds_draw, odds_away,
    odds_updated}), paired by kickoff and names (cup_odds.pair_events, its aliases), plus
    the priced events that paired with no fixture."""
    from soccer_stats import cup_odds

    if not odds_events:
        return {}, []
    pairs = cup_odds.pair_events(fixtures, odds_events)
    out = {}
    for p in pairs:
        o = p["odds"]
        q = cup_odds.h2h_prices(o, ("draftkings",)).get("draftkings")
        if not q:
            continue
        oh, oa = o.get("home_team"), o.get("away_team")
        if p["swapped"]:
            oh, oa = oa, oh
        pr = q["outcomes"]
        out[str(p["espn"]["id"])] = {
            "odds_home": pr[oh],
            "odds_draw": pr["Draw"],
            "odds_away": pr[oa],
            "odds_updated": q.get("last_update"),
        }
    used = {id(p["odds"]) for p in pairs}
    horizon = max((cups._ts(f["kickoff"]) for f in fixtures), default=None)
    unmatched = []
    for o in odds_events:
        if id(o) in used:
            continue
        try:
            k = pd.Timestamp(o.get("commence_time"))
        except (TypeError, ValueError):
            continue
        if horizon is not None and k <= horizon + pd.Timedelta(hours=3):
            unmatched.append(f"{o.get('home_team')} v {o.get('away_team')} ({k:%Y-%m-%d %H:%M})")
    return out, unmatched


NO_MODEL_P = {k: None for k in ("home", "draw", "away", "over25", "under25", "btts")}


def no_model_card(e: dict, odds: dict | None, reason: str, implied: Callable) -> dict:
    """A card with DraftKings' odds only: no model chance (`no_model`), `low_data` so no
    paper trade opens, and the reason in plain English."""
    r = {
        "odds_home": (odds or {}).get("odds_home"),
        "odds_draw": (odds or {}).get("odds_draw"),
        "odds_away": (odds or {}).get("odds_away"),
        "odds_over25": None,
        "odds_under25": None,
    }
    return {
        "league": CUP,
        "kickoff": e["kickoff"],
        "home": e["home"],
        "away": e["away"],
        "xg": [None, None],
        "p": dict(NO_MODEL_P),
        "odds": {k[5:]: r[k] for k in r},
        "implied": implied(r),
        "odds_updated": (odds or {}).get("odds_updated"),
        "low_data": True,
        "no_model": True,
        "no_model_reason": reason,
        "news_applied": False,
        "news": None,
        "xg_mult": [1.0, 1.0],
        "p_base": None,
        "top_scores": [],
        "matrix": None,
        "goals_cdf": None,
        "total_goals_cdf": None,
    }


def cup_results(
    season: int,
    raw_dir: Path = RAW_DIR,
    get: Callable = requests.get,
    now: pd.Timestamp | None = None,
    stats: dict | None = None,
) -> pd.DataFrame:
    """The Champions League's finished matches this season for paper-trade settlement, in
    the shape paper._result reads: league, season, date, home, away (ESPN names, as the
    cards), home_goals, away_goals (after 90 minutes: an extra-time match without a
    90-minute score is left out, so its trade voids after paper.VOID_AFTER_DAYS) and
    close_* NaN (no Pinnacle close here)."""
    now = now or pd.Timestamp.now(tz="UTC")
    start, end = cups.season_window(season)
    cal = cups.season_calendar(
        SLUG, start, min(end, now + pd.Timedelta(days=1)), raw_dir, get, today=now, stats=stats
    )
    rows = [
        {
            "league": CUP,
            "season": season_code(season),
            "date": _naive(e["kickoff"]).normalize(),
            "home": e["home"],
            "away": e["away"],
            "home_goals": int(e["score90"][0]),
            "away_goals": int(e["score90"][1]),
            "close_home": np.nan,
            "close_draw": np.nan,
            "close_away": np.nan,
        }
        for e in results(cal["events"])
    ]
    return pd.DataFrame(
        rows,
        columns=[
            "league",
            "season",
            "date",
            "home",
            "away",
            "home_goals",
            "away_goals",
            "close_home",
            "close_draw",
            "close_away",
        ],
    )
