"""Line shopping on match markets with football-data.co.uk bookmaker prices.

football-data's season CSVs carry, per match, the 1X2 and over/under 2.5 prices of
several bookmakers at two times: "early" (collected about one to three days before
kickoff; football-data calls these the pre-closing odds) and "close" (the last price
before kickoff). They also carry the market maximum (Max) and average (Avg) across the
books its source tracks. Max is an upper bound on line shopping: some of those books
are offshore, limit winners or are stale.

Everything here works on frames already in memory; nothing reads the network.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from soccer_stats import trades as tr
from soccer_stats.edge.stats import bootstrap_mean
from soccer_stats.odds import devig_shin

MARKETS = tr.MARKETS  # home, draw, away, over25, under25
SUFFIX = {"home": "H", "draw": "D", "away": "A"}
OU = {"over25": ">2.5", "under25": "<2.5"}
WHEN = {"early": "", "close": "C"}

# book -> (1X2 column prefix, over/under column prefix or None)
BOOKS = {
    "pinnacle": ("PS", "P"),
    "bet365": ("B365", "B365"),
    "betfair_ex": ("BFE", "BFE"),
    "betfair_sb": ("BF", None),
    "williamhill": ("WH", None),
    "bwin": ("BW", None),
    "interwetten": ("IW", None),
    "betvictor": ("VC", None),
    "1xbet": ("1XB", None),
    "max": ("Max", "Max"),
    "avg": ("Avg", "Avg"),
}
# Single books a bettor could hold accounts with; "best_named" is the best of these.
NAMED = ("pinnacle", "bet365", "betfair_sb", "williamhill", "bwin", "interwetten", "betvictor")
EXCHANGE_COMMISSION = {"betfair_ex": 0.05}  # charged on net winnings


def column(book: str, when: str, market: str) -> str | None:
    """football-data column for a book's price, e.g. ('pinnacle', 'close', 'home') -> PSCH."""
    p1x2, pou = BOOKS[book]
    c = WHEN[when]
    if market in SUFFIX:
        return f"{p1x2}{c}{SUFFIX[market]}"
    return f"{pou}{c}{OU[market]}" if pou else None


def book_prices(raw: pd.DataFrame, season: str | None = None) -> pd.DataFrame:
    """One row per played match: result plus `{book}_{when}_{market}` decimal prices.

    Exchange prices are net of commission. Missing books are NaN. Adds `best_named`,
    the best price across NAMED books at each time.
    """
    df = raw.dropna(subset=["HomeTeam", "AwayTeam", "FTHG", "FTAG"]).copy()
    out = pd.DataFrame(
        {
            "date": pd.to_datetime(df["Date"], dayfirst=True, format="mixed"),
            "home": df["HomeTeam"],
            "away": df["AwayTeam"],
            "home_goals": df["FTHG"].astype(int),
            "away_goals": df["FTAG"].astype(int),
        }
    )
    out["season"] = season
    for col, src in (("home_shots", "HS"), ("away_shots", "AS")):
        out[col] = pd.to_numeric(df[src], errors="coerce") if src in df else np.nan
    for col, src in (("home_sot", "HST"), ("away_sot", "AST")):
        out[col] = pd.to_numeric(df[src], errors="coerce") if src in df else np.nan
    cols = {}
    for book in BOOKS:
        fee = EXCHANGE_COMMISSION.get(book, 0.0)
        for when in WHEN:
            for m in MARKETS:
                c = column(book, when, m)
                v = pd.to_numeric(df[c], errors="coerce") if c and c in df else np.nan
                v = pd.Series(v, index=df.index, dtype=float)
                v = v.where(v > 1)
                cols[f"{book}_{when}_{m}"] = 1 + (v - 1) * (1 - fee)
    px = pd.DataFrame(cols, index=df.index)
    best = {
        f"best_named_{when}_{m}": px[[f"{b}_{when}_{m}" for b in NAMED]].max(axis=1, skipna=True)
        for when in WHEN
        for m in MARKETS
    }
    out = pd.concat([out, px, pd.DataFrame(best, index=df.index)], axis=1)
    return out.reset_index(drop=True)


def all_books() -> list[str]:
    return [*BOOKS, "best_named"]


def margins(prices: pd.DataFrame) -> pd.DataFrame:
    """Average overround (sum of 1/odds minus 1) per book, time and market group.

    Rows where any side is missing are skipped. Max and best_named can be negative:
    taking the best side from different books can add up to under 100%.
    """
    rows = []
    for book in all_books():
        for when in WHEN:
            for group, ms in (("1x2", MARKETS[:3]), ("ou25", MARKETS[3:])):
                cols = [f"{book}_{when}_{m}" for m in ms]
                if not set(cols) <= set(prices.columns):
                    continue
                sub = prices[cols].dropna()
                if sub.empty:
                    continue
                over = (1 / sub).sum(axis=1) - 1
                rows.append(
                    {
                        "book": book,
                        "when": when,
                        "market": group,
                        "matches": len(sub),
                        "margin": float(over.mean()),
                        "margin_median": float(over.median()),
                    }
                )
    return pd.DataFrame(rows)


