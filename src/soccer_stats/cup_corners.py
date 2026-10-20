"""Cup corners: a cross-league team-corner model for the three UEFA cups (owner, 20 Oct
2026: "build cross league models for corners so we can track UCL, UEL, and Conference
League"; pre-registered in docs/totals.md, "Cup corners model").

Model (f) (corners_live.TeamCornersF) rates each club only against its own league. A cup
match pits clubs from two leagues, so this module puts every club on one scale:

* **Domestic ratios.** Model (f), unchanged, on each football-data main division with
  corner counts (HC/AC), at the fit time, on earlier matches only. A club is keyed by
  country and football-data name ("Portugal:Benfica"); its league at a fit is the league
  of its latest domestic match with corner counts before then (so a promoted club keeps
  one identity).
* **Cup scales.** Mean corners = s(cup, side) x F(club taking them) x A(club conceding
  them), with s one Poisson scale per cup and side, fitted on the cup's earlier matches.
* **(j) League offsets.** A corners-for and a corners-against offset per domestic league,
  Normal(0, PRIOR_SD^2) prior, fitted by maximum a posteriori on earlier cup matches: the
  cup matches tie the leagues together.
* **(k) Cup-only ratings.** A club without domestic corner data (Austria, Czechia, ...)
  rated from its own earlier cup matches (at least MIN_CUP_ONLY), empirical-Bayes shrunk.
* **No rating, no chance.** A match with an unrated club gets no chance (`card_corners`
  returns None, so a live card would carry no `corners` block and no corner trade opens).

Cup matches and corner counts come from ESPN's free scoreboards and match summaries
(`wonCorners`); each summary's counts are cached for good once the match is final, as a
few bytes under `data/raw/cups/corners/`. 0 credits, no Odds API key. The evaluation
(`evaluate`, CLI `soccer-stats cup-corners`) is the pre-registered weekly walk-forward on
2024/25 and 2025/26 against the cup average; 2026/27 corner counts are never scored.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from soccer_stats import cup_model, cups
from soccer_stats.corners_live import CDF_MAX, KMAX, TeamCornersF
from soccer_stats.data import RAW_DIR, season_code

CUPS = cups.CUPS  # code -> ESPN main slug
CORNER_LEAGUES = cups.FD_MAIN  # football-data main divisions (HC/AC where the file has them)
EXTRA_LEAGUES = cup_model.DOMESTIC_EXTRA  # all-season files: names only (no HC/AC expected)
COUNTRY = {**cups.COUNTRY, "EC": "England"}
UEFA = "UEFA"  # key prefix for a club no football-data file has
WINDOW_DAYS = 730  # the cup window (domestic windows: TeamCornersF's own 730 days)
MIN_CUP_ROWS = 50  # a cup's own scale/average needs this many earlier rows, else pooled
MIN_OFFSET_ROWS = 200  # (j): earlier cup rows with both clubs rated, else (i)'s chances
PRIOR_SD = 0.2  # (j): league offsets' prior sd on the log scale
MIN_CUP_ONLY = 4  # (k): earlier cup matches with corner counts for a cup-only rating
MIN_JUDGED = 120  # fewer scored matches in a group: reported, not judged
SLOPE_BAND = (0.80, 1.25)
LINES = [
    (side, part, line) for side, part in (("home", 0), ("away", 1)) for line in (3.5, 4.5, 5.5)
]
CANDS = ("a", "i", "j", "k")
FAMILY = ("i", "j", "k")
GROUPS = ("UCL", "UEL", "UECL", "all")
TESTS = len(FAMILY) * len(GROUPS)  # 12: 99.583%
NAMES = {
    "a": "cup average (baseline)",
    "i": "cross-league (f), no league offsets",
    "j": "(i) + league offsets",
    "k": "(j) + cup-only ratings (coverage extension)",
}
SET_OF = {"i": "D", "j": "D", "k": "X"}
HISTORY_SEASONS = (2022, 2023)  # windows only
SCORED_SEASONS = (2024, 2025)
SCORE_START = pd.Timestamp("2024-06-01")  # naive UTC
SCORE_END = pd.Timestamp("2026-07-01")  # 2026/27 cup corner counts are never scored
DOMESTIC_SEASONS = (2022, 2023, 2024, 2025, 2026)
CURRENT = 2026  # this season (the league-phase count and the cost estimate)
LEAGUE_PHASE = (pd.Timestamp("2026-09-01", tz="UTC"), pd.Timestamp("2027-02-01", tz="UTC"))
CACHE = "cups/corners"
WORKERS = 6
TIMEOUT = 15
RETRIES = 2
MAX_FAILURES = 60  # failed summaries (after retries) before a run stops asking ESPN


def _naive(t) -> pd.Timestamp:
    return cup_model._naive(t)


def season_of(t) -> int:
    """A cup season's start year: qualifying begins in late June, the final is in May."""
    t = pd.Timestamp(t)
    return t.year if t.month >= 6 else t.year - 1


# ---------- domestic corners (football-data) ----------


def _played(raw: pd.DataFrame) -> pd.DataFrame:
    need = {"HomeTeam", "AwayTeam", "FTHG", "FTAG"}
    if not need <= set(raw):
        return raw.iloc[0:0]
    return raw.dropna(subset=list(need))


