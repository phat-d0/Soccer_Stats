import numpy as np
from scipy.stats import poisson

from soccer_stats import backtest
from soccer_stats.markets import match_odds


def _with_odds(df, margin=0.05):
    """Attach 'true' closing odds (with margin) and noisy opening odds."""
    rng = np.random.default_rng(3)
    df = df.copy()
    g = np.arange(11)
    probs = np.array(
        [
            match_odds(np.outer(poisson.pmf(g, lam), poisson.pmf(g, mu)))
            for lam, mu in zip(df["lam"], df["mu"], strict=True)
        ]
    )
    close = 1 / (probs * (1 + margin))
    open_ = close * np.exp(rng.normal(0, 0.05, close.shape))
    for i, o in enumerate(backtest.OUTCOMES):
        df[f"close_{o}"] = close[:, i]
        df[f"odds_{o}"] = open_[:, i]
    return df


def test_walk_forward_end_to_end(league):
    df, _ = league
    df = _with_odds(df)
    start = df["date"].min() + (df["date"].max() - df["date"].min()) * 0.6
    preds = backtest.walk_forward(df, start=start, refit_every="30D")
    assert len(preds) > 50
    assert (preds["date"] >= start).all()
    np.testing.assert_allclose(preds[["p_home", "p_draw", "p_away"]].sum(axis=1), 1.0)

    preds = backtest.add_market_probs(preds)
    scores = backtest.score(preds)
    # Market here is the true generating process, so it should not lose to the model by much
    # and the model should beat a naive uniform forecast (log loss of ln 3).
    assert scores.loc["market", "log_loss"] <= scores.loc["model", "log_loss"] + 0.02
    assert scores.loc["model", "log_loss"] < np.log(3)

    summary = backtest.summarize_bets(backtest.simulate_bets(preds, min_edge=0.0))
    assert summary["bets"] > 0 and "avg_clv" in summary
