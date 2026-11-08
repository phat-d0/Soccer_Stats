import json

import numpy as np
import pandas as pd
import pytest
from test_lab_tuning import _league

from soccer_stats.lab import tuning_base as tb
from soccer_stats.lab import tuning_judged as tj


def test_dev_choice_picks_the_lowest_development_log_loss():
    m = _league(seasons=3, first=2017)
    opts = {"default": tb.Config(), "r10": tb.Config(ridge=10.0)}
    res = tj.dev_choice(m, "E0", opts)
    assert res["pick"] == min(res["dev_ll"], key=res["dev_ll"].get)
    assert res["dev_matches"] > 0


def test_judged_rows_score_only_2025_26():
    m = _league(seasons=3, first=2023)  # 2023/24, 2024/25, 2025/26
    rows = tj.judged_rows(m, m, tb.Config(), tb.Config(ridge=3.0))
    assert len(rows) > 0
    assert set(rows["season"]) == {"2526"}
    assert rows["gain"].to_numpy() == pytest.approx(
        (rows["ll_default"] - rows["ll_candidate"]).to_numpy()
    )
    assert rows["ll_close"].notna().all()


def test_pool_passes_only_when_the_range_is_above_zero(tmp_path):
    rng = np.random.default_rng(0)
    n = 400
    base = rng.uniform(0.9, 1.1, n)
    for lg, shift in (("E1", 0.01), ("E0", 0.0)):
        frame = pd.DataFrame({"league": lg, "season": "2526", "home": [f"H{i}" for i in range(n)],
                              "away": "A", "ll_default": base + shift, "ll_candidate": base,
                              "ll_early": np.nan, "ll_close": np.nan, "gain": shift})  # fmt: skip
        frame.to_csv(tmp_path / f"judged_{lg}.csv.gz", index=False)
        (tmp_path / f"judged_{lg}.json").write_text(json.dumps({"league": lg, "candidate": "x"}))
    res = tj.pool(tmp_path, n_boot=200)
    assert res["passes"] is True and res["pooled"]["gain"] == pytest.approx(0.005)
    assert res["lower_leagues"]["gain"] == pytest.approx(0.01)
    assert tj.summary_lines(res)
