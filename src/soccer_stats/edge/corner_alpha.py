"""Candidate "f-oos": model (f) with its NB2 dispersion estimated out of sample (corners
team, 8 Nov 2026; lead's go at low priority; docs/totals.md "Corner dispersion: f-oos").

Model evaluation's stage 1 (run 42461482443) found (f)'s NB2 too narrow out of sample:
dispersion under the fitted NB2 is 1.07-1.12 in every league, because alpha is
moment-matched on the same matches the team ratings were fitted on. f-oos keeps (f)'s
means exactly (`TeamCornersF.fit`, `rates`) and replaces alpha per side with the
moment-matched alpha of out-of-sample residuals: the last `OOS_DAYS` of matches before the
fit, each predicted by a (f) fit on matches before its own `BLOCK` (earlier kickoffs only).
A block whose inner fit fails (too few matches) is skipped; with fewer than `MIN_OOS`
predicted matches the in-sample alpha is kept (`alpha_source` says which).

Development only, 2017/18-2023/24, scored by Model evaluation beside (f) through
`lab.model_assumptions.corner_walk(..., model=CandidateFOos)`. Nothing live uses it.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from soccer_stats.corners_live import TeamCornersF, _ts
from soccer_stats.edge.corners import mom_alpha

OOS_DAYS = 365
BLOCK = "28D"
MIN_OOS = 200
NAME = "f-oos"


class CandidateFOos(TeamCornersF):
    """(f) with out-of-sample alpha. Same interface: fit, rates, pmfs, info, alpha_."""

    def fit(self, matches: pd.DataFrame, now=None):
        super().fit(matches, now)
        t = matches.copy()
        t["date"] = pd.to_datetime(t["date"])
        if now is not None:
            t = t[t["date"] < _ts(now).tz_convert(None)]
        t = t.sort_values("date", kind="stable").reset_index(drop=True)
        end = t["date"].max() + pd.Timedelta(days=1)
        start = end - pd.Timedelta(days=OOS_DAYS)
        ys, ms = [], []
        for lo in pd.date_range(start, end, freq=BLOCK):
            hi = lo + pd.Timedelta(BLOCK)
            test = t[(t["date"] >= lo) & (t["date"] < min(hi, end))]
            if test.empty:
                continue
            try:
                inner = TeamCornersF().fit(t[t["date"] < lo])
            except ValueError:
                continue
            lh, la = inner.rates(test["home"], test["away"])
            ys.append((test["home_corners"].to_numpy(float), test["away_corners"].to_numpy(float)))
            ms.append((lh, la))
        n = sum(len(y[0]) for y in ys)
        self.alpha_in_ = self.alpha_
        self.oos_matches_ = int(n)
        if n >= MIN_OOS:
            yh, ya = np.concatenate([y[0] for y in ys]), np.concatenate([y[1] for y in ys])
            mh, ma = np.concatenate([m[0] for m in ms]), np.concatenate([m[1] for m in ms])
            self.alpha_ = (mom_alpha(yh, mh), mom_alpha(ya, ma))
            self.alpha_source_ = "oos"
        else:
            self.alpha_source_ = "in_sample"
        return self

    def info(self) -> dict:
        return {
            **super().info(),
            "model": NAME,
            "alpha_source": self.alpha_source_,
            "oos_matches": self.oos_matches_,
            "alpha_in_sample": [round(float(a), 4) for a in self.alpha_in_],
        }
