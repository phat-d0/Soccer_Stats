"""Walk-forward backtest: refit periodically, predict only future matches, compare to market.

Two questions matter, in this order:
1. Calibration: is the model's log loss close to (or better than) the de-vigged closing line?
2. Value: betting at the available (opening) price where the model sees an edge,
   do we get positive ROI and, more reliably, positive closing line value (CLV)?
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from soccer_stats import trades as tr
from soccer_stats.markets import match_odds, over_under
from soccer_stats.models import DixonColes
from soccer_stats.odds import devig_shin

OUTCOMES = ["home", "draw", "away"]


def walk_forward(
    matches: pd.DataFrame,
    start: str | pd.Timestamp,
    refit_every: str = "7D",
    lookback_days: int = 730,
    min_team_matches: int = 6,
    model_factory=DixonColes,
) -> pd.DataFrame:
    """Predict 1X2 probabilities for every match on/after `start` using only prior data.

    Matches involving a team with fewer than `min_team_matches` games in the
    training window are skipped (ratings for them are mostly guesswork).
    """
    matches = matches.sort_values("date").reset_index(drop=True)
    start = pd.Timestamp(start)
    windows = pd.date_range(start, matches["date"].max() + pd.Timedelta(days=1), freq=refit_every)

    out = []
    for lo, hi in zip(windows[:-1], windows[1:], strict=True):
        test = matches[(matches["date"] >= lo) & (matches["date"] < hi)]
        if test.empty:
            continue
        train = matches[
            (matches["date"] < lo) & (matches["date"] >= lo - pd.Timedelta(days=lookback_days))
        ]
        counts = pd.concat([train["home"], train["away"]]).value_counts()
        model = model_factory().fit(train, as_of=lo)

        for row in test.itertuples(index=False):
            if min(counts.get(row.home, 0), counts.get(row.away, 0)) < min_team_matches:
                continue
            m = model.score_matrix(row.home, row.away)
            p = match_odds(m)
            over = over_under(m, 2.5)[0]
            exp_h, exp_a = model.expected_goals(row.home, row.away)
            out.append(
                {
                    **row._asdict(),
                    "exp_home": exp_h,
                    "exp_away": exp_a,
                    "p_home": p[0],
                    "p_draw": p[1],
                    "p_away": p[2],
                    "p_over25": over,
                    "p_under25": 1 - over,
                }
            )
    return pd.DataFrame(out)


def add_market_probs(preds: pd.DataFrame, prefix: str = "close") -> pd.DataFrame:
    """Add de-vigged market probabilities (mkt_home/draw/away) from `{prefix}_*` odds."""
    df = preds.copy()
    cols = [f"{prefix}_{o}" for o in OUTCOMES]
    probs = np.full((len(df), 3), np.nan)
    ok = df[cols].notna().all(axis=1).to_numpy()
    for i in np.flatnonzero(ok):
        probs[i] = devig_shin(df[cols].iloc[i].to_numpy(dtype=float))
    df[["mkt_home", "mkt_draw", "mkt_away"]] = probs
    return df


def _result_index(df: pd.DataFrame) -> np.ndarray:
    return np.select(
        [df["home_goals"] > df["away_goals"], df["home_goals"] == df["away_goals"]], [0, 1], 2
    )


def score(preds: pd.DataFrame) -> pd.DataFrame:
    """Log loss and Brier score for the model and (if present) the market."""
    df = preds.dropna(subset=["mkt_home"]) if "mkt_home" in preds else preds
    y = np.eye(3)[_result_index(df)]
    rows = {}
    for name, cols in [
        ("model", ["p_home", "p_draw", "p_away"]),
        ("market", ["mkt_home", "mkt_draw", "mkt_away"]),
    ]:
        if not set(cols) <= set(df.columns):
            continue
        p = df[cols].to_numpy()
        rows[name] = {
            "log_loss": float(-np.mean(np.log(np.clip((p * y).sum(1), 1e-12, None)))),
            "brier": float(np.mean(((p - y) ** 2).sum(1))),
            "n": len(df),
        }
    return pd.DataFrame(rows).T


def simulate_bets(
    preds: pd.DataFrame, min_edge: float = 0.03, price_prefix: str = "odds", max_odds: float = 6.0
) -> pd.DataFrame:
    """Flat 1-unit bets wherever model edge at `price_prefix` odds exceeds `min_edge`.

    Returns one row per bet with profit and CLV versus the de-vigged closing line.
    """
    res = _result_index(preds)
    bets = []
    for i, o in enumerate(OUTCOMES):
        price = preds[f"{price_prefix}_{o}"]
        p = preds[f"p_{o}"]
        e = p * price - 1
        mask = (e > min_edge) & (price <= max_odds) & price.notna()
        sel = preds[mask]
        won = res[mask.to_numpy()] == i
        b = pd.DataFrame(
            {
                "date": sel["date"],
                "home": sel["home"],
                "away": sel["away"],
                "pick": o,
                "price": price[mask],
                "model_p": p[mask],
                "edge": e[mask],
                "profit": np.where(won, price[mask] - 1, -1.0),
            }
        )
        if f"mkt_{o}" in preds:
            b["clv"] = price[mask] * preds.loc[mask, f"mkt_{o}"] - 1
        bets.append(b)
    return pd.concat(bets).sort_values("date").reset_index(drop=True)


def summarize_bets(bets: pd.DataFrame) -> dict[str, float]:
    if bets.empty:
        return {"bets": 0}
    out = {
        "bets": len(bets),
        "staked": float(len(bets)),
        "profit": float(bets["profit"].sum()),
        "roi": float(bets["profit"].mean()),
        "avg_odds": float(bets["price"].mean()),
        "avg_edge": float(bets["edge"].mean()),
    }
    if "clv" in bets:
        out["avg_clv"] = float(bets["clv"].mean())
        out["pct_beat_close"] = float((bets["clv"] > 0).mean())
    return out


# ---------- DraftKings backtest (historical Odds API snapshots) ----------

ODDS_KEYS = {m: f"odds_{m}" for m in tr.MARKETS}


class _Refits:
    """Weekly refits; a look at time t uses the latest refit on or before t's date, fitted
    only on matches played before that refit date (so never on anything after t)."""

    def __init__(self, matches, start, refit_every, lookback_days, model_factory):
        self.matches = matches.sort_values("date").reset_index(drop=True)
        start = pd.Timestamp(start)
        start = start.tz_convert(None) if start.tz is not None else start
        self.dates = pd.date_range(start.normalize(), periods=600, freq=refit_every)
        self.lookback = pd.Timedelta(days=lookback_days)
        self.factory = model_factory
        self.cache: dict = {}

    def model_for(self, at: pd.Timestamp):
        day = pd.Timestamp(at).tz_convert(None).normalize()
        i = self.dates.searchsorted(day, side="right") - 1
        if i < 0:
            return None, None
        lo = self.dates[i]
        if lo not in self.cache:
            m = self.matches
            train = m[(m["date"] < lo) & (m["date"] >= lo - self.lookback)]
            counts = pd.concat([train["home"], train["away"]]).value_counts()
            model = self.factory().fit(train, as_of=lo) if len(train) else None
            self.cache[lo] = (model, counts)
        return self.cache[lo]


def _group_prices(row: dict | None) -> dict:
    return {m: (row or {}).get(ODDS_KEYS[m]) for m in tr.MARKETS}


def dk_candidates(
    matches: pd.DataFrame,
    history: pd.DataFrame,
    start: str | pd.Timestamp,
    looks_hours: tuple[float, ...] = (48.0, 3.0),
    refit_every: str = "7D",
    lookback_days: int = 730,
    model_factory=DixonColes,
    now: pd.Timestamp | None = None,
) -> pd.DataFrame:
    """One row per (match, look) with model probabilities and the DraftKings price then.

    Matches are the DraftKings events in `history` kicking off on or after `start`;
    the kickoff is the one listed in the event's latest snapshot. The close (last price
    before kickoff), the result and Pinnacle's close from football-data come along for
    settlement. Every price comes from a snapshot at or before its look time.
    """
    from soccer_stats.odds_history import CLOSE_MINUTES, price_at

    if history.empty:
        return pd.DataFrame()
    start = pd.Timestamp(start, tz="UTC") if pd.Timestamp(start).tz is None else start
    now = now or pd.Timestamp.now(tz="UTC")
    refits = _Refits(matches, start, refit_every, lookback_days, model_factory)
    results = matches.copy()
    results["season"] = results["season"].astype(str)

    h = history.copy()
    h["season"] = [tr.season_label(k) for k in h["kickoff"]]
    events = h.sort_values("snapshot_ts").groupby(["home", "away", "season"]).tail(1)
    events = events[(events["kickoff"] >= start) & (events["kickoff"] < now)]

    rows = []
    for ev in events.sort_values("kickoff").itertuples(index=False):
        kickoff = ev.kickoff
        res = results[
            (results["home"] == ev.home)
            & (results["away"] == ev.away)
            & (results["season"] == ev.season)
        ]
        res = res.iloc[0].to_dict() if not res.empty else None
        if res is None and kickoff.tz_convert(None) > results["date"].max() + pd.Timedelta(days=1):
            continue  # not played yet (or results not out): nothing to settle
        moved = (
            res is not None and abs((res["date"] - kickoff.tz_convert(None).normalize()).days) > 2
        )
        close = price_at(
            history,
            ev.home,
            ev.away,
            kickoff,
            kickoff - pd.Timedelta(minutes=CLOSE_MINUTES),
            stale_hours=None,
        )
        for hrs in looks_hours:
            at = kickoff - pd.Timedelta(hours=hrs)
            snap = price_at(history, ev.home, ev.away, kickoff, at)
            model, counts = refits.model_for(at)
            if snap is None or model is None:
                continue
            if ev.home not in model.attack or ev.away not in model.attack:
                continue
            mtx = model.score_matrix(ev.home, ev.away)
            p = match_odds(mtx)
            over = over_under(mtx, 2.5)[0]
            rows.append(
                {
                    "league": res["league"] if res else None,
                    "season": ev.season,
                    "home": ev.home,
                    "away": ev.away,
                    "kickoff": kickoff,
                    "look": f"{hrs:g}h",
                    "look_at": at,
                    "odds_fetched_at": pd.Timestamp(snap["snapshot_ts"]).isoformat(),
                    "home_n": int(counts.get(ev.home, 0)),
                    "away_n": int(counts.get(ev.away, 0)),
                    "p_home": p[0],
                    "p_draw": p[1],
                    "p_away": p[2],
                    "p_over25": over,
                    "p_under25": 1 - over,
                    **{ODDS_KEYS[m]: snap.get(ODDS_KEYS[m]) for m in tr.MARKETS},
                    "close": _group_prices(close),
                    "close_fetched_at": (
                        pd.Timestamp(close["snapshot_ts"]).isoformat() if close else None
                    ),
                    "pinnacle_close": {
                        m: res.get(f"close_{m}") if res else None for m in tr.MARKETS
                    },
                    "home_goals": res["home_goals"] if res else None,
                    "away_goals": res["away_goals"] if res else None,
                    "void": res is None or moved,
                    "matches_fit": len(refits.matches),
                }
            )
    return pd.DataFrame(rows)


def dk_trades(
    candidates: pd.DataFrame,
    threshold: float = tr.PAPER_EDGE,
    max_odds: float | None = None,
    league: str = "E0",
    model_ref: dict | None = None,
) -> pd.DataFrame:
    """Apply the trade rule at each look in turn; a match trades at its first qualifying
    look and never again. Trades are settled and given closing line value."""
    if candidates.empty:
        return pd.DataFrame(columns=tr.ENTRY_FIELDS)
    order = {k: i for i, k in enumerate(dict.fromkeys(candidates["look"]))}
    picked = tr.select_trades(candidates, threshold=threshold, max_odds=max_odds)
    if picked.empty:
        return pd.DataFrame(columns=tr.ENTRY_FIELDS)
    picked = picked.assign(_o=picked["look"].map(order)).sort_values(["kickoff", "_o"])
    first = picked.groupby(["home", "away", "season"], sort=False).head(1)
    out = []
    for r in first.to_dict("records"):
        t = tr.new_trade(
            r,
            source="backtest",
            league=r.get("league") or league,
            home=r["home"],
            away=r["away"],
            kickoff=r["kickoff"],
            opened_at=r["look_at"],
            odds_fetched_at=r["odds_fetched_at"],
            threshold=threshold,
            model_p_base=r["model_p"],
            news_applied=False,  # team news history starts Oct 2026: not replayable
            model_ref=model_ref,
            look=r["look"],
        )
        close = r["close"]
        t["close_odds"] = close.get(t["market"])
        t["close_fetched_at"] = r["close_fetched_at"]
        t["clv_dk"] = tr.clv(t["odds"], t["market"], close)
        t["clv_pinnacle"] = tr.clv(t["odds"], t["market"], r["pinnacle_close"])
        t.update(tr.settle(t, r["home_goals"], r["away_goals"], void=r["void"]))
        t["settled_at"] = t["kickoff"]
        out.append(t)
    return pd.DataFrame(out)


def dk_log_loss(candidates: pd.DataFrame) -> dict:
    """Model log loss beside DraftKings' (margin-free close) on the same matches."""
    if candidates.empty:
        return {}
    df = candidates.drop_duplicates(["home", "away", "season"], keep="last")
    df = df[~df["void"]]
    rows = []
    for r in df.to_dict("records"):
        c = r["close"]
        if not all(c.get(m) and c.get(m) > 1 for m in ("home", "draw", "away")):
            continue
        hg, ag = r["home_goals"], r["away_goals"]
        y = 0 if hg > ag else 1 if hg == ag else 2
        mk = devig_shin(np.array([c["home"], c["draw"], c["away"]], dtype=float))
        pm = [r["p_home"], r["p_draw"], r["p_away"]]
        rows.append((-np.log(max(pm[y], 1e-12)), -np.log(max(mk[y], 1e-12))))
    if not rows:
        return {}
    a = np.array(rows)
    return {"matches": len(a), "model": float(a[:, 0].mean()), "draftkings": float(a[:, 1].mean())}