def load_domestic(
    seasons=DOMESTIC_SEASONS,
    raw_dir: Path = RAW_DIR,
    main_path: Callable | None = None,
    extra_path: Callable | None = None,
    leagues=CORNER_LEAGUES,
    extras=EXTRA_LEAGUES,
) -> dict:
    """Domestic matches with corner counts, club names for the mapping, and the coverage
    count. Returns {matches: DataFrame(league, country, date, home, away, home_corners,
    away_corners), teams: {code: names}, coverage: [{league, season, played, with_corners,
    columns}], errors: {...}}. A file that can't be had is skipped and named in errors."""
    from soccer_stats.data import _fetch, download

    main_path = main_path or (lambda code, y: download(code, y, raw_dir=raw_dir))
    extra_path = extra_path or (
        lambda code: _fetch(
            cups.FD_EXTRA_URL.format(code=code),
            Path(raw_dir) / cup_model.EXTRA_DIR / f"{code}.csv",
            cup_model.EXTRA_MAX_AGE_HOURS,
            stale_ok=True,
        )
    )
    frames, teams, cov, errors = [], {}, [], {}
    for code in leagues:
        for y in seasons:
            try:
                raw = pd.read_csv(main_path(code, y), encoding="latin-1", on_bad_lines="skip")
            except Exception as exc:  # noqa: BLE001  a season not posted (yet) or unreachable
                errors[f"{code} {season_code(y)}"] = type(exc).__name__
                continue
            played = _played(raw)
            teams.setdefault(code, set()).update(set(played["HomeTeam"]) | set(played["AwayTeam"]))
            has = {"HC", "AC"} <= set(raw)
            df = pd.DataFrame(
                {
                    "league": code,
                    "country": COUNTRY.get(code, code),
                    "date": pd.to_datetime(
                        played["Date"], dayfirst=True, format="mixed", errors="coerce"
                    )
                    if "Date" in played
                    else pd.NaT,
                    "home": played["HomeTeam"],
                    "away": played["AwayTeam"],
                    "home_corners": pd.to_numeric(played["HC"], errors="coerce") if has else np.nan,
                    "away_corners": pd.to_numeric(played["AC"], errors="coerce") if has else np.nan,
                }
            )
            df = df.dropna(subset=["date", "home", "away", "home_corners", "away_corners"])
            cov.append(
                {
                    "league": code,
                    "season": season_code(y),
                    "played": int(len(played)),
                    "with_corners": int(len(df)),
                    "columns": has,
                }
            )
            frames.append(df)
    start = pd.Timestamp(f"{min(seasons)}-07-01")
    for code in extras:
        try:
            raw = pd.read_csv(extra_path(code), encoding="latin-1", on_bad_lines="skip")
        except Exception as exc:  # noqa: BLE001
            errors[code] = type(exc).__name__
            continue
        date = pd.to_datetime(raw.get("Date"), dayfirst=True, format="mixed", errors="coerce")
        keep = raw[date >= start]
        names = set(keep.get("Home", pd.Series(dtype=str)).dropna())
        names |= set(keep.get("Away", pd.Series(dtype=str)).dropna())
        teams[code] = names
        has = {"HC", "AC"} <= set(raw)
        n = int(keep.dropna(subset=[c for c in ("Home", "Away", "HG", "AG") if c in keep]).shape[0])
        k = 0
        if has:
            k = int(
                keep[["HC", "AC"]].apply(pd.to_numeric, errors="coerce").notna().all(axis=1).sum()
            )
        cov.append(
            {"league": code, "season": "all", "played": n, "with_corners": k, "columns": has}
        )
    cols = ["league", "country", "date", "home", "away", "home_corners", "away_corners"]
    out = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=cols)
    out = out[cols].sort_values("date", kind="stable").reset_index(drop=True)
    return {"matches": out, "teams": teams, "coverage": cov, "errors": errors}


# ---------- club names ----------


def club_map(names, teams: dict[str, set]) -> dict[str, dict | None]:
    """ESPN name -> {key: "<country>:<football-data name>", league, name, how} or None."""
    out = {}
    for n in sorted(set(names)):
        m = cups.match_team(n, teams)
        if m:
            country = COUNTRY.get(m["league"], m["league"])
            out[n] = {**m, "key": f"{country}:{m['name']}", "country": country}
        else:
            out[n] = None
    return out


def side_key(e: dict, side: str, mapping: dict) -> str:
    """A side's club key: its football-data club, else its own UEFA key (by ESPN id)."""
    m = mapping.get(e[side])
    return m["key"] if m else f"{UEFA}:{e.get(f'{side}_id') or e[side]}"


# ---------- ESPN corner counts ----------


def _cache_path(raw_dir: Path, slug: str, event_id: str) -> Path:
    return Path(raw_dir) / CACHE / f"{slug}_{event_id}.json"


def fetch_corners(
    events: list[dict],
    raw_dir: Path = RAW_DIR,
    get: Callable = requests.get,
    today: pd.Timestamp | None = None,
    workers: int = WORKERS,
) -> tuple[dict[str, tuple[int, int] | None], dict]:
    """(home, away) corner counts per finished event id (None when the summary has none),
    from ESPN's match summary (espn_results.summary_corners). A final match (kicked off
    over a day ago) is cached for good, counts or not; nothing else is cached. Returns the
    counts and {requests, failures, cached, stopped}."""
    from soccer_stats.espn_results import summary_corners

    today = cups._ts(today) if today is not None else pd.Timestamp.now(tz="UTC")
    done = [e for e in events if e.get("completed")]
    out: dict[str, tuple[int, int] | None] = {}
    todo = []
    stats = {"requests": 0, "failures": 0, "cached": 0, "stopped": False}
    for e in done:
        path = _cache_path(raw_dir, e.get("slug", "x"), e["id"])
        if path.exists():
            try:
                c = json.loads(path.read_text()).get("corners")
                out[e["id"]] = tuple(c) if c else None
                stats["cached"] += 1
                continue
            except (OSError, ValueError):
                pass
        todo.append(e)
    lock = threading.Lock()

    def one(e: dict) -> None:
        slug = e.get("slug") or "x"
        body = None
        for attempt in range(RETRIES + 1):
            with lock:
                if stats["failures"] >= MAX_FAILURES:
                    stats["stopped"] = True
                    return
                stats["requests"] += 1
            try:
                r = get(
                    cups.ESPN.format(slug=slug) + "/summary",
                    params={"event": e["id"]},
                    timeout=TIMEOUT,
                )
                if r.status_code == 200:
                    body = r.json()
                    break
                if r.status_code < 500:
                    break
            except (requests.RequestException, ValueError):
                pass
            if attempt < RETRIES:
                time.sleep(0.2 * (attempt + 1))
        else:
            with lock:
                stats["failures"] += 1
            return
        if body is None:
            with lock:
                stats["failures"] += 1
            return
        c = summary_corners(body, e)
        out[e["id"]] = c
        if cups._ts(e["kickoff"]) < today - pd.Timedelta(days=1):
            path = _cache_path(raw_dir, slug, e["id"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"id": e["id"], "corners": list(c) if c else None}))

    if todo:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            list(pool.map(one, todo))
    return out, stats


