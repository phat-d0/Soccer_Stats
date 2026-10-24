"""Live team-corner prices at Pinnacle and the corners model (f) on every card.

Owner-approved on 10 Oct (docs/totals.md, "Live corner prices and paper trades"):

* **Prices.** Pinnacle's `alternate_team_totals_corners` (each team's corners; the market
  probe, run 37889595473, found Pinnacle listing it in all six leagues, one line per team,
  two-sided, median margin 5.8-7.0%) in E0, SP1, D1, I1, F1 and E1, and from 24 Oct in
  League One and League Two (E2, E3: listed in the 24 Oct discovery, run 40088231180, not
  yet seen priced; Pinnacle only, PINNACLE_ONLY). A league whose last EMPTY_PAUSE calls
  logged no rows pauses for EMPTY_PAUSE_DAYS (paused_leagues), so listed-but-unpriced
  costs a few credits, not one per look. One look 18-30 hours
  before kickoff and one close just before it, on the team-total schedule
  (team_totals.due_snapshot). Each snapshot is its own `/events/{id}/odds` call with
  `bookmakers=pinnacle` and one market: 1 credit, the same as adding the market to the
  team-total call, and the team-total test's calls and rows stay exactly as they were.
* **Never twice.** Every call is recorded (`corners_calls_<YYYY-MM>.jsonl` on data-log and
  a local copy), one row per event, snapshot and market group; a recorded one is never
  fetched again, even if Pinnacle returned nothing.
* **Budget.** Its own monthly cap (CORNERS_MONTHLY_CAP, counted on the corner calls only,
  so the team-total test's 800 is untouched) and the shared 3,000-credit reserve on the
  freshest balance seen (any league's DraftKings meta, the last team-total or corner
  call). The cost is read from `x-requests-last`. The key is never printed or logged.
* **Rows.** `odds_log/<code>_corners_<YYYY-MM>.jsonl`, `market: "team_corners"`: one row
  per team and line with both sides, Shin's fair prices, the margin, the snapshot, when
  it was downloaded and how long before kickoff, and the model's P(over) then.
* **Model (f).** Corners bake-off 2's empirical-Bayes team averages (edge/corners2.py),
  refitted every build on the league's football-data matches with corner counts from the
  730 days before the last earlier match (the bake-off's window), earlier matches only.
  File reads and the fit only: no API calls.

* **DraftKings beside Pinnacle (owner, 20 Oct).** The same call asks for
  `bookmakers=pinnacle,draftkings`: up to ten books count as one region and the API bills
  per market per region, so it stays 1 credit (the market probe, run 37889595473, billed
  each odds call exactly its market count with ten books from three regions). Pinnacle's
  rows are exactly as before (two-sided lines only). DraftKings' rows have the same schema
  with `book: "draftkings"`; a one-sided DraftKings line is kept with the missing side
  None and no fair price or margin. The probe's discovery lists the market at DraftKings
  in E0, SP1, D1, I1 and F1 (one line per team, two-sided, median margin 7.5-8.2%), not
  in the Championship, where those calls return Pinnacle only. The pre-call budget check
  uses the dearest call this month at these books (at least COST_PER_CALL), so the cap and
  the reserve still hold if the real cost ever rises.

The corners paper portfolio (paper.update_corner_ledger) trades on these rows and cards:
one strategy per book (Pinnacle, and DraftKings from 20 Oct).
"""

from __future__ import annotations

import math
import os
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from soccer_stats import team_totals as tt
from soccer_stats.data import RAW_DIR, current_season, season_code
from soccer_stats.odds import devig_shin
from soccer_stats.odds_feed import MATCHDAY_RESERVE_CREDITS, SPORTS, _team

