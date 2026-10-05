import numpy as np
import pandas as pd
import pytest
from scipy.stats import poisson

from soccer_stats import backtest
from soccer_stats import trades as tr
from soccer_stats.paper import update_ledger


def _probs(lam, mu):
    g = np.arange(11)
    m = np.outer(poisson.pmf(g, lam), poisson.pmf(g, mu))
    over = 1 - sum(m[i, j] for i in range(3) for j in range(3) if i + j <= 2)
    return [np.tril(m, -1).sum(), np.trace(m), np.triu(m, 1).sum(), over, 1 - over]


def make_history(matches, seed=1, margin=0.05):
    """DraftKings-like snapshots: noisy true odds with a margin, every 6 hours from 3 days
    before kickoff until just before it."""
    rng = np.random.default_rng(seed)
    rows = []
    for r in matches.itertuples(index=False):
        kickoff = pd.Timestamp(r.date).tz_localize("UTC") + pd.Timedelta(hours=15)
        true = _probs(r.lam, r.mu)
        for h in range(72, -1, -6):
            at = kickoff - pd.Timedelta(hours=h, minutes=1 if h == 0 else 0)
            noisy = np.clip(np.array(true) * rng.lognormal(0, 0.08, 5), 0.02, 0.97)
            p13 = noisy[:3] / noisy[:3].sum()
            ou = noisy[3:] / noisy[3:].sum()
            o = np.concatenate([1 / (p13 * (1 + margin)), 1 / (ou * (1 + margin))])
            rows.append(
                {
                    "at": at,
                    "snapshot_ts": at,
                    "kickoff": kickoff,
                    "home": r.home,
                    "away": r.away,
                    "odds_updated": at - pd.Timedelta(minutes=5),
                    **{f"odds_{m}": round(float(x), 2) for m, x in zip(tr.MARKETS, o, strict=True)},
                }
            )
    return pd.DataFrame(rows)


@pytest.fixture(scope="module")
def setup(league):
    df, _ = league
    df = df.copy()
    df["league"] = "E0"
    df["season"] = [tr.season_label(d) for d in df["date"]]
    hist = make_history(df[df["date"] >= "2022-07-01"])
    # Pinnacle's close: same as DraftKings' last price here
    close = hist.sort_values("at").groupby(["home", "away"]).tail(1).set_index(["home", "away"])
    for m in tr.MARKETS:
        df[f"close_{m}"] = [
            close[f"odds_{m}"].get((h, a)) for h, a in zip(df["home"], df["away"], strict=True)
        ]
    now = pd.Timestamp("2030-01-01", tz="UTC")
    cands = backtest.dk_candidates(df, hist, start="2022-07-01", now=now)
    return df, hist, cands, now


def test_candidates_use_only_earlier_snapshots(setup):
    _, _, cands, _ = setup
    assert not cands.empty
    assert set(cands["look"]) == {"48h", "3h"}
    fetched = pd.to_datetime(cands["odds_fetched_at"], utc=True)
    assert (fetched <= cands["look_at"]).all()
    assert (cands["look_at"] < cands["kickoff"]).all()


def test_trades_are_settled_once_per_match(setup):
    _, _, cands, _ = setup
    trades = backtest.dk_trades(cands, threshold=0.05)
    assert len(trades) > 10
    assert not trades.duplicated(["home", "away", "season"]).any()
    assert set(trades["status"]) <= {"won", "lost", "void"}
    assert (trades["edge"] >= 0.05 - 1e-9).all()
    assert trades["clv_dk"].notna().all() and trades["clv_pinnacle"].notna().all()
    # A trade opens at its first qualifying look.
    first = trades.groupby("look").size()
    assert first.get("48h", 0) > 0


def test_no_look_ahead(setup):
    df, hist, cands, now = setup
    trades = backtest.dk_trades(cands, threshold=0.05)
    cut = trades["kickoff"].sort_values().iloc[len(trades) // 2]
    # Change everything after the cut: later results and later odds snapshots.
    df2 = df.copy()
    later = df2["date"] >= pd.Timestamp(cut).tz_convert(None).normalize() + pd.Timedelta(days=1)
    df2.loc[later, ["home_goals", "away_goals"]] = (
        df2.loc[later, ["away_goals", "home_goals"]].to_numpy() + 3
    )
    hist2 = hist.copy()
    late = hist2["at"] > pd.Timestamp(cut) + pd.Timedelta(days=1)
    hist2.loc[late, [f"odds_{m}" for m in tr.MARKETS]] *= 1.3
    cands2 = backtest.dk_candidates(df2, hist2, start="2022-07-01", now=now)
    trades2 = backtest.dk_trades(cands2, threshold=0.05)
    early = trades[pd.to_datetime(trades["kickoff"]) <= pd.Timestamp(cut)]
    early2 = trades2[pd.to_datetime(trades2["kickoff"]) <= pd.Timestamp(cut)]
    cols = ["id", "market", "odds", "model_p", "edge", "status", "profit"]
    pd.testing.assert_frame_equal(
        early[cols].reset_index(drop=True), early2[cols].reset_index(drop=True)
    )


def test_csv_reproduces_summary(setup, tmp_path):
    _, _, cands, _ = setup
    trades = backtest.dk_trades(cands, threshold=0.05)
    path = tmp_path / "t.csv"
    trades.to_csv(path, index=False)
    again = pd.read_csv(path)
    a, b = tr.summarize(trades), tr.summarize(again)
    for k in ("trades", "settled", "staked", "profit", "roi", "win_rate", "clv_dk", "max_drawdown"):
        assert a[k] == pytest.approx(b[k]), k


def test_backtest_and_live_paths_give_the_same_trade(setup):
    _, _, cands, _ = setup
    trades = backtest.dk_trades(cands, threshold=tr.PAPER_EDGE)
    t = trades.iloc[0]
    c = cands[
        (cands["home"] == t["home"]) & (cands["away"] == t["away"]) & (cands["look"] == t["look"])
    ].iloc[0]
    card = {
        "home": c["home"],
        "away": c["away"],
        "kickoff": c["kickoff"].isoformat(),
        "p": {m: c[f"p_{m}"] for m in tr.MARKETS},
        "odds": {m: c[f"odds_{m}"] for m in tr.MARKETS},
        "low_data": False,
    }
    ledger = {}
    source = {"name": "DraftKings", "fetched_at": c["odds_fetched_at"]}
    update_ledger(ledger, [card], source, None, now=c["look_at"])
    live = next(iter(ledger.values()))
    for k in (
        "id",
        "market",
        "odds",
        "model_p",
        "edge",
        "stake",
        "threshold",
        "kickoff",
        "hours_to_kickoff",
    ):
        assert live[k] == t[k], k


def test_log_loss_reported(setup):
    _, _, cands, _ = setup
    ll = backtest.dk_log_loss(cands)
    assert ll["matches"] > 50 and 0.5 < ll["model"] < 1.5 and 0.5 < ll["draftkings"] < 1.5