def cup_frame(events: list[dict], corners: dict, mapping: dict) -> pd.DataFrame:
    """One row per finished cup event: espn_id, cup, slug, kickoff (naive UTC), season,
    home, away (ESPN names), home_key, away_key, neutral, home_corners, away_corners (NaN
    without counts)."""
    rows = []
    for e in events:
        if not e.get("completed"):
            continue
        c = corners.get(e["id"])
        rows.append(
            {
                "espn_id": str(e["id"]),
                "cup": e.get("cup"),
                "slug": e.get("slug"),
                "kickoff": _naive(e["kickoff"]),
                "season": season_of(_naive(e["kickoff"])),
                "home": e["home"],
                "away": e["away"],
                "home_key": side_key(e, "home", mapping),
                "away_key": side_key(e, "away", mapping),
                "neutral": bool(e.get("neutral")),
                "home_corners": float(c[0]) if c else np.nan,
                "away_corners": float(c[1]) if c else np.nan,
            }
        )
    cols = [
        "espn_id",
        "cup",
        "slug",
        "kickoff",
        "season",
        "home",
        "away",
        "home_key",
        "away_key",
        "neutral",
        "home_corners",
        "away_corners",
    ]
    return pd.DataFrame(rows, columns=cols).sort_values("kickoff", kind="stable")


# ---------- the model ----------


def _mom_alpha(y, m) -> float | None:
    from soccer_stats.edge.corners import mom_alpha

    y, m = np.asarray(y, float), np.asarray(m, float)
    return mom_alpha(y, m) if len(y) else None