MARKET = "alternate_team_totals_corners"  # The Odds API key (market probe, run 37889595473)
ROW_MARKET = "team_corners"  # `market` on a logged row (team totals: "team_totals")
GROUP = "team_corners"  # the call record's market group
BOOK = "pinnacle"  # the original track: its rows and trades are unchanged
DK = "draftkings"
BOOKS = (BOOK, DK)  # one call, both books (one region of up to ten books: 1 credit)
BOOKS_PARAM = ",".join(BOOKS)
# League One and League Two from 24 Oct (owner, pre-approved 20 Oct; docs/leagues.md "League
# One and League Two check"): Pinnacle's discovery listed the market there (run
# 40088231180), DraftKings didn't, so those calls ask Pinnacle alone.
LEAGUES = ("E0", "SP1", "D1", "I1", "F1", "E1", "E2", "E3")
PINNACLE_ONLY = ("E2", "E3")
COST_PER_CALL = 1  # one market, up to ten books (one region): 1 credit
CORNERS_MONTHLY_CAP = 750  # credits a calendar month (UTC); 500 until 10 Oct, 550 until 24 Oct
# Listed is not priced: a league whose last EMPTY_PAUSE calls all came back with no rows is
# paused for EMPTY_PAUSE_DAYS after the last of them, then looked at once more.
EMPTY_PAUSE = 6
EMPTY_PAUSE_DAYS = 7
LOCAL_CALLS = "corners_calls.jsonl"  # local copy of the call record (raw-data cache)
TRAIN_DAYS = 730  # corners2's window
MIN_TRAIN = 300  # matches with corner counts in the window, else no model for the league
TRAIN_SEASONS = 3  # football-data files read (publish caches three per live league)
CDF_MAX = 15  # the card's cdf: P(corners <= k), k = 0..CDF_MAX
KMAX = 30  # corners per side, 0..KMAX (as the bake-off)
MODEL = "f"


def _ts(x) -> pd.Timestamp:
    return tt._ts(x)


def books_for(league: str) -> tuple[str, ...]:
    """The books one corner call asks for in `league`: Pinnacle alone where DraftKings
    lists no team corners (PINNACLE_ONLY), else Pinnacle and DraftKings (one region, 1
    credit either way)."""
    return (BOOK,) if league in PINNACLE_ONLY else BOOKS


def paused_leagues(calls: list[dict], now) -> dict[str, str]:
    """{league: until} for the leagues whose last EMPTY_PAUSE answered calls (status 200)
    all logged no rows, while EMPTY_PAUSE_DAYS haven't passed since the last of them: a
    listed market that isn't priced stops costing a credit per look."""
    now = _ts(now)
    by: dict[str, list[dict]] = {}
    for c in calls:
        if c.get("group", GROUP) == GROUP and c.get("league") and c.get("fetched_at"):
            by.setdefault(c["league"], []).append(c)
    out = {}
    for lg, cs in by.items():
        cs = sorted(cs, key=lambda c: _ts(c["fetched_at"]))[-EMPTY_PAUSE:]
        if len(cs) < EMPTY_PAUSE:
            continue
        if all(c.get("status") == 200 and not c.get("rows") for c in cs):
            until = _ts(cs[-1]["fetched_at"]) + pd.Timedelta(days=EMPTY_PAUSE_DAYS)
            if now < until:
                out[lg] = until.isoformat(timespec="minutes")
    return out


def _iso(t) -> str:
    return tt._iso(t)


# ---------- model (f), live ----------


def corner_matches(league: str, raw_dir: Path = RAW_DIR, seasons=None) -> pd.DataFrame:
    """Played matches with corner counts (football-data's HC/AC) from the cached season
    files: league, season, date, home, away, home_corners, away_corners. File reads only."""
    if seasons is None:
        now = current_season()
        seasons = range(now - TRAIN_SEASONS + 1, now + 1)
    frames = []
    for y in seasons:
        code = season_code(y)
        path = Path(raw_dir) / f"{league}_{code}.csv"
        if not path.exists():
            continue
        raw = pd.read_csv(path, encoding="latin-1", on_bad_lines="skip")
        if not {"Date", "HomeTeam", "AwayTeam", "HC", "AC"} <= set(raw):
            continue
        df = pd.DataFrame(
            {
                "league": league,
                "season": code,
                "date": pd.to_datetime(raw["Date"], dayfirst=True, format="mixed", errors="coerce"),
                "home": raw["HomeTeam"],
                "away": raw["AwayTeam"],
                "home_corners": pd.to_numeric(raw["HC"], errors="coerce"),
                "away_corners": pd.to_numeric(raw["AC"], errors="coerce"),
            }
        )
        frames.append(df.dropna(subset=["date", "home", "away", "home_corners", "away_corners"]))
    if not frames:
        return pd.DataFrame(
            columns=["league", "season", "date", "home", "away", "home_corners", "away_corners"]
        )
    return pd.concat(frames, ignore_index=True).sort_values("date", kind="stable")


