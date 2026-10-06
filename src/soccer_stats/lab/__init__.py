"""Research lab: a shared evaluation harness and the match-model bake-off.

* harness: walk-forward splits by time, nested time-ordered tuning, a locked holdout.
* metrics: log loss, Brier, calibration, blend weight beside a market price, CLV and ROI
  with bootstrap ranges that resample whole groups (matches).
* features, models, run: the match bake-off (see docs/lab.md).

The harness and metrics are generic: rows with a time, a group, an outcome index,
features, and market probabilities/prices. Player markets can use them as they are.
"""