class CupCorners:
    """Every candidate fitted at one time `as_of` on earlier matches only. `means(df)`
    gives per candidate (home mean, away mean) arrays, NaN where a club is unrated."""

    def fit(self, domestic: pd.DataFrame, cup: pd.DataFrame, as_of):
        from soccer_stats.edge.corners2 import eb_shrink

        T = _naive(as_of)
        self.as_of = T
        dom = domestic[pd.to_datetime(domestic["date"]) < T]
        # Model (f) per domestic league, and each club's league (its latest match).
        self.leagues: dict[str, TeamCornersF] = {}
        for code, g in dom.groupby("league", sort=False):
            try:
                self.leagues[code] = TeamCornersF().fit(g, now=T)
            except ValueError:
                continue
        long = pd.concat(
            [
                pd.DataFrame(
                    {
                        "key": dom["country"] + ":" + dom[s].astype(str),
                        "name": dom[s],
                        "date": dom["date"],
                        "league": dom["league"],
                    }
                )
                for s in ("home", "away")
            ]
        ).sort_values("date", kind="stable")
        last = long.groupby("key").tail(1)
        self.club: dict[str, tuple[str, float, float]] = {}  # key -> (league, F, A)
        for key, name, lg in zip(last["key"], last["name"], last["league"], strict=True):
            m = self.leagues.get(lg)
            if m is not None and name in m.for_:
                self.club[key] = (lg, float(m.for_[name]), float(m.against_[name]))
        # Earlier cup matches in the window with corner counts.
        c = cup[
            (cup["kickoff"] < T)
            & (cup["kickoff"] >= T - pd.Timedelta(days=WINDOW_DAYS))
            & cup["home_corners"].notna()
            & cup["away_corners"].notna()
        ].reset_index(drop=True)
        self.n_cup = int(len(c))
        plain = c[~c["neutral"]]
        # (a) the cup average per side, pooled when a cup has too few rows.
        self.base_pool = (plain["home_corners"].mean(), plain["away_corners"].mean())
        self.base = {}
        for code, g in plain.groupby("cup"):
            if len(g) >= MIN_CUP_ROWS:
                self.base[code] = (g["home_corners"].mean(), g["away_corners"].mean())
        bh, ba = self._base_means(c)
        self.alpha = {
            "a": (_mom_alpha(c["home_corners"], bh), _mom_alpha(c["away_corners"], ba))
            if len(c)
            else (None, None)
        }
        # (i) and (j) on rows with both clubs rated domestically.
        known = set(self.club)
        rated = c["home_key"].isin(known) & c["away_key"].isin(known)
        d = c[rated].reset_index(drop=True)
        self.n_rated = int(len(d))
        self._fit_scales(d)
        self._fit_offsets(d)
        self._fit_cup_only(c, eb_shrink)
        return self

    # ----- helpers -----

    def _base_means(self, df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        h = np.array([self.base.get(x, self.base_pool)[0] for x in df["cup"]], float)
        a = np.array([self.base.get(x, self.base_pool)[1] for x in df["cup"]], float)
        neutral = df["neutral"].to_numpy(bool)
        mid = (h + a) / 2
        return np.where(neutral, mid, h), np.where(neutral, mid, a)

    def _terms(self, keys, which: str) -> np.ndarray:
        """Domestic F (which="F") or A per club key; NaN when unrated."""
        i = 1 if which == "F" else 2
        return np.array([self.club[k][i] if k in self.club else np.nan for k in keys], float)

    def _league(self, keys) -> list:
        return [self.club[k][0] if k in self.club else None for k in keys]

    def _scale(self, table: dict, pool, cups_, neutral) -> tuple[np.ndarray, np.ndarray]:
        h = np.array([table.get(x, pool)[0] for x in cups_], float)
        a = np.array([table.get(x, pool)[1] for x in cups_], float)
        neutral = np.asarray(neutral, bool)
        mid = (h + a) / 2
        return np.where(neutral, mid, h), np.where(neutral, mid, a)

    def _fit_scales(self, d: pd.DataFrame) -> None:
        """(i): s(cup, side) = sum corners / sum F x A, one Poisson scale per cup and side."""
        self.ok_i = len(d) >= MIN_CUP_ROWS
        self.s_pool, self.s_cup = (np.nan, np.nan), {}
        self.alpha["i"] = (None, None)
        if not self.ok_i:
            return
        p = d[~d["neutral"]]
        fh = self._terms(p["home_key"], "F") * self._terms(p["away_key"], "A")
        fa = self._terms(p["away_key"], "F") * self._terms(p["home_key"], "A")
        yh, ya = p["home_corners"].to_numpy(float), p["away_corners"].to_numpy(float)
        self.s_pool = (yh.sum() / fh.sum(), ya.sum() / fa.sum())
        for code in p["cup"].unique():
            m = (p["cup"] == code).to_numpy()
            if m.sum() >= MIN_CUP_ROWS:
                self.s_cup[code] = (yh[m].sum() / fh[m].sum(), ya[m].sum() / fa[m].sum())
        mh, ma = self._means_i(d)
        self.alpha["i"] = (
            _mom_alpha(d["home_corners"], mh),
            _mom_alpha(d["away_corners"], ma),
        )

    def _means_i(self, df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        sh, sa = self._scale(self.s_cup, self.s_pool, df["cup"], df["neutral"])
        hF, hA = self._terms(df["home_key"], "F"), self._terms(df["home_key"], "A")
        aF, aA = self._terms(df["away_key"], "F"), self._terms(df["away_key"], "A")
        return sh * hF * aA, sa * aF * hA

    def _fit_offsets(self, d: pd.DataFrame) -> None:
        """(j): league for/against offsets (Normal(0, PRIOR_SD^2)) and the cup scales by
        maximum a posteriori on the earlier cup rows with both clubs rated."""
        self.ok_j = self.ok_i and len(d) >= MIN_OFFSET_ROWS
        self.o, self.dd = {}, {}
        self.b_cup, self.b_pool = {}, None
        self.alpha["j"] = self.alpha["i"]
        if not self.ok_j:
            return
        p = d[~d["neutral"]].reset_index(drop=True)
        n = len(p)
        groups = [x if x in self.s_cup else "pooled" for x in p["cup"]]
        gnames = sorted(set(groups))
        gidx = np.array([gnames.index(x) for x in groups])
        lh, la = self._league(p["home_key"]), self._league(p["away_key"])
        lnames = sorted(set(lh) | set(la))
        li = {x: i for i, x in enumerate(lnames)}
        nl, ng = len(lnames), len(gnames)
        # Two observations per match: home corners (taker = home), away corners.
        y = np.r_[p["home_corners"].to_numpy(float), p["away_corners"].to_numpy(float)]
        off = np.log(
            np.r_[
                self._terms(p["home_key"], "F") * self._terms(p["away_key"], "A"),
                self._terms(p["away_key"], "F") * self._terms(p["home_key"], "A"),
            ]
        )
        intercept = np.r_[gidx * 2, gidx * 2 + 1]  # (group, side)
        taker = np.r_[[li[x] for x in lh], [li[x] for x in la]]
        giver = np.r_[[li[x] for x in la], [li[x] for x in lh]]
        prec = 1.0 / PRIOR_SD**2
        n_int = 2 * ng
        npar = n_int + 2 * nl
        X = np.zeros((len(y), npar))
        rows_ = np.arange(len(y))
        X[rows_, intercept] = 1.0
        X[rows_, n_int + taker] = 1.0
        X[rows_, n_int + nl + giver] = 1.0
        pen = np.r_[np.zeros(n_int), np.full(2 * nl, prec)]

        def objective(theta):
            eta = X @ theta + off
            return float((np.exp(eta) - y * eta).sum() + 0.5 * (pen * theta**2).sum())

        theta = np.zeros(npar)
        for gi, gname in enumerate(gnames):
            s = self.s_cup.get(gname, self.s_pool)
            theta[2 * gi], theta[2 * gi + 1] = np.log(s[0]), np.log(s[1])
        # Newton-Raphson on the log posterior (concave), with step halving.
        val = objective(theta)
        for _ in range(100):
            mu = np.exp(X @ theta + off)
            grad = X.T @ (mu - y) + pen * theta
            hess = (X * mu[:, None]).T @ X + np.diag(pen) + 1e-9 * np.eye(npar)
            step = np.linalg.solve(hess, grad)
            t = 1.0
            while t > 1e-6:
                cand = theta - t * step
                new_val = objective(cand)
                if new_val <= val + 1e-12:
                    break
                t /= 2
            theta, done = cand, abs(val - new_val) < 1e-10
            val = new_val
            if done or np.abs(t * step).max() < 1e-9:
                break
        for gi, gname in enumerate(gnames):
            pair = (float(np.exp(theta[2 * gi])), float(np.exp(theta[2 * gi + 1])))
            if gname == "pooled":
                self.b_pool = pair
            else:
                self.b_cup[gname] = pair
        self.o = {x: float(theta[n_int + i]) for x, i in li.items()}
        self.dd = {x: float(theta[n_int + nl + i]) for x, i in li.items()}
        self.n_offset_rows = n
        mh, ma = self._means_j(d)
        self.alpha["j"] = (
            _mom_alpha(d["home_corners"], mh),
            _mom_alpha(d["away_corners"], ma),
        )

    def _j_scales(self, cups_, neutral):
        pool = self.b_pool or self.s_pool
        return self._scale(self.b_cup, pool, cups_, neutral)

    def _j_terms(self, keys) -> tuple[np.ndarray, np.ndarray]:
        """(j)'s for and against terms per club: ratio x exp(league offset); NaN unrated."""
        lg = self._league(keys)
        F, A = self._terms(keys, "F"), self._terms(keys, "A")
        o = np.array([self.o.get(x, 0.0) if x else np.nan for x in lg], float)
        d = np.array([self.dd.get(x, 0.0) if x else np.nan for x in lg], float)
        return F * np.exp(o), A * np.exp(d)

    def _means_j(self, df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        sh, sa = self._j_scales(df["cup"], df["neutral"])
        hF, hA = self._j_terms(df["home_key"])
        aF, aA = self._j_terms(df["away_key"])
        return sh * hF * aA, sa * aF * hA

    def _fit_cup_only(self, c: pd.DataFrame, eb_shrink) -> None:
        """(k): clubs without a domestic rating, from their earlier cup matches: per match
        r = corners / (scale x the opponent's (j) term, 1 when unrated), shrunk with k from
        eb_shrink over all such rows; at least MIN_CUP_ONLY matches."""
        self.cup_only: dict[str, tuple[float, float, int]] = {}
        self.ok_k = self.ok_i
        if not self.ok_k or c.empty:
            return
        if self.ok_j:
            sh, sa = self._j_scales(c["cup"], c["neutral"])
            hF, hA = self._j_terms(c["home_key"])
            aF, aA = self._j_terms(c["away_key"])
        else:
            sh, sa = self._scale(self.s_cup, self.s_pool, c["cup"], c["neutral"])
            hF, hA = self._terms(c["home_key"], "F"), self._terms(c["home_key"], "A")
            aF, aA = self._terms(c["away_key"], "F"), self._terms(c["away_key"], "A")
        one = lambda x: np.where(np.isfinite(x), x, 1.0)  # noqa: E731
        yh, ya = c["home_corners"].to_numpy(float), c["away_corners"].to_numpy(float)
        rows = []
        for _side, key, y_for, y_ag, s_for, s_ag, opp_a, opp_f in (
            ("home", c["home_key"], yh, ya, sh, sa, one(aA), one(aF)),
            ("away", c["away_key"], ya, yh, sa, sh, one(hA), one(hF)),
        ):
            keep = ~key.isin(set(self.club)).to_numpy()
            rows.append(
                pd.DataFrame(
                    {
                        "key": key.to_numpy()[keep],
                        "f": (y_for / (s_for * opp_a))[keep],
                        "a": (y_ag / (s_ag * opp_f))[keep],
                    }
                )
            )
        r = pd.concat(rows, ignore_index=True)
        r = r[np.isfinite(r["f"]) & np.isfinite(r["a"])]
        if r["key"].nunique() < 2:
            return
        kf, ka = eb_shrink(r["key"], r["f"].to_numpy()), eb_shrink(r["key"], r["a"].to_numpy())
        g = r.groupby("key")
        n = g.size()
        F = (g["f"].sum() + kf) / (n + kf)
        A = (g["a"].sum() + ka) / (n + ka)
        self.k_cup_only = (float(kf), float(ka))
        for key in n.index:
            if n[key] >= MIN_CUP_ONLY:
                self.cup_only[key] = (float(F[key]), float(A[key]), int(n[key]))

    def _k_terms(self, keys) -> tuple[np.ndarray, np.ndarray]:
        if self.ok_j:
            F, A = self._j_terms(keys)
        else:
            F, A = self._terms(keys, "F"), self._terms(keys, "A")
        for i, k in enumerate(keys):
            if k in self.cup_only:
                F[i], A[i] = self.cup_only[k][0], self.cup_only[k][1]
        return F, A

    def _means_k(self, df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        if self.ok_j:
            sh, sa = self._j_scales(df["cup"], df["neutral"])
        else:
            sh, sa = self._scale(self.s_cup, self.s_pool, df["cup"], df["neutral"])
        hF, hA = self._k_terms(list(df["home_key"]))
        aF, aA = self._k_terms(list(df["away_key"]))
        return sh * hF * aA, sa * aF * hA

    # ----- predictions -----

    def rated(self, key: str) -> str | None:
        """ "domestic", "cup_only" or None (no chance)."""
        if key in self.club:
            return "domestic"
        if key in self.cup_only:
            return "cup_only"
        return None

    def means(self, df: pd.DataFrame) -> dict[str, tuple[np.ndarray, np.ndarray]]:
        nan = np.full(len(df), np.nan)
        out = {"a": self._base_means(df) if self.n_cup else (nan, nan)}
        out["i"] = self._means_i(df) if self.ok_i else (nan, nan)
        if self.ok_j:
            out["j"] = self._means_j(df)
        else:
            out["j"] = out["i"]
        out["k"] = self._means_k(df) if self.ok_k else (nan, nan)
        return out

    def pmfs(self, df: pd.DataFrame, cand: str) -> tuple[np.ndarray, np.ndarray]:
        """NB2 pmfs (0..KMAX) per side for one candidate; NaN rows where unrated."""
        from soccer_stats.models.player_counts import nb_pmf

        alpha = self.alpha["j"] if cand == "k" else self.alpha[cand]
        lh, la = self.means(df)[cand]
        out = []
        for lam, al in ((lh, alpha[0]), (la, alpha[1])):
            p = np.full((len(df), KMAX + 1), np.nan)
            ok = np.isfinite(lam) & (lam > 0)
            if ok.any() and al is not None:
                p[ok] = nb_pmf(lam[ok], al, KMAX)
            out.append(p)
        return out[0], out[1]

    def info(self) -> dict:
        return {
            "as_of": self.as_of.isoformat(),
            "leagues": sorted(self.leagues),
            "domestic_clubs": len(self.club),
            "cup_rows": self.n_cup,
            "rated_rows": self.n_rated,
            "offsets": self.ok_j,
            "cup_only_clubs": len(self.cup_only),
        }


# ---------- live card shape (corners_live.add_model's) ----------


def _cdf(pmf: np.ndarray) -> list[float]:
    return [round(float(x), 5) for x in np.cumsum(pmf)[: CDF_MAX + 1]]


def card_corners(model: CupCorners, row: dict, cand: str = "j") -> dict | None:
    """`corners = {model, home: {mean, cdf}, away: {mean, cdf}}` for one cup match, the
    shape corners_live.add_model gives domestic cards, or None when a club is unrated
    under this candidate (no chance, so no corner trade)."""
    df = pd.DataFrame([{"neutral": False, **row}])
    lh, la = model.means(df)[cand]
    if not (np.isfinite(lh[0]) and np.isfinite(la[0])):
        return None
    ph, pa = model.pmfs(df, cand)
    if not (np.isfinite(ph).all() and np.isfinite(pa).all()):
        return None
    return {
        "model": f"cup_{cand}",
        "home": {"mean": round(float(lh[0]), 3), "cdf": _cdf(ph[0])},
        "away": {"mean": round(float(la[0]), 3), "cdf": _cdf(pa[0])},
    }


def add_model(
    cards: list[dict], model: CupCorners, mapping: dict, cand: str = "j", cups_=tuple(CUPS)
) -> dict:
    """Each cup card whose clubs are rated gets `corners`; the rest get none. Returns
    {cards, with_model, unrated: [club names]}."""
    out = {"cards": 0, "with_model": 0, "unrated": []}
    for c in cards:
        if c.get("league") not in cups_ or not c.get("home") or not c.get("away"):
            continue
        out["cards"] += 1
        row = {
            "cup": c["league"],
            "neutral": bool(c.get("neutral")),
            "home_key": side_key({**c, "home_id": c.get("home_id")}, "home", mapping),
            "away_key": side_key({**c, "away_id": c.get("away_id")}, "away", mapping),
        }
        got = card_corners(model, row, cand)
        if got is None:
            for s in ("home", "away"):
                ok = model.rated(row[f"{s}_key"])
                if ok is None or (ok == "cup_only" and cand != "k"):
                    out["unrated"].append(c[s])
            continue
        c["corners"] = got
        out["with_model"] += 1
    out["unrated"] = sorted(set(out["unrated"]))
    return out


# ---------- walk-forward and scoring ----------


def walk_forward(
    domestic: pd.DataFrame,
    cup: pd.DataFrame,
    start=SCORE_START,
    end=SCORE_END,
    seasons=SCORED_SEASONS,
) -> pd.DataFrame:
    """Chances for every scored cup match (both corner counts known, kickoff in
    [start, end), a season in `seasons`: late-June 2026 qualifiers are 2026/27's), from a
    fit at the Monday 00:00 UTC before its kickoff on matches before then. Rows: the
    match, `set` ("D": both clubs rated domestically; "X": (k) rates it with a cup-only
    club; None: no chance), and p_<cand>_<side>_<line> per candidate."""
    from soccer_stats.edge.totals import over_from_pmf

    t = cup[
        (cup["kickoff"] >= start)
        & (cup["kickoff"] < end)
        & cup["home_corners"].notna()
        & cup["away_corners"].notna()
        & cup["season"].isin(list(seasons))
    ].copy()
    t["fit_at"] = [cup_model.block_start(k) for k in t["kickoff"]]
    parts = []
    for lo, g in t.groupby("fit_at", sort=True):
        m = CupCorners().fit(domestic, cup, lo)
        g = g.copy()
        rh = [m.rated(k) for k in g["home_key"]]
        ra = [m.rated(k) for k in g["away_key"]]
        sets = []
        for a, b in zip(rh, ra, strict=True):
            if a == "domestic" and b == "domestic":
                sets.append("D")
            elif a and b:
                sets.append("X")
            else:
                sets.append(None)
        g["set"] = sets
        g["home_rating"], g["away_rating"] = rh, ra
        for cand in CANDS:
            ph, pa = m.pmfs(g, cand)
            for side, part, line in LINES:
                pm = ph if part == 0 else pa
                g[f"p_{cand}_{side}_{line}"] = over_from_pmf(pm, line)
        parts.append(g)
    if not parts:
        return t.assign(set=None)
    return pd.concat(parts).sort_values("kickoff", kind="stable").reset_index(drop=True)


def long_rows(pred: pd.DataFrame, cand: str) -> pd.DataFrame:
    """corner_recal.score's rows for one candidate on its set: one per match and line."""
    d = pred[pred["set"] == SET_OF[cand]].reset_index(drop=True)
    d = d[np.isfinite(d[[f"p_{cand}_{s}_{ln}" for s, _, ln in LINES]]).all(axis=1)]
    d = d[np.isfinite(d[[f"p_a_{s}_{ln}" for s, _, ln in LINES]]).all(axis=1)]
    d = d.reset_index(drop=True)
    counts = {0: d["home_corners"].to_numpy(float), 1: d["away_corners"].to_numpy(float)}
    out = []
    for side, part, line in LINES:
        out.append(
            pd.DataFrame(
                {
                    "line": f"{side}_{line}",
                    "p_raw": d[f"p_{cand}_{side}_{line}"].to_numpy(float),
                    "p_base": d[f"p_a_{side}_{line}"].to_numpy(float),
                    "y": (counts[part] > line).astype(int),
                    "match": d["espn_id"].to_numpy(),
                    "season": d["season"].to_numpy(int),
                    "cup": d["cup"].to_numpy(),
                    "row": np.arange(len(d)),
                }
            )
        )
    cols = ["line", "p_raw", "p_base", "y", "match", "season", "cup", "row"]
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame(columns=cols)


def score(pred: pd.DataFrame, level: float) -> dict:
    """Each family candidate in each group (UCL, UEL, UECL, all) through
    corner_recal.score: gain over (a) with its range at `level`, the slopes, pass and
    whether the group is judged (at least MIN_JUDGED matches)."""
    from soccer_stats.edge import corner_recal

    out: dict = {}
    for cand in FAMILY:
        rows = long_rows(pred, cand)
        out[cand] = {}
        for grp in GROUPS:
            x = rows if grp == "all" else rows[rows["cup"] == grp]
            if x.empty or x["row"].nunique() < 2:
                out[cand][grp] = {"matches": int(x["row"].nunique()), "judged": False}
                continue
            x = x.copy()
            x["row"] = pd.factorize(x["row"])[0]
            r = corner_recal.score(x, "p_raw", level)
            r["judged"] = r["matches"] >= MIN_JUDGED
            r["pass"] = bool(r["pass"] and r["judged"])
            out[cand][grp] = r
    return out


def verdict(res: dict) -> dict:
    """The pre-registered reading per cup: the finalist (i or j, by gain; ties i) and
    whether cup-only clubs get chances ((k) passes there, or pooled when not judged)."""
    out = {}
    for cup in ("UCL", "UEL", "UECL"):
        judged = any(res[c][cup].get("judged") for c in ("i", "j"))
        src = cup if judged else "all"
        passing = [
            (res[c][src]["gain"], c == "i", c) for c in ("i", "j") if res[c][src].get("pass")
        ]
        fin = max(passing)[2] if passing else None
        k = res["k"][cup] if res["k"][cup].get("judged") else res["k"]["all"]
        out[cup] = {"from": src, "finalist": fin, "cup_only": bool(k.get("pass"))}
    return out


# ---------- the runs ----------


def load_all(
    raw_dir: Path = RAW_DIR,
    get: Callable = requests.get,
    today: pd.Timestamp | None = None,
    workers: int = WORKERS,
) -> dict:
    """Everything both runs read: domestic corners and names, cup events of 2022/23 to
    now (main and qualifying), their corner counts, the club map and the cup frame, plus
    the 2026/27 league-phase clubs per cup."""
    today = today or pd.Timestamp.now(tz="UTC")
    dom = load_domestic(raw_dir=raw_dir)
    stats: dict = {}
    years = list(range(min(HISTORY_SEASONS), cup_model.season_of(today) + 1))
    events, rep = cup_model.european_events(
        years, raw_dir, get, today=today, until=today, stats=stats
    )
    corners, cstats = fetch_corners(events, raw_dir, get, today=today, workers=workers)
    season: dict[str, list[dict]] = {}  # 2026/27 main-slug calendars (the estimate's too)
    phase: dict[str, list[dict]] = {}
    for code, slug in CUPS.items():
        cal = cups.season_calendar(
            slug, *cups.season_window(CURRENT), raw_dir, get, today=today, stats=stats
        )
        season[code] = cal["events"]
        phase[code] = [
            e
            for e in cal["events"]
            if LEAGUE_PHASE[0] <= e["kickoff"] < LEAGUE_PHASE[1]
            and not (
                cup_model.PLACEHOLDER.match(e["home"]) or cup_model.PLACEHOLDER.match(e["away"])
            )
        ]
    names = [e[s] for e in events for s in ("home", "away")]
    names += [e[s] for evs in phase.values() for e in evs for s in ("home", "away")]
    mapping = club_map(names, dom["teams"])
    frame = cup_frame(events, corners, mapping)
    return {
        "domestic": dom,
        "events": events,
        "espn": {**rep, "summaries": cstats},
        "mapping": mapping,
        "cup": frame,
        "phase": phase,
        "season": season,
        "today": today,
    }


def coverage(data: dict) -> dict:
    """Counts only (no fit, no score): football-data's HC/AC per file and season; per cup
    and season the finished matches and summaries with both counts; the cup clubs mapped
    to a corner league, to a file without corners, or to nothing; the 2026/27
    league-phase clubs with domestic corner data."""
    dom = data["domestic"]
    corner_leagues = {
        r["league"] for r in dom["coverage"] if r["with_corners"] > 0 and r["season"] != "all"
    }
    cup = data["cup"]
    per_cup = []
    for (code, season), g in cup.groupby(["cup", "season"]):
        per_cup.append(
            {
                "cup": code,
                "season": int(season),
                "finished": int(len(g)),
                "with_corners": int((g["home_corners"].notna() & g["away_corners"].notna()).sum()),
                "qualifying": int(g["slug"].str.endswith("_qual").sum()),
            }
        )

    def tier(name: str) -> str:
        m = data["mapping"].get(name)
        if not m:
            return "unmapped"
        return "corners" if m["league"] in corner_leagues else "no_corners"

    clubs = {}
    for (code, season), g in cup.groupby(["cup", "season"]):
        names = sorted(set(g["home"]) | set(g["away"]))
        t = [tier(n) for n in names]
        clubs[f"{code} {season}"] = {
            "clubs": len(names),
            "corners": t.count("corners"),
            "no_corners": t.count("no_corners"),
            "unmapped": t.count("unmapped"),
        }
    phase = {}
    for code, evs in data["phase"].items():
        names = sorted({e[s] for e in evs for s in ("home", "away")})
        t = {n: tier(n) for n in names}
        phase[code] = {
            "clubs": len(names),
            "corners": sum(v == "corners" for v in t.values()),
            "no_corners": sorted(n for n, v in t.items() if v == "no_corners"),
            "unmapped": sorted(n for n, v in t.items() if v == "unmapped"),
        }
    return {
        "football_data": dom["coverage"],
        "football_data_errors": dom["errors"],
        "corner_leagues": sorted(corner_leagues),
        "espn": data["espn"],
        "cups": per_cup,
        "clubs": clubs,
        "league_phase_2026": phase,
    }


def current_clubs(data: dict) -> dict:
    """Per cup, the 2026/27 league-phase clubs with a chance from a fit at today: rated
    domestically ((i)/(j)), from cup matches only ((k)), or unrated."""
    m = CupCorners().fit(data["domestic"]["matches"], data["cup"], data["today"])
    out = {"fit": m.info()}
    for code, evs in data["phase"].items():
        names = sorted({e[s] for e in evs for s in ("home", "away")})
        ids = {e[s]: e.get(f"{s}_id") for e in evs for s in ("home", "away")}
        r = {
            n: m.rated(side_key({"x": n, "x_id": ids.get(n)}, "x", data["mapping"])) for n in names
        }
        out[code] = {
            "clubs": len(names),
            "domestic": sum(v == "domestic" for v in r.values()),
            "cup_only": sorted(n for n, v in r.items() if v == "cup_only"),
            "unrated": sorted(n for n, v in r.items() if v is None),
        }
    return out


def estimate(data: dict) -> dict:
    """Live costs on ESPN's 2026/27 main-slug calendar (August to June): corner snapshots
    (look + close, 1 credit a call with bookmakers=pinnacle,draftkings and one market)
    per month, and DraftKings h2h refreshes (the matchday rule) for the event ids."""
    out = {}
    start, end = cups.season_window(CURRENT)
    for code in CUPS:
        ks = [e["kickoff"] for e in data["season"].get(code, [])]
        snap = cups.snapshot_estimate(code, ks, start, end)
        live = cups.live_estimate(code, ks, start, end)
        out[code] = {
            "matches": len(ks),
            "corners": {r["month"]: r["corners"] for r in snap["months"] if r["matches"]},
            "corners_season": snap["season"]["corners"],
            "corners_typical": snap["typical"]["corners"],
            "h2h": {r["month"]: r["credits"] for r in live["months"] if r["matches"]},
            "h2h_season": live["season"]["credits"],
            "h2h_typical": live["typical"],
        }
    return out


def evaluate(data: dict, level: float) -> dict:
    """The pre-registered walk-forward on 2024/25 and 2025/26 and its scores."""
    pred = walk_forward(data["domestic"]["matches"], data["cup"])
    res = score(pred, level)
    sets = pred["set"].fillna("none").value_counts().to_dict() if len(pred) else {}
    by_cup = {}
    for code, g in pred.groupby("cup"):
        by_cup[code] = {
            "scored": int(len(g)),
            "D": int((g["set"] == "D").sum()),
            "X": int((g["set"] == "X").sum()),
            "none": int(g["set"].isna().sum()),
        }
    return {
        "level": level,
        "tests": TESTS,
        "matches": int(len(pred)),
        "sets": sets,
        "by_cup": by_cup,
        "seasons": sorted(int(s) for s in pred["season"].unique()) if len(pred) else [],
        "candidates": res,
        "verdict": verdict(res),
    }


# ---------- report ----------


def _rng(v, f="{:+.4f}"):
    return f"{f.format(v[0])}..{f.format(v[1])}" if v else "–"


def coverage_report(cov: dict) -> str:
    out = ["== Cup corners: coverage (counts only; no fit, no score) =="]
    out.append("football-data HC/AC (file, season: with corners / played):")
    rows = cov["football_data"]
    for lg in dict.fromkeys(r["league"] for r in rows):
        mine = [r for r in rows if r["league"] == lg]
        out.append(
            f"  {lg:<4} "
            + ", ".join(f"{r['season']} {r['with_corners']}/{r['played']}" for r in mine)
            + ("" if any(r["columns"] for r in mine) else "  (no HC/AC columns)")
        )
    if cov["football_data_errors"]:
        out.append(f"  unavailable: {cov['football_data_errors']}")
    out.append(f"Leagues with corner counts: {cov['corner_leagues']}")
    e = cov["espn"]
    state = "complete" if e.get("complete") else f"INCOMPLETE {e.get('why')}"
    out.append(
        f"ESPN: scoreboards {e.get('requests')} requests ({state}); summaries {e['summaries']}"
    )
    out.append("Cup matches (finished, with both corner counts, of which qualifying):")
    for r in cov["cups"]:
        out.append(
            f"  {r['cup']:<4} {r['season']}/{(r['season'] + 1) % 100:02d}: {r['with_corners']} of "
            f"{r['finished']} ({r['qualifying']} qualifying)"
        )
    out.append("Cup clubs per season (corner league / file without corners / unmapped):")
    for k, v in cov["clubs"].items():
        out.append(
            f"  {k}: {v['clubs']} clubs: {v['corners']} / {v['no_corners']} / {v['unmapped']}"
        )
    out.append("2026/27 league phase, clubs with domestic corner data:")
    for code, v in cov["league_phase_2026"].items():
        out.append(
            f"  {code}: {v['corners']} of {v['clubs']}; no corners {v['no_corners']}; "
            f"unmapped {v['unmapped']}"
        )
    return "\n".join(out)


def report(res: dict) -> str:
    out = [
        f"== Cup corners: walk-forward {res['seasons']} (gain ranges {res['level']:.3%}, "
        f"{res['tests']} tests, slope band {SLOPE_BAND[0]}-{SLOPE_BAND[1]}), "
        f"{res['matches']} scored matches, sets {res['sets']} =="
    ]
    for code, v in res["by_cup"].items():
        out.append(f"  {code}: {v}")
    for cand, groups in res["candidates"].items():
        out.append(f"({cand}) {NAMES[cand]} on set {SET_OF[cand]}:")
        for grp, r in groups.items():
            if "gain" not in r:
                out.append(f"  {grp:<4}: {r['matches']} matches (not scored)")
                continue
            out.append(
                f"  {grp:<4}: {r['matches']} matches, gain {r['gain']:+.4f} "
                f"({_rng(r['gain_range'])}), slopes {min(r['slopes'].values()):.2f}-"
                f"{max(r['slopes'].values()):.2f}, in band {r['slopes_in_band']}, judged "
                f"{r['judged']}: PASS {r['pass']}"
            )
            for name, s in r["lines"].items():
                out.append(
                    f"      {name:>9}: over {s['over_rate']:.3f} vs predicted "
                    f"{s['predicted']:.3f}, slope {s['slope']:.2f} "
                    f"({_rng(s['slope_range'], '{:.2f}')}), gain {s['gain']:+.4f}"
                )
    for cup, v in res["verdict"].items():
        out.append(
            f"Verdict {cup} (from {v['from']}): finalist {v['finalist'] or 'none'}; "
            f"cup-only clubs {'rated' if v['cup_only'] else 'unrated'}"
        )
    return "\n".join(out)


def current_report(cur: dict) -> str:
    out = [f"== 2026/27 league-phase clubs with a chance (fit {cur['fit']}) =="]
    for code in CUPS:
        v = cur.get(code)
        if not v:
            continue
        out.append(
            f"  {code}: {v['clubs']} clubs: {v['domestic']} rated domestically, "
            f"{len(v['cup_only'])} from cup matches only {v['cup_only']}, unrated "
            f"{len(v['unrated'])} {v['unrated']}"
        )
    return "\n".join(out)


def estimate_report(est: dict) -> str:
    out = ["== Live cost on ESPN's 2026/27 calendar (estimate, 0 credits) =="]
    for code, v in est.items():
        out.append(
            f"  {code}: {v['matches']} matches; corner snapshots season {v['corners_season']}, "
            f"typical month {v['corners_typical']:.0f} {v['corners']}; DraftKings h2h season "
            f"{v['h2h_season']}, typical month {v['h2h_typical']:.0f}"
        )
    return "\n".join(out)


def cli(coverage_only: bool = False, workers: int = WORKERS, json_path: str | None = None):
    """`soccer-stats cup-corners`: the coverage count (and the cost estimate) with
    `coverage_only`, else the coverage, the pre-registered evaluation and the 2026/27
    clubs with a chance. Prints only; ESPN and football-data, no key, 0 credits."""
    from soccer_stats.edge.stats import bonferroni_level
    from soccer_stats.lab.run import _jsonable

    data = load_all(workers=workers)
    cov = coverage(data)
    print(coverage_report(cov))
    out: dict = {"coverage": cov}
    if coverage_only:
        est = estimate(data)
        print(estimate_report(est))
        out["estimate"] = est
        print("CUP_CORNER_COVERAGE_JSON " + json.dumps(_jsonable(out), default=str))
    else:
        res = evaluate(data, bonferroni_level(TESTS))
        print(report(res))
        cur = current_clubs(data)
        print(current_report(cur))
        out.update(results=res, current=cur)
        print("CUP_CORNERS_JSON " + json.dumps(_jsonable(out), default=str))
    if json_path:
        Path(json_path).parent.mkdir(parents=True, exist_ok=True)
        Path(json_path).write_text(json.dumps(_jsonable(out), default=str))
    return out