class TeamCornersF:
    """Corners bake-off 2's (f): each team's corners for and against as ratios to the
    league's home/away means, shrunk with empirical-Bayes pseudo-matches estimated on the
    window, NB2 dispersion moment-matched per side. The same arithmetic as
    corners2.Bakeoff2's (f) (tests/test_corners_live.py checks they agree)."""

    def fit(self, matches: pd.DataFrame, now=None):
        from soccer_stats.edge.corners import mom_alpha
        from soccer_stats.edge.corners2 import eb_shrink

        t = matches.copy()
        t["date"] = pd.to_datetime(t["date"])
        if now is not None:  # earlier matches only (football-data dates are days)
            cut = _ts(now).tz_convert(None)
            t = t[t["date"] < cut]
        if t.empty:
            raise ValueError("no earlier matches with corner counts")
        t = t[t["date"] > t["date"].max() - pd.Timedelta(days=TRAIN_DAYS)].reset_index(drop=True)
        if len(t) < MIN_TRAIN:
            raise ValueError(
                f"{len(t)} matches with corner counts in the window (need {MIN_TRAIN})"
            )
        hc, ac = t["home_corners"].to_numpy(float), t["away_corners"].to_numpy(float)
        mh, ma = hc.mean(), ac.mean()
        self.mean_ = (mh, ma)
        teams = pd.concat([t["home"], t["away"]], ignore_index=True)
        f = np.r_[hc / mh, ac / ma]
        a = np.r_[ac / ma, hc / mh]
        self.k_ = (eb_shrink(teams, f), eb_shrink(teams, a))
        g = pd.DataFrame({"team": teams, "f": f, "a": a}).groupby("team")
        n = g.size()
        kf, ka = self.k_
        self.for_ = ((g["f"].sum() + kf) / (n + kf)).to_dict()
        self.against_ = ((g["a"].sum() + ka) / (n + ka)).to_dict()
        lh, la = self.rates(t["home"], t["away"])
        self.alpha_ = (mom_alpha(hc, lh), mom_alpha(ac, la))
        self.n_ = int(len(t))
        self.window_ = (t["date"].min(), t["date"].max())
        return self

    def rates(self, home, away) -> tuple[np.ndarray, np.ndarray]:
        get = lambda d, ts: np.array([d.get(x, 1.0) for x in ts])  # noqa: E731
        h, a = np.asarray(home), np.asarray(away)
        mh, ma = self.mean_
        return (
            mh * get(self.for_, h) * get(self.against_, a),
            ma * get(self.for_, a) * get(self.against_, h),
        )

    def pmfs(self, home, away) -> tuple[np.ndarray, np.ndarray]:
        from soccer_stats.models.player_counts import nb_pmf

        lh, la = self.rates(home, away)
        return nb_pmf(lh, self.alpha_[0], KMAX), nb_pmf(la, self.alpha_[1], KMAX)

    def info(self) -> dict:
        return {
            "model": MODEL,
            "matches": self.n_,
            "window_from": self.window_[0].date().isoformat(),
            "window_to": self.window_[1].date().isoformat(),
            "k_for": round(float(self.k_[0]), 2),
            "k_against": round(float(self.k_[1]), 2),
            "mean_home": round(float(self.mean_[0]), 3),
            "mean_away": round(float(self.mean_[1]), 3),
            "alpha_home": round(float(self.alpha_[0]), 4),
            "alpha_away": round(float(self.alpha_[1]), 4),
        }


def side_probs(cdf, line: float) -> dict | None:
    """Win and push chances for over and under at a team line from P(corners <= k):
    over wins at count > line, under at count < line; a whole line pushes at count ==
    line. None for a quarter line or a line beyond the cdf."""
    if cdf is None or line is None:
        return None
    frac = float(line) % 1
    if frac not in (0.0, 0.5):
        return None
    k = math.floor(line)
    if k < 0 or k >= len(cdf) or cdf[k] is None:
        return None
    le = float(cdf[k])  # P(count <= k)
    if frac == 0.5:
        return {"over": 1 - le, "under": le, "push": 0.0}
    lt = float(cdf[k - 1]) if k >= 1 else 0.0  # whole line: push at count == line
    return {"over": 1 - le, "under": lt, "push": le - lt}


def model_over(card: dict, side: str, line: float) -> float | None:
    """P(team corners > line) from the card's model cdf (None without one)."""
    cdf = ((card.get("corners") or {}).get(side) or {}).get("cdf")
    p = side_probs(cdf, line)
    return round(p["over"], 4) if p else None


