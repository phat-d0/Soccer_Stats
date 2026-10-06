import numpy as np
import pandas as pd
import pytest
from scipy.stats import poisson

from soccer_stats import backtest
from soccer_stats import match_calibration as mc
from soccer_stats import match_markets as mm
from soccer_stats.markets import over_under


def _matrix(lam, mu, n=11):
    g = np.arange(n)
    m = np.outer(poisson.pmf(g, lam), poisson.pmf(g, mu))
    return m / m.sum()


# ---------- Asian handicap ----------


@pytest.mark.parametrize(
    "line, hg, ag, expected",
    [
        (-0.5, 1, 0, (1.0, 0.0)),
        (-0.5, 1, 1, (0.0, 1.0)),
        (-1.0, 1, 0, (0.0, 0.0)),  # push
        (-1.0, 2, 0, (1.0, 0.0)),
        (-0.75, 1, 0, (0.5, 0.0)),  # half on -0.5 wins, half on -1 pushes
        (-0.25, 0, 0, (0.0, 0.5)),  # half on 0 pushes, half on -0.5 loses
        (0.25, 0, 0, (0.5, 0.0)),
        (0.0, 0, 0, (0.0, 0.0)),
        (1.5, 0, 1, (1.0, 0.0)),
    ],
)
def test_ah_settle(line, hg, ag, expected):
    assert mm.ah_settle(line, hg, ag) == expected


def test_ah_shares_match_settlement_and_half_lines():
    m = _matrix(1.6, 1.1)
    for line in (-1.75, -1.5, -1.0, -0.75, -0.25, 0.0, 0.25, 0.5, 1.0):
        w, l_ = mm.ah_shares(m, line)
        ew = sum(m[i, j] * mm.ah_settle(line, i, j)[0] for i in range(11) for j in range(11))
        el = sum(m[i, j] * mm.ah_settle(line, i, j)[1] for i in range(11) for j in range(11))
        assert (w, l_) == pytest.approx((ew, el))
    # A half line never pushes, and -0.5 is a home win.
    w, l_ = mm.ah_shares(m, -0.5)
    assert w + l_ == pytest.approx(1)
    assert w == pytest.approx(np.tril(m, -1).sum())
    # Away's chance at the mirrored line is 1 - home's.
    w, l_ = mm.ah_shares(m, -0.75)
    assert mm.ah_chance(w, l_) + mm.ah_chance(l_, w) == pytest.approx(1)


# ---------- the blend with weights and extra signals ----------


def test_fit_without_extras_is_unchanged():
    rng = np.random.default_rng(1)
    q = rng.uniform(0.2, 0.8, 2000)
    mk = np.column_stack([q, 1 - q])
    y = (rng.uniform(size=2000) > q).astype(int)
    base = mc.fit(mk, mk, y)
    assert len(base) == 3
    assert mc.fit(mk, mk, y, weight=np.ones(2000)) == pytest.approx(base, abs=1e-4)


def test_fit_recovers_a_feature_weight_and_ignores_zero_weight_rows():
    rng = np.random.default_rng(2)
    n = 20000
    q = rng.uniform(0.2, 0.8, n)
    x = rng.normal(0, 1, n)
    logit = np.log(q / (1 - q)) + 0.4 * x
    p = 1 / (1 + np.exp(-logit))
    y = np.where(rng.uniform(size=n) < p, 0, 1)
    mk = np.column_stack([q, 1 - q])
    w = np.ones(n)
    # Rows with weight 0 (pushes) carry nonsense outcomes; they must not matter.
    junk = rng.uniform(size=n) < 0.2
    y2 = np.where(junk, 1, y)
    w[junk] = 0
    coef = mc.fit(mk, mk, y2, weight=w, extra=x[:, None])
    assert len(coef) == 4
    assert coef[-1] == pytest.approx(0.4, abs=0.06)
    assert coef[1] + coef[2] == pytest.approx(1.0, abs=0.1)  # b + c on the same input
    out = mc.apply(coef, mk[:5], mk[:5], x[:5, None])
    assert out[:, 0] == pytest.approx(p[:5], abs=0.03)