def add_fair_close(prices: pd.DataFrame, book: str = "pinnacle") -> pd.DataFrame:
    """fair_<market>: the book's closing price with its margin removed (Shin)."""
    df = prices.copy()
    for ms in (MARKETS[:3], MARKETS[3:]):
        cols = [f"{book}_close_{m}" for m in ms]
        fair = np.full((len(df), len(ms)), np.nan)
        vals = df[cols].to_numpy(dtype=float)
        for i in np.flatnonzero(np.isfinite(vals).all(axis=1)):
            fair[i] = devig_shin(vals[i])
        for j, m in enumerate(ms):
            df[f"fair_{m}"] = fair[:, j]
    return df


def join(preds: pd.DataFrame, prices: pd.DataFrame) -> pd.DataFrame:
    """Walk-forward predictions (date, home, away, p_*) beside every book's prices."""
    keep = ["date", "home", "away", *[f"p_{m}" for m in MARKETS]]
    p = preds[keep].copy()
    p["date"] = pd.to_datetime(p["date"]).dt.normalize()
    q = prices.copy()
    q["date"] = pd.to_datetime(q["date"]).dt.normalize()
    return p.merge(q, on=["date", "home", "away"], how="inner")


def replay(
    joined: pd.DataFrame,
    book: str,
    when: str,
    threshold: float = tr.PAPER_EDGE,
    max_odds: float | None = None,
) -> pd.DataFrame:
    """The match trade rule at one book's prices: one bet per match, 1 unit each.

    Returns one row per bet with profit and CLV against the fair Pinnacle close
    (`fair_<market>` from add_fair_close; NaN where Pinnacle has no close).
    """
    cand = joined.copy()
    for m in MARKETS:
        cand[f"odds_{m}"] = cand.get(f"{book}_{when}_{m}", np.nan)
    picked = tr.select_trades(cand, threshold=threshold, max_odds=max_odds)
    if picked.empty:
        return pd.DataFrame(columns=["date", "season", "market", "odds", "edge", "profit", "clv"])
    won = np.array(
        [
            tr.won(m, h, a)
            for m, h, a in zip(
                picked["market"], picked["home_goals"], picked["away_goals"], strict=True
            )
        ]
    )
    fair = np.array(
        [r.get(f"fair_{r['market']}", np.nan) for r in picked.to_dict("records")], dtype=float
    )
    out = picked[["date", "season", "home", "away", "market", "odds", "model_p", "edge"]].copy()
    out["won"] = won
    out["profit"] = np.where(won, out["odds"] - 1, -1.0)
    out["clv"] = out["odds"] * fair - 1
    out["book"], out["when"] = book, when
    return out.reset_index(drop=True)


def summarize(bets: pd.DataFrame, seed: int = 0) -> dict:
    """Bets, ROI with a bootstrap 95% range, average odds and CLV (with its range)."""
    if bets.empty:
        return {"bets": 0}
    clv = bets["clv"].dropna()
    return {
        "bets": len(bets),
        "roi": float(bets["profit"].mean()),
        "roi_ci95": bootstrap_mean(bets["profit"], seed=seed),
        "avg_odds": float(bets["odds"].mean()),
        "win_rate": float(bets["won"].mean()),
        "clv": float(clv.mean()) if len(clv) else None,
        "clv_ci95": bootstrap_mean(clv, seed=seed) if len(clv) > 1 else None,
        "beat_close": float((clv > 0).mean()) if len(clv) else None,
    }


def shots_check(prices: pd.DataFrame, apps: pd.DataFrame) -> dict:
    """Understat's team shot counts beside football-data's (HS/AS, HST/AST).

    `apps` is player_data.load_appearances output. A systematic gap would mean player
    shot bets settle on a different count from the one the model learns.
    """
    if apps.empty:
        return {}
    a = apps.copy()
    a["date"] = pd.to_datetime(a["kickoff"]).dt.tz_convert(None).dt.normalize()
    team = a.groupby(["date", "team", "home"]).agg(shots=("shots", "sum"), sot=("sot", "sum"))
    team = team.reset_index()
    is_home = team.pop("home")
    h = team[is_home].rename(columns={"team": "home", "shots": "u_hs", "sot": "u_hst"})
    w = team[~is_home].rename(columns={"team": "away", "shots": "u_as", "sot": "u_ast"})
    q = prices.copy()
    q["date"] = pd.to_datetime(q["date"]).dt.normalize()
    m = q.merge(h, on=["date", "home"]).merge(w, on=["date", "away"])
    m = m.dropna(subset=["home_shots", "away_shots", "home_sot", "away_sot"])
    if m.empty:
        return {}
    fd_s = np.r_[m["home_shots"], m["away_shots"]]
    us_s = np.r_[m["u_hs"], m["u_as"]]
    fd_t = np.r_[m["home_sot"], m["away_sot"]]
    us_t = np.r_[m["u_hst"], m["u_ast"]]
    return {
        "team_matches": len(fd_s),
        "shots_football_data": float(fd_s.mean()),
        "shots_understat": float(us_s.mean()),
        "shots_ratio": float(us_s.sum() / fd_s.sum()),
        "shots_exact": float((fd_s == us_s).mean()),
        "sot_football_data": float(fd_t.mean()),
        "sot_understat": float(us_t.mean()),
        "sot_ratio": float(us_t.sum() / fd_t.sum()),
        "sot_exact": float((fd_t == us_t).mean()),
    }