def add_model(
    cards: list[dict], raw_dir: Path = RAW_DIR, now=None, leagues=LEAGUES, seasons=None
) -> dict:
    """Each card in these leagues gets `corners = {model, home: {mean, cdf}, away: {...}}`
    from the league's model (f) fitted at `now` on earlier matches. Returns per league the
    fit's info or the error. Never raises."""
    now = _ts(now or pd.Timestamp.now(tz="UTC"))
    out: dict = {}
    for lg in leagues:
        mine = [c for c in cards if c.get("league") == lg and c.get("home") and c.get("away")]
        if not mine:
            continue
        try:
            m = TeamCornersF().fit(corner_matches(lg, raw_dir, seasons), now)
            ph, pa = m.pmfs([c["home"] for c in mine], [c["away"] for c in mine])
            lh, la = m.rates([c["home"] for c in mine], [c["away"] for c in mine])
        except Exception as exc:  # noqa: BLE001  a league without corners keeps its cards
            out[lg] = {"error": f"{type(exc).__name__}: {exc}"}
            continue
        for i, c in enumerate(mine):
            c["corners"] = {
                "model": MODEL,
                "home": {"mean": round(float(lh[i]), 3), "cdf": _cdf(ph[i])},
                "away": {"mean": round(float(la[i]), 3), "cdf": _cdf(pa[i])},
            }
        out[lg] = {**m.info(), "cards": len(mine)}
    return out


def _cdf(pmf: np.ndarray) -> list[float]:
    return [round(float(x), 5) for x in np.cumsum(pmf)[: CDF_MAX + 1]]


# ---------- the trade rule's view of a quote (shared with paper and the card) ----------


def pick(cdf, line: float, over: float, under: float, threshold: float) -> dict | None:
    """The better of over and under at a team line if its edge reaches `threshold`:
    edge = P(win) x decimal odds + P(push) - 1 (the stake comes back on a push, so on a
    half line this is p x odds - 1). None for a quarter line, an unpriced side or no model."""
    from soccer_stats.trades import EPS

    probs = side_probs(cdf, line)
    if probs is None or threshold is None:
        return None
    best = None
    for side, odds in (("over", over), ("under", under)):
        if odds is None or not np.isfinite(odds) or odds <= 1:
            continue
        p = probs[side]
        e = p * odds + probs["push"] - 1
        if e > 0 and e >= threshold - EPS and (best is None or e > best["edge"]):
            best = {
                "side": side,
                "odds": float(odds),
                "model_p": round(p, 4),
                "p_push": round(probs["push"], 4),
                "edge": round(float(e), 4),
            }
    return best


# ---------- state: the call record ----------


def load_calls(*dirs) -> list[dict]:
    """Every recorded corner call in these folders (data-log's odds_log, a local copy),
    one per (event, snapshot, market group)."""
    paths = []
    for d in dirs:
        if d is None:
            continue
        d = Path(d)
        paths += sorted(d.glob("corners_calls_*.jsonl")) + [d / LOCAL_CALLS]
    seen, out = set(), []
    for c in tt._read_jsonl(paths):
        key = _call_key(c)
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def _call_key(c: dict) -> tuple:
    return (c.get("event_id"), c.get("snapshot"), c.get("group") or GROUP)


# ---------- rows ----------


def rows_from_body(
    body: dict | None, card: dict, event_id: str, snapshot: str, downloaded: pd.Timestamp
) -> list[dict]:
    """One row per book, team and line: Pinnacle's with both sides priced (unchanged),
    then DraftKings' with at least one side priced (a one-sided line has the other side,
    the fair prices and the margin None)."""
    rows = _book_rows(body, card, event_id, snapshot, downloaded, BOOK, two_sided=True)
    return rows + _book_rows(body, card, event_id, snapshot, downloaded, DK, two_sided=False)


def _book_rows(
    body: dict | None,
    card: dict,
    event_id: str,
    snapshot: str,
    downloaded: pd.Timestamp,
    book: str,
    two_sided: bool,
) -> list[dict]:
    k = _ts(card["kickoff"])
    known = {card["home"], card["away"]}
    pairs: dict[tuple, dict] = {}
    for b in (body or {}).get("bookmakers", []) or []:
        if b.get("key") != book:
            continue
        for m in b.get("markets", []) or []:
            if m.get("key") != MARKET:
                continue
            updated = m.get("last_update") or b.get("last_update")
            for o in m.get("outcomes", []) or []:
                side = str(o.get("name", "")).lower()
                team = _team(str(o.get("description", "")), known)
                if team not in known or side not in ("over", "under"):
                    continue
                if o.get("point") is None or o.get("price") is None:
                    continue
                key = (team, float(o["point"]))
                pairs.setdefault(key, {"last_update": updated})[side] = float(o["price"])
    rows = []
    for (team, line), q in sorted(pairs.items()):
        if two_sided:
            if "over" not in q or "under" not in q or min(q["over"], q["under"]) <= 1:
                continue
        else:
            for s_ in ("over", "under"):
                if s_ in q and not q[s_] > 1:
                    del q[s_]
            if "over" not in q and "under" not in q:
                continue
        both = "over" in q and "under" in q
        fair = devig_shin(np.array([q["over"], q["under"]])) if both else (None, None)
        quoted = _ts(q["last_update"]) if q.get("last_update") else downloaded
        side = "home" if team == card["home"] else "away"
        rows.append(
            {
                "market": ROW_MARKET,
                "league": card.get("league"),
                "home": card["home"],
                "away": card["away"],
                "kickoff": _iso(k),
                "event_id": event_id,
                "snapshot": snapshot,
                "book": book,
                "team": team,
                "side": side,
                "line": line,
                "over": q.get("over"),
                "under": q.get("under"),
                "fair_over": round(float(fair[0]), 6) if both else None,
                "fair_under": round(float(fair[1]), 6) if both else None,
                "margin": round(1 / q["over"] + 1 / q["under"] - 1, 6) if both else None,
                "fetched_at": _iso(quoted),
                "time_source": "last_update" if q.get("last_update") else "download",
                "downloaded_at": _iso(downloaded),
                "minutes_before": round((k - downloaded) / pd.Timedelta(minutes=1), 1),
                "p_model_over": model_over(card, side, line),
            }
        )
    return rows


