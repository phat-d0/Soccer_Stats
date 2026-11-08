"""Shared base of the model tuning programme (docs/lab.md "Model tuning programme").

`walk` runs the walk-forward Dixon-Coles for any configuration (xi, xG weight, window,
minimum team matches) exactly as `backtest.walk_forward` does at today's defaults (a test
checks it), and keeps beside the out-of-sample chances each refit's in-sample log loss and
team ratings. `load_league` reads a league's football-data matches with Understat xG and
Pinnacle's own early and close prices; nothing at or after LOCK is ever loaded.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from soccer_stats.lab.harness import Holdout
from soccer_stats.markets import match_odds
from soccer_stats.models import DixonColes
from soccer_stats.odds import devig_shin

LEAGUES = ("E0", "SP1", "D1", "I1", "F1", "E1", "E2", "E3")
XG_LEAGUES = ("E0", "SP1", "D1", "I1", "F1")
SIDES = ("home", "draw", "away")
LOCK = pd.Timestamp("2024-07-01")
SCORED_FROM = pd.Timestamp("2019-07-01")
BURN_FROM = pd.Timestamp("2017-07-01")
LAST_YEAR = 2023
N_BOOT = 2000
SEED = 0


@dataclass(frozen=True)
class Config:
    xi: float = 0.0019
    xg_weight: float = 0.7
    window: int = 730
    min_team: int = 6
    ridge: float = 0.0  # DixonColes' ratings ridge (0 = none, the live model)

    def key(self) -> str:
        return (f"xi{self.xi:g}_xg{self.xg_weight:g}_w{self.window}_m{self.min_team}"
                f"_r{self.ridge:g}")  # fmt: skip


DEFAULT = Config()


def default_for(code: str) -> Config:
    return DEFAULT if code in XG_LEAGUES else Config(xg_weight=0.0)


def log_loss_rows(p: np.ndarray, y: np.ndarray) -> np.ndarray:
    p = np.asarray(p, float)
    return -np.log(np.clip(p[np.arange(len(y)), np.asarray(y, int)], 1e-15, None))


def outcome(home_goals, away_goals) -> np.ndarray:
    hg, ag = np.asarray(home_goals, float), np.asarray(away_goals, float)
    return np.select([hg > ag, hg == ag], [0, 1], 2)


def _probs(model: DixonColes, home, away) -> np.ndarray:
    return np.vstack(
        [match_odds(model.score_matrix(h, a)) for h, a in zip(home, away, strict=True)]
    )


def walk(
    matches: pd.DataFrame,
    cfg: Config,
    start=BURN_FROM,
    end=LOCK,
    refit: str = "7D",
    insample_every: int = 4,
    recent_days: int = 365,
    keep_ratings: bool = True,
    holdout: Holdout | None = None,
) -> tuple[pd.DataFrame, list[dict]]:
    """Out-of-sample 1X2 chances for matches in [start, end) and one record per refit.

    Same windows, training rows, team-count filter and model as backtest.walk_forward.
    Every `insample_every`-th refit also records the in-sample log loss on its training
    rows (all, and those of the last `recent_days`) and, with keep_ratings, every team's
    attack and defence (every refit)."""
    m = matches.sort_values("date").reset_index(drop=True)
    m = m[m["date"] < pd.Timestamp(end)]
    start = pd.Timestamp(start)
    windows = pd.date_range(start, m["date"].max() + pd.Timedelta(refit), freq=refit)
    out, refits = [], []
    for i, (lo, hi) in enumerate(zip(windows[:-1], windows[1:], strict=True)):
        test = m[(m["date"] >= lo) & (m["date"] < hi)]
        if test.empty:
            continue
        if holdout is not None:
            holdout.check(test["date"])
        train = m[(m["date"] < lo) & (m["date"] >= lo - pd.Timedelta(days=cfg.window))]
        if train.empty:  # nothing to fit (a gap longer than the window)
            continue
        counts = pd.concat([train["home"], train["away"]]).value_counts()
        model = DixonColes(xi=cfg.xi, xg_weight=cfg.xg_weight, ridge=cfg.ridge).fit(train, as_of=lo)
        ok = [
            min(counts.get(h, 0), counts.get(a, 0)) >= cfg.min_team
            for h, a in zip(test["home"], test["away"], strict=True)
        ]
        t = test[ok]
        if len(t):
            p = _probs(model, t["home"], t["away"])
            frame = t[["date", "season", "home", "away", "home_goals", "away_goals"]].copy()
            for k, s in enumerate(SIDES):
                frame[f"p_{s}"] = p[:, k]
            frame["refit"] = lo
            frame["n_train"] = len(train)
            out.append(frame)
        rec = {
            "as_of": lo,
            "n_train": int(len(train)),
            "home_adv": float(model.params["home_adv"]),
            "rho": float(model.params["rho"]),
        }
        if keep_ratings:
            rec["ratings"] = {
                t_: (float(model.attack[t_]), float(model.defence[t_])) for t_ in model.teams
            }
        if insample_every and i % insample_every == 0:
            if len(train):
                y = outcome(train["home_goals"], train["away_goals"])
                ll = log_loss_rows(_probs(model, train["home"], train["away"]), y)
                recent = (train["date"] >= lo - pd.Timedelta(days=recent_days)).to_numpy()
                rec.update(
                    {
                        "ll_in_all": float(ll.mean()),
                        "ll_in_recent": float(ll[recent].mean()) if recent.any() else None,
                        "n_recent": int(recent.sum()),
                    }
                )
        refits.append(rec)
    preds = pd.concat(out, ignore_index=True) if out else pd.DataFrame()
    if len(preds):
        preds["y"] = outcome(preds["home_goals"], preds["away_goals"])
    return preds, refits


def fair(df: pd.DataFrame, cols) -> np.ndarray:
    """Shin-fair chances (n, 3) where all three prices are > 1, else NaN rows."""
    v = (
        df[list(cols)].to_numpy(float)
        if set(cols) <= set(df.columns)
        else np.full((len(df), 3), np.nan)
    )
    ok = (np.isfinite(v) & (v > 1)).all(axis=1)
    out = np.full((len(df), 3), np.nan)
    if ok.any():
        out[ok] = np.vstack([devig_shin(r) for r in v[ok]])
    return out


EARLY = ("odds_home", "odds_draw", "odds_away")
CLOSE = ("close_home", "close_draw", "close_away")


def with_prices(preds: pd.DataFrame, pins: pd.DataFrame) -> pd.DataFrame:
    """preds plus Pinnacle's own early/close odds and their Shin-fair chances
    (q_early_<side>, q_close_<side>)."""
    keys = ["season", "home", "away"]
    pr = pins.drop(columns=[c for c in ("date",) if c in pins]).drop_duplicates(keys)
    pr = pr.assign(season=pr["season"].astype(str))
    out = preds.assign(season=preds["season"].astype(str)).merge(pr, on=keys, how="left")
    for name, cols in (("early", EARLY), ("close", CLOSE)):
        f = fair(out, cols)
        for k, s in enumerate(SIDES):
            out[f"q_{name}_{s}"] = f[:, k]
    return out


def boot_mean(v, n_boot: int = N_BOOT, seed: int = SEED) -> list[float] | None:
    v = np.asarray(v, float)
    v = v[np.isfinite(v)]
    if len(v) < 2:
        return None
    rng = np.random.default_rng(seed)
    means = v[rng.integers(0, len(v), (n_boot, len(v)))].mean(axis=1)
    return [float(np.quantile(means, 0.025)), float(np.quantile(means, 0.975))]


def load_league(code: str, first_year: int, holdout: Holdout, last: int = LAST_YEAR):
    """football-data matches first_year..last with Understat xG (NaN where none), and
    Pinnacle's own early and close prices. Raises if an Understat league gets no xG."""
    from soccer_stats import leagues as lgs
    from soccer_stats.data import load_matches, pinnacle_prices
    from soccer_stats.xg import with_xg

    years = range(first_year, last + 1)
    matches = load_matches([code], years)
    matches, err = with_xg(matches)
    if lgs.LEAGUES[code].understat and not matches["home_xg"].notna().any():
        raise RuntimeError(f"no Understat xG attached ({err})")
    matches = matches[matches["date"] < LOCK]
    holdout.check(matches["date"])
    pins = pinnacle_prices(code, years)
    pins = pins[pd.to_datetime(pins["date"]) < LOCK]
    holdout.check(pins["date"])
    xg_share = matches.groupby(matches["season"].astype(str))["home_xg"].apply(
        lambda v: round(float(v.notna().mean()), 3)
    )
    return matches, pins, {"xg_share": xg_share.to_dict(), "matches": int(len(matches))}


def config_dict(cfg: Config) -> dict:
    return asdict(cfg)