def test_h2h_feature_moves_home_against_away():
    coef = [0.0, 0.0, 1.0, 0.0, 0.5]
    mk = np.array([[0.4, 0.3, 0.3]])
    up = mc.apply(coef, mk, mk, [[1.0]])[0]
    assert up[0] > 0.4 and up[2] < 0.3
    assert mc.apply(coef, mk, mk, [[np.nan]])[0] == pytest.approx([np.nan] * 3, nan_ok=True)


# ---------- xG signals ----------


def test_xg_features_use_earlier_matches_only(league):
    df, _ = league
    f = mm.xg_features(df)
    assert len(f) == len(df)
    later = df.copy()
    cut = later["date"] >= later["date"].iloc[len(df) // 2]
    later.loc[cut, ["home_xg", "away_xg", "home_goals", "away_goals"]] = [9.0, 0.0, 0, 9]
    g = mm.xg_features(later)
    first_cut = int(np.argmax(cut.to_numpy()))
    cols = ["x_xgd", "x_luck", "x_xgt", "x_luckt"]
    # Changing results from some date on can't change any feature up to that date.
    a = f.loc[:first_cut, cols].to_numpy()
    b = g.loc[:first_cut, cols].to_numpy()
    assert np.allclose(a, b, equal_nan=True)
    assert not np.allclose(f[cols].to_numpy(), g[cols].to_numpy(), equal_nan=True)
    # The first matches have no history.
    assert f[cols].iloc[0].isna().all()


def test_xg_features_by_hand():
    d = pd.date_range("2024-08-01", periods=4, freq="7D")
    m = pd.DataFrame(
        {
            "date": d,
            "home": ["A", "B", "A", "B"],
            "away": ["B", "A", "B", "A"],
            "home_goals": [1, 0, 2, 0],
            "away_goals": [0, 0, 0, 0],
            "home_xg": [2.0, 0.5, 1.0, 1.0],
            "away_xg": [1.0, 0.5, 1.0, 1.0],
        }
    )
    f = mm.xg_features(m, n=6, min_games=1)
    # Match 3 (A v B): A's earlier xgd is (+1, 0) -> 0.5; B's is (-1, 0) -> -0.5.
    assert f.loc[2, "x_xgd"] == pytest.approx(1.0)
    # Luck: A (xgd - gd) = (1-1, 0-0) -> 0; B = (-1+1, 0) -> 0.
    assert f.loc[2, "x_luck"] == pytest.approx(0.0)
    # Totals: A's xG in play (3, 1) -> 2; B's (3, 1) -> 2.
    assert f.loc[2, "x_xgt"] == pytest.approx(4.0)


# ---------- end to end on a synthetic league ----------


def _raw(df, rng):
    """football-data-like rows: Pinnacle-ish prices from the true rates, plus noise."""
    rows = []
    for r in df.itertuples(index=False):
        m = _matrix(r.lam, r.mu)
        h2h = np.array([np.tril(m, -1).sum(), np.trace(m), np.triu(m, 1).sum()])
        ou = over_under(m, 2.5)
        line = -round((r.lam - r.mu) * 2) / 4  # quarter steps, home handicap
        w, l_ = mm.ah_shares(m, line)
        ah = np.array([mm.ah_chance(w, l_), mm.ah_chance(l_, w)])

        def px(p, margin=1.03, noise=0.04):
            q = p * rng.lognormal(0, noise, len(p))
            return np.round(1 / (q / q.sum() * margin), 3)

        e, c = px(h2h), px(h2h)
        eo, co = px(ou), px(ou)
        ea, ca = px(ah), px(ah)
        rows.append(
            {
                "Date": r.date.strftime("%d/%m/%Y"),
                "HomeTeam": r.home,
                "AwayTeam": r.away,
                "FTHG": r.home_goals,
                "FTAG": r.away_goals,
                "PSH": e[0],
                "PSD": e[1],
                "PSA": e[2],
                "PSCH": c[0],
                "PSCD": c[1],
                "PSCA": c[2],
                "AvgH": e[0] * 0.97,
                "AvgD": e[1] * 0.97,
                "AvgA": e[2] * 0.97,
                "P>2.5": eo[0],
                "P<2.5": eo[1],
                "PC>2.5": co[0],
                "PC<2.5": co[1],
                "AHh": line,
                "AHCh": line,
                "PAHH": ea[0],
                "PAHA": ea[1],
                "PCAHH": ca[0],
                "PCAHA": ca[1],
                "MaxAHH": ea[0] * 1.02,
                "MaxAHA": ea[1] * 1.02,
            }
        )
    return pd.DataFrame(rows)


@pytest.fixture(scope="module")
def result(league):
    df, _ = league
    df = df.copy()
    df["season"] = "x"
    raw = _raw(df, np.random.default_rng(3))
    prices = mm.load_prices(raw, season="2021")
    preds = backtest.walk_forward(df, start="2021-01-01", keep_matrix=True)
    joined = mm.join(preds, prices).merge(
        mm.xg_features(df).assign(date=lambda x: x["date"].dt.normalize()),
        on=["date", "home", "away"],
        how="left",
    )
    return joined, mm.run(joined, thresholds=(0.02, 0.12), min_rows=60)


def test_load_prices_columns(league):
    df, _ = league
    raw = _raw(df.head(5), np.random.default_rng(0))
    p = mm.load_prices(raw, season="2021")
    assert {"line_early", "ah_early_pinnacle_ah_home", "h2h_close_pinnacle_home"} <= set(p)
    assert p["h2h_early_max_home"].isna().all()  # column missing in this file
    assert len(p) == 5


def test_candidates_no_bets_without_prices(result):
    joined, _ = result
    c = mm.candidates(joined, "ah", "early")
    assert len(c) > 0
    assert np.allclose(c["p_ah_home"] + c["p_ah_away"], 1)
    assert set(np.unique(c["w"])) <= {0.0, 0.5, 1.0}
    assert {"odds_pinnacle_early_ah_home", "odds_max_early_ah_away"} <= set(c)
    assert "odds_pinnacle_close_ah_home" not in c


def test_run_reports_each_market(result):
    _, out = result
    for g in ("h2h", "totals", "ah"):
        r = out[g]
        assert r["matches_close"] > 0
        ll = r["log_loss"]
        assert {"model", "pinnacle_close", "blend"} <= set(ll)
        assert r["fits"]["blend"]["refits"] > 0
        assert r["sweep"], g
        row = r["sweep"][0]
        assert {"source", "strategy", "threshold", "bets"} <= set(row)
    assert "signal" in out["h2h"]
    assert out["ah"]["ah_line_moved"] == 0.0


def test_bets_profit_and_clv_by_hand():
    cands = pd.DataFrame(
        {
            "date": pd.to_datetime(["2024-01-01"] * 2),
            "season": ["2324"] * 2,
            "home": ["A", "C"],
            "away": ["B", "D"],
            "win_ah_home": [0.5, 0.0],
            "loss_ah_home": [0.0, 1.0],
            "win_ah_away": [0.0, 1.0],
            "loss_ah_away": [0.5, 0.0],
            "fair_close_ah_home": [0.55, np.nan],
            "fair_close_ah_away": [0.45, np.nan],
            "odds_pinnacle_early_ah_home": [2.0, 1.9],
            "odds_pinnacle_early_ah_away": [1.9, 2.0],
        }
    )
    probs = pd.DataFrame({"ah_home": [0.6, 0.45], "ah_away": [0.4, 0.55]})
    b = mm.bets(cands, probs, "ah", "pinnacle_early", 0.05)
    assert list(b["market"]) == ["ah_home", "ah_away"]
    assert list(b["profit"]) == pytest.approx([0.5, 1.0])  # half-win at 2.0; away win
    assert b["clv"].iloc[0] == pytest.approx(2.0 * 0.55 - 1)
    assert np.isnan(b["clv"].iloc[1])
    assert mm.bets(cands, probs, "ah", "pinnacle_early", 0.5).empty