# ---------- one publish run ----------


def run(
    cards: list[dict],
    out_dir: Path,
    state_dirs=(),
    raw_dir: Path = RAW_DIR,
    api_key: str | None = None,
    now: pd.Timestamp | None = None,
    get=requests.get,
    balance_dirs=(),
) -> dict:
    """Fetch every due corner snapshot within budget; write the rows and calls to
    `out_dir` (rows.jsonl, calls.jsonl) and the calls to the local record. Never raises.

    `state_dirs` hold the corner call record (data-log copy); `balance_dirs` the
    team-total one, read only for the freshest balance on the shared key."""
    now = _ts(now or pd.Timestamp.now(tz="UTC"))
    api_key = api_key if api_key is not None else os.environ.get("ODDS_API_KEY", "")
    raw_dir = Path(raw_dir)
    calls = load_calls(*state_dirs, raw_dir)
    done = {_call_key(c) for c in calls}
    spent = tt.month_spend(calls, now)
    expected = expected_cost(calls, now)
    try:
        others = tt.load_calls(*balance_dirs, raw_dir)
    except Exception:  # noqa: BLE001  the balance falls back to the DraftKings meta
        others = []
    credits = tt.freshest_credits(raw_dir, calls + others)
    paused = paused_leagues(calls, now)
    summary = {
        "calls": 0,
        "credits": 0,
        "rows": 0,
        "rows_by_book": {},
        "due": 0,
        "month_spent": spent,
        "stop": None,
        "paused": paused,
    }
    if not api_key:
        summary["stop"] = "no ODDS_API_KEY"
        return summary
    runs = tt.publish_runs(now, now + pd.Timedelta(hours=2))
    by_league: dict[str, list[dict]] = {}
    for c in cards:
        if c.get("league") in LEAGUES and c.get("kickoff"):
            by_league.setdefault(c["league"], []).append(c)
    new_rows, new_calls = [], []
    try:  # a crash part-way still writes the calls already made (below)
        for league in [lg for lg in LEAGUES if lg in by_league and lg not in paused]:
            lcards = by_league[league]
            books = ",".join(books_for(league))
            due = [(c, s) for c in lcards if (s := tt.due_snapshot(c["kickoff"], now, runs))]
            if not due:
                continue
            known = {c["home"] for c in lcards} | {c["away"] for c in lcards}
            events = tt.event_index(raw_dir, league, known)
            for card, snap in due:
                eid = tt.match_event(card, events)
                if eid is None or (eid, snap, GROUP) in done:
                    continue
                summary["due"] += 1
                if credits is not None and credits - expected < MATCHDAY_RESERVE_CREDITS:
                    summary["stop"] = (
                        f"reserve: {credits} credits left (keeping {MATCHDAY_RESERVE_CREDITS})"
                    )
                    break
                if spent + expected > CORNERS_MONTHLY_CAP:
                    summary["stop"] = (
                        f"monthly cap: {spent} of {CORNERS_MONTHLY_CAP} corner credits used"
                    )
                    break
                try:
                    resp = get(
                        tt.URL.format(sport=SPORTS[league], event_id=eid),
                        params={
                            "apiKey": api_key,
                            "bookmakers": books,
                            "markets": MARKET,
                            "oddsFormat": "decimal",
                            "dateFormat": "iso",
                        },
                        timeout=30,
                    )
                except requests.RequestException as exc:  # message could contain the URL
                    summary["stop"] = f"could not reach The Odds API ({type(exc).__name__})"
                    break
                cost = tt._header_int(resp, "x-requests-last")
                left = tt._header_int(resp, "x-requests-remaining")
                if cost is None:  # unknown: count the worst case of a successful call
                    cost = expected if resp.ok else 0
                if left is not None:
                    credits = left
                spent += cost
                expected = max(expected, cost)  # a dearer call than thought: budget for it
                rows = []
                if resp.ok:
                    try:
                        rows = rows_from_body(resp.json(), card, eid, snap, now)
                    except Exception:  # noqa: BLE001  odd body: keep the call record, no rows
                        rows = []
                call = {
                    "league": league,
                    "event_id": eid,
                    "home": card["home"],
                    "away": card["away"],
                    "kickoff": _iso(card["kickoff"]),
                    "snapshot": snap,
                    "group": GROUP,
                    "fetched_at": _iso(now),
                    "status": resp.status_code,
                    "cost": cost,
                    "credits_left": credits,
                    "books": books,
                    "markets": MARKET,
                    "rows": len(rows),
                }
                done.add((eid, snap, GROUP))
                new_calls.append(call)
                new_rows += rows
            if summary["stop"]:
                break
    except Exception as exc:  # noqa: BLE001  type only: a message could hold the URL
        summary["stop"] = f"error ({type(exc).__name__})"
    summary.update(
        calls=len(new_calls),
        credits=sum(c["cost"] for c in new_calls),
        rows=len(new_rows),
        rows_by_book={b: sum(r["book"] == b for r in new_rows) for b in BOOKS},
        month_spent=spent,
        credits_left=credits,
    )
    if new_calls:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        tt._append(out_dir / "rows.jsonl", new_rows)
        tt._append(out_dir / "calls.jsonl", new_calls)
        raw_dir.mkdir(parents=True, exist_ok=True)
        tt._append(raw_dir / LOCAL_CALLS, new_calls)
    return summary


