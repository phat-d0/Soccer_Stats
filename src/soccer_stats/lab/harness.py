"""Walk-forward evaluation with nested, time-ordered tuning and a locked holdout.

A *candidate* is any object with

    fit(train: DataFrame) -> None
    predict(test: DataFrame) -> ndarray (n, K)      # chances per outcome, rows sum to 1

made by `factory(**params)`. The data is one row per event (a match, or a player line)
with a time column; a row's outcome is only known after its time, so a model refitted
at time t may only learn from rows with time < t.

`walk_forward` refits every `refit` (e.g. 28 days) on all earlier rows and predicts the
next block. `nested` picks the parameters for each period (a season) by running the
same walk-forward over the previous period, trained only on rows before it, and keeps
the best log loss; the period being scored is never used to choose anything.

The holdout is locked: any attempt to predict a row at or after `Holdout.start` raises
`HoldoutLocked` unless `Holdout.unlock(reason)` was called, which is printed and logged.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd


class HoldoutLocked(RuntimeError):
    """Raised when code tries to score the locked holdout."""


@dataclass
class Holdout:
    start: pd.Timestamp
    unlocked: bool = False
    log_path: Path | None = None
    events: list[str] = field(default_factory=list)

    def __post_init__(self):
        self.start = pd.Timestamp(self.start)

    def unlock(self, reason: str) -> None:
        if not reason or not reason.strip():
            raise ValueError("Opening the holdout needs a reason.")
        stamp = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        line = f"HOLDOUT OPENED at {stamp} (rows from {self.start.date()}): {reason.strip()}"
        self.unlocked = True
        self.events.append(line)
        print("=" * len(line) + f"\n{line}\n" + "=" * len(line), flush=True)
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a") as f:
                f.write(line + "\n")

    def check(self, times: Iterable) -> None:
        t = pd.to_datetime(pd.Series(list(times)))
        if not self.unlocked and len(t) and (t >= self.start).any():
            raise HoldoutLocked(
                f"{int((t >= self.start).sum())} rows fall in the locked holdout "
                f"(from {self.start.date()}). Call Holdout.unlock(reason) to score them."
            )

    def development(self, df: pd.DataFrame, time_col: str = "time") -> pd.DataFrame:
        """The rows before the holdout (safe to use for anything)."""
        return df[pd.to_datetime(df[time_col]) < self.start]


def blocks(start, end, freq: str = "28D") -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """Consecutive [lo, hi) windows from `start` that cover up to `end`."""
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    edges = list(pd.date_range(start, end, freq=freq))
    if not edges or edges[-1] < end:
        edges.append(edges[-1] + pd.Timedelta(freq) if edges else end)
    return list(zip(edges[:-1], edges[1:], strict=True))


def walk_forward(
    data: pd.DataFrame,
    factory: Callable,
    params: dict,
    start,
    end,
    *,
    holdout: Holdout | None = None,
    time_col: str = "time",
    refit: str = "28D",
    min_train: int = 200,
    train_from=None,
) -> pd.DataFrame:
    """Out-of-sample chances p_0..p_{K-1} for rows with start <= time < end.

    Each block is predicted by a model fitted on rows with time < block start (and
    >= `train_from` when given). Blocks with fewer than `min_train` earlier rows are
    skipped (no prediction). Returns a frame indexed like the predicted rows.
    """
    t = pd.to_datetime(data[time_col])
    out = []
    for lo, hi in blocks(start, end, refit):
        hi = min(hi, pd.Timestamp(end))
        test = data[(t >= lo) & (t < hi)]
        if test.empty:
            continue
        if holdout is not None:
            holdout.check(test[time_col])
        keep = t < lo
        if train_from is not None:
            keep &= t >= pd.Timestamp(train_from)
        train = data[keep]
        if len(train) < min_train:
            continue
        model = factory(**params)
        model.fit(train)
        p = np.asarray(model.predict(test), dtype=float)
        out.append(pd.DataFrame(p, index=test.index, columns=[f"p_{k}" for k in range(p.shape[1])]))
    if not out:
        return pd.DataFrame()
    return pd.concat(out)


def mean_log_loss(p: np.ndarray, y: np.ndarray) -> float:
    p = np.clip(np.asarray(p, dtype=float), 1e-12, 1.0)
    return float(-np.log(p[np.arange(len(y)), np.asarray(y, dtype=int)]).mean())


def nested(
    data: pd.DataFrame,
    factory: Callable,
    grid: list[dict],
    periods: list,
    *,
    period_col: str = "season",
    outcome_col: str = "y",
    holdout: Holdout | None = None,
    time_col: str = "time",
    refit: str = "28D",
    tune_refit: str = "84D",
    train_from=None,
    min_train: int = 200,
) -> tuple[pd.DataFrame, list[dict]]:
    """Walk-forward over `periods` with parameters tuned on the period before each.

    For period P (rows with period_col == P, in time order), every config in `grid` is
    run walk-forward over the previous period, trained only on rows before it, refitting
    every `tune_refit`; the config with the lowest log loss there is used for P. With one
    config there is nothing to tune. Returns (predictions, one record per period).
    """
    t = pd.to_datetime(data[time_col])
    order = sorted(data[period_col].dropna().unique())
    preds, chosen = [], []
    for p in periods:
        rows = data[data[period_col] == p]
        if rows.empty:
            continue
        lo, hi = t[rows.index].min(), t[rows.index].max() + pd.Timedelta(days=1)
        params, scores = grid[0], {}
        prev = [q for q in order if q < p]
        if len(grid) > 1 and prev:
            pv = data[data[period_col] == prev[-1]]
            vlo, vhi = t[pv.index].min(), t[pv.index].max() + pd.Timedelta(days=1)
            for i, cfg in enumerate(grid):
                pr = walk_forward(
                    data[t < vhi],
                    factory,
                    cfg,
                    vlo,
                    vhi,
                    time_col=time_col,
                    refit=tune_refit,
                    train_from=train_from,
                    min_train=min_train,
                )
                if pr.empty:
                    continue
                y = data.loc[pr.index, outcome_col].to_numpy()
                scores[i] = mean_log_loss(pr.to_numpy(), y)
            if scores:
                params = grid[min(scores, key=scores.get)]
        pr = walk_forward(
            data[t < hi],
            factory,
            params,
            lo,
            hi,
            holdout=holdout,
            time_col=time_col,
            refit=refit,
            train_from=train_from,
            min_train=min_train,
        )
        if not pr.empty:
            preds.append(pr)
        chosen.append(
            {"period": p, "params": params, "tune_log_loss": {str(k): v for k, v in scores.items()}}
        )
    return (pd.concat(preds) if preds else pd.DataFrame()), chosen
