import numpy as np
import pandas as pd
import pytest

from soccer_stats.lab import tuning_base as tb
from soccer_stats.lab import tuning_grid as tg


def test_config_counts_match_the_pre_registration():
    a = tg.model_configs("E0")
    assert (len(a["A"]), len(a["R"]), len(a["B"])) == (30, 5, 12)
    e = tg.model_configs("E2")
    assert (len(e["A"]), len(e["R"]), len(e["B"])) == (5, 5, 12)
    assert len(tg.blend_configs()) == 9
    for block in ("A", "R", "B"):
        assert "default" in a[block] and a[block]["default"] == tb.default_for("E0")
    assert a["R"]["default"].ridge == 0
    assert len(a["P"]) == 6 and a["P"]["default"].rho_option() is None
    opts = {c.rho for c in a["P"].values()}
    assert {"fixed:0", "penalty:1000"} <= opts


def _rows(seed=0):
    """Config 'good' is better in 1718-1819, worse later; 'bad' never better."""
    rng = np.random.default_rng(seed)
    out = []
    for s in ("1718", "1819", "1920", "2021"):
        for i in range(50):
            base = rng.uniform(0.9, 1.1)
            early = s in ("1718", "1819")
            for cfg, ll in (("default", base), ("good", base - (0.05 if early else -0.02)),
                            ("bad", base + 0.03)):  # fmt: skip
                out.append({"season": s, "home": f"H{i}", "away": f"A{i}", "config": cfg,
                            "ll": ll})  # fmt: skip
    return pd.DataFrame(out)


def test_nested_choice_uses_only_earlier_seasons():
    d, choices = tg.nested(_rows())
    assert [c["season"] for c in choices] == ["1920", "2021"]
    # 1920 is chosen on 1718-1819 only, where 'good' was better.
    assert choices[0]["config"] == "good"
    # Scored on 1920, where 'good' is worse: the gain is negative there.
    g1920 = d[d["season"] == "1920"]["gain"].mean()
    assert g1920 == pytest.approx(-0.02)
    blk = tg.gain_block(d, n_boot=100)
    assert blk["matches"] == 100


def test_hindsight_picks_the_best_scored_config():
    h = tg.hindsight(_rows())
    assert h["config"] == "default" and h["gain"] == pytest.approx(0)


def test_pool_flags_a_pass_only_with_a_range_above_zero(tmp_path):
    r = _rows().assign(league="E0", block="A")
    r.to_csv(tmp_path / "grid_E0.csv.gz", index=False)
    (tmp_path / "grid_E0.json").write_text('{"league": "E0", "configs": {"A": 3}}')
    res = tg.pool(tmp_path, n_boot=100)
    assert res["blocks"]["A"]["pooled"]["passes"] is False
    assert res["configs"] == 3
    assert tg.summary_lines(res)


def test_rho_config_reaches_the_model():
    c = tb.Config(rho="fixed:0")
    assert c.rho_option() == {"fixed": 0.0}
    assert c.key().endswith("_rhofixed:0")
    assert tb.Config().key() == tb.DEFAULT.key() and "rho" not in tb.Config().key()