def expected_cost(calls: list[dict], now) -> int:
    """What the next call is budgeted at: COST_PER_CALL, or the dearest recorded call this
    month (UTC) at this test's books (both, or Pinnacle alone), if dearer. The cost always
    comes from `x-requests-last`; this only keeps the cap and the reserve checks honest if
    billing for a corner call is ever more than 1 credit."""
    month = _ts(now).strftime("%Y-%m")
    params = {BOOKS_PARAM, BOOK}
    seen = [
        int(c.get("cost") or 0)
        for c in calls
        if c.get("books") in params
        and c.get("fetched_at")
        and _ts(c["fetched_at"]).strftime("%Y-%m") == month
    ]
    return max([COST_PER_CALL, *seen])


def summary_line(s: dict) -> str:
    by = s.get("rows_by_book") or {}
    return (
        f"Corners (Pinnacle, DraftKings): {s.get('calls', 0)} calls, "
        f"{s.get('credits', 0)} credits, {s.get('rows', 0)} rows "
        f"({by.get(BOOK, 0)} Pinnacle, {by.get(DK, 0)} DraftKings); "
        f"month {s.get('month_spent', 0)} of {CORNERS_MONTHLY_CAP} corner credits"
        + (f", credits left {s['credits_left']}" if s.get("credits_left") is not None else "")
        + (
            "; paused (no prices in the last "
            + f"{EMPTY_PAUSE} calls): "
            + ", ".join(f"{lg} until {u}" for lg, u in sorted(s["paused"].items()))
            if s.get("paused")
            else ""
        )
        + (f" (stopped: {s['stop']})" if s.get("stop") else "")
    )


# ---------- data-log ----------


def _row_key(r: dict) -> tuple:
    return (
        r.get("league"),
        r.get("home"),
        r.get("away"),
        r.get("event_id"),
        r.get("snapshot"),
        r.get("book"),
        r.get("side"),
        r.get("line"),
        r.get("over"),
        r.get("under"),
        r.get("fetched_at"),
    )


def log(pending: Path, log_root: Path) -> tuple[int, int]:
    """Append the run's rows and calls to data-log's odds_log, deduplicated.
    Returns (rows written, calls written)."""
    pending, d = Path(pending), Path(log_root) / "odds_log"
    rows = tt._read_jsonl([pending / "rows.jsonl"])
    calls = tt._read_jsonl([pending / "calls.jsonl"])
    n_rows = n_calls = 0
    by_file: dict[Path, list[dict]] = {}
    for r in rows:
        month = _ts(r["downloaded_at"]).strftime("%Y-%m")
        by_file.setdefault(d / f"{r['league']}_corners_{month}.jsonl", []).append(r)
    for path, new in by_file.items():
        seen = {_row_key(x) for x in tt._read_jsonl([path])}
        keep = []
        for r in new:
            if _row_key(r) not in seen:
                seen.add(_row_key(r))
                keep.append(r)
        d.mkdir(parents=True, exist_ok=True)
        tt._append(path, keep)
        n_rows += len(keep)
    done = {_call_key(c) for c in load_calls(d)}
    by_month: dict[Path, list[dict]] = {}
    for c in calls:
        if _call_key(c) in done:
            continue
        done.add(_call_key(c))
        month = _ts(c["fetched_at"]).strftime("%Y-%m")
        by_month.setdefault(d / f"corners_calls_{month}.jsonl", []).append(c)
    for path, new in by_month.items():
        d.mkdir(parents=True, exist_ok=True)
        tt._append(path, new)
        n_calls += len(new)
    return n_rows, n_calls


def row_paths(*dirs) -> list[Path]:
    """Corner price files in these folders (data-log's odds_log or a state copy), plus a
    pending run's rows.jsonl; never the call record."""
    paths: list[Path] = []
    for d in dirs:
        if not d:
            continue
        d = Path(d)
        paths += [p for p in sorted(d.glob("*_corners_*.jsonl")) if "calls" not in p.name]
        paths.append(d / "rows.jsonl")
    return paths


def load_rows(*dirs) -> list[dict]:
    """Every logged corner row in these folders (team_corners only), deduplicated."""
    seen, out = set(), []
    for r in tt._read_jsonl(row_paths(*dirs)):
        if r.get("market") != ROW_MARKET:
            continue
        key = _row_key(r)
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def newest_quotes(
    rows: list[dict], before_kickoff: bool = True, book: str = BOOK
) -> dict[tuple, dict]:
    """The newest quote at one book (rows without `book` are Pinnacle's) per (league,
    home, away, kickoff, side, line), downloaded before kickoff."""
    latest: dict[tuple, dict] = {}
    for r in rows:
        if (r.get("book") or BOOK) != book:
            continue
        try:
            k = _ts(r["kickoff"])
            key = (r.get("league"), r["home"], r["away"], k, r["side"], float(r["line"]))
            got = _ts(r["downloaded_at"])
        except (KeyError, TypeError, ValueError):
            continue
        if before_kickoff and got >= k:
            continue
        if key not in latest or got > _ts(latest[key]["downloaded_at"]):
            latest[key] = r
    return latest


def add_quotes(
    cards: list[dict], dirs, threshold: float | None = None, book: str = BOOK, rows=None
) -> int:
    """Each card's newest team-corner quote at `book` per side and line before kickoff
    (data-log's price files in the state folder plus this run's rows.jsonl), with the
    model's chances now and the live-test pick (`pick`, or None): `corners.<book> =
    {book, fetched_at, home: [...], away: [...]}` (`corners.pinnacle`,
    `corners.draftkings`). A one-sided DraftKings line has the missing side and
    `fair_over`/`margin` None. Display only; no API calls. Returns the number of cards
    given prices."""
    from soccer_stats.trades import CORNERS_EDGE

    threshold = CORNERS_EDGE if threshold is None else threshold
    latest = newest_quotes(load_rows(*dirs) if rows is None else rows, book=book)
    n = 0
    for c in cards:
        try:
            ko = _ts(c["kickoff"])
        except (KeyError, TypeError, ValueError):
            continue
        out: dict = {"home": [], "away": []}
        stamps = []
        model = c.get("corners") or {}
        for (lg, h, a, k, side, line), r in latest.items():
            if (lg, h, a, k) != (c.get("league"), c["home"], c["away"], ko):
                continue
            cdf = (model.get(side) or {}).get("cdf")
            probs = side_probs(cdf, line)
            out[side].append(
                {
                    "line": line,
                    "over": r.get("over"),
                    "under": r.get("under"),
                    "fair_over": r.get("fair_over"),
                    "margin": r.get("margin"),
                    "snapshot": r.get("snapshot"),
                    "downloaded_at": r["downloaded_at"],
                    "p_over": round(probs["over"], 4) if probs else None,
                    "p_under": round(probs["under"], 4) if probs else None,
                    "p_push": round(probs["push"], 4) if probs else None,
                    "pick": pick(cdf, line, r.get("over"), r.get("under"), threshold),
                }
            )
            stamps.append(r.get("fetched_at") or r["downloaded_at"])
        if stamps:
            for side in ("home", "away"):
                out[side].sort(key=lambda x: x["line"])
            c.setdefault("corners", {})[book] = {
                "book": book,
                "fetched_at": max(stamps),
                **out,
            }
            n += 1
    return n


# ---------- results (settlement) ----------


def corner_results(leagues, raw_dir: Path = RAW_DIR, seasons=None) -> pd.DataFrame:
    """Played matches with corner counts for these leagues (cached football-data files;
    file reads only). Columns: league, season, date, home, away, home_corners,
    away_corners."""
    frames = [corner_matches(lg, raw_dir, seasons) for lg in leagues]
    frames = [f for f in frames if not f.empty]
    if not frames:
        return corner_matches("", raw_dir, [])
    return pd.concat(frames, ignore_index=True)


# ---------- calibration of every quoted line (owner, 22 Oct) ----------

QUOTE_CAL_NOTE = (
    "Every logged team-corner line, not only the trades: the newest quote per team and "
    "line downloaded before kickoff, the model (f)'s chance of the over recorded with it "
    "(p_model_over) and the book's margin-free over (fair_over; none on a one-sided "
    "DraftKings line), against the team's corner count. Half lines only: a whole line can "
    "push, and the logged rows carry no push chance, so whole lines are counted apart. "
    "Matches without a corner count yet are counted as unsettled."
)


def _result_index(*frames) -> dict[tuple, list[tuple]]:
    """(league, home, away) -> [(date, home corners, away corners)] from result frames
    (corner_results, espn_results rows); rows without both counts are skipped."""
    out: dict[tuple, list[tuple]] = {}
    for df in frames:
        if df is None or df.empty or "home_corners" not in df:
            continue
        for r in df.to_dict("records"):
            hc, ac = r.get("home_corners"), r.get("away_corners")
            if hc is None or ac is None or pd.isna(hc) or pd.isna(ac):
                continue
            d = pd.Timestamp(r.get("kickoff") or r.get("date"))
            d = (d.tz_convert(None) if d.tzinfo else d).normalize()
            out.setdefault((r.get("league"), r["home"], r["away"]), []).append(
                (d, int(hc), int(ac))
            )
    return out


def _count(index: dict, q: dict) -> int | None:
    """The quoted team's corner count for the quote's match (result dated within a day of
    the kickoff), else None."""
    k = _ts(q["kickoff"]).tz_convert(None).normalize()
    for d, hc, ac in index.get((q.get("league"), q["home"], q["away"]), []):
        if abs((d - k).days) <= 1:
            return hc if q["side"] == "home" else ac
    return None


def quote_calibration(rows: list[dict], *results, books=BOOKS) -> dict:
    """Per book: {quotes, settled, unsettled, whole_lines, pushes, matches, model, price}
    over each team line's newest pre-kickoff quote (newest_quotes): `model` =
    calibration.reliability of p_model_over (implied: fair_over where the book has both
    sides) and `price` = fair_over, both against over won (count > line). Clustered by
    match. `results` are frames with league, home, away, date (or kickoff) and
    home/away_corners (corner_results, ESPN rows)."""
    from soccer_stats import calibration as cal

    index = _result_index(*results)
    out = {"note": QUOTE_CAL_NOTE}
    for book in books:
        quotes = list(newest_quotes(rows, book=book).values())
        keep, unsettled, whole, pushes = [], 0, 0, 0
        for q in quotes:
            n = _count(index, q)
            line = float(q["line"])
            if n is None:
                unsettled += 1
            elif line == int(line):
                whole += 1
                pushes += int(n == line)
            else:
                keep.append((q, 1.0 if n > line else 0.0))

        def f(q, k):
            v = q.get(k)
            return float(v) if v is not None else np.nan

        group = [f"{q.get('league')}|{q['home']}|{q['away']}|{q['kickoff']}" for q, _ in keep]
        won = [w for _, w in keep]
        model = [f(q, "p_model_over") for q, _ in keep]
        price = [f(q, "fair_over") for q, _ in keep]
        out[book] = {
            "quotes": len(quotes),
            "settled": len(keep),
            "unsettled": unsettled,
            "whole_lines": whole,
            "pushes": pushes,
            "matches": len(set(group)),
            "model": cal.reliability(model, won, price, groups=group),
            "price": cal.reliability(price, won, groups=group),
        }
    return out


# ---------- credit estimate ----------


def estimate(kickoffs: dict, start: pd.Timestamp, end: pd.Timestamp) -> dict:
    """Credits a month for the base plan (24 h look + close), one credit a call, per
    league: odds_feed.estimate_snapshot_credits on each league's calendar."""
    from soccer_stats.odds_feed import estimate_snapshot_credits

    out = {}
    for code, ks in kickoffs.items():
        e = estimate_snapshot_credits(code, ks, start, end, "base", markets=COST_PER_CALL)
        out[code] = {"matches": e["matches"], "calls": e["calls"], "credits": e["credits"]}
    return out
