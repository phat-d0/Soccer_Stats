import re
from pathlib import Path

import pandas as pd
import pytest

from soccer_stats import trades as tr


def _cand(**kw):
    row = {
        "home": "A",
        "away": "B",
        "p_home": 0.5,
        "p_draw": 0.25,
        "p_away": 0.25,
        "p_over25": 0.5,
        "p_under25": 0.5,
        "odds_home": 1.9,
        "odds_draw": 3.5,
        "odds_away": 4.0,
        "odds_over25": 1.9,
        "odds_under25": 1.9,
        "home_n": 20,
        "away_n": 20,
    }
    row.update(kw)
    return row


def test_edge_of_exactly_the_threshold_qualifies():
    # 0.4 x 2.8 - 1 = 0.12 (in floating point a hair under)
    pick = tr.best_pick({"home": 0.4}, {"home": 2.8}, threshold=0.12)
    assert pick is not None and pick["market"] == "home"
    assert tr.best_pick({"home": 0.4}, {"home": 2.79}, threshold=0.12) is None


def test_one_trade_per_match_on_the_best_edge():
    # draw: 0.25 x 5.0 - 1 = +25%; away: 0.25 x 4.6 - 1 = +15%: both qualify
    df = pd.DataFrame([_cand(odds_draw=5.0, odds_away=4.6)])
    out = tr.select_trades(df, threshold=0.12)
    assert len(out) == 1
    assert out.iloc[0]["market"] == "draw"
    assert out.iloc[0]["edge"] == pytest.approx(0.25)


def test_thin_data_matches_are_skipped():
    df = pd.DataFrame([_cand(odds_draw=5.0, home_n=5), _cand(home="C", odds_draw=5.0)])
    out = tr.select_trades(df, threshold=0.12)
    assert list(out["home"]) == ["C"]


def test_odds_cap_and_missing_prices():
    df = pd.DataFrame([_cand(odds_draw=8.0, odds_away=None)])
    assert tr.select_trades(df).iloc[0]["market"] == "draw"
    assert tr.select_trades(df, max_odds=6.0).empty


def _trade(market="home", odds=2.5):
    return {"market": market, "odds": odds, "stake": 10.0}


def test_settle_win_loss_void():
    assert tr.settle(_trade(), 2, 1) == {"status": "won", "score": "2-1", "profit": 15.0}
    assert tr.settle(_trade(), 0, 0) == {"status": "lost", "score": "0-0", "profit": -10.0}
    assert tr.settle(_trade("under25"), 1, 1)["status"] == "won"
    assert tr.settle(_trade(), None, None) == {"status": "void", "score": None, "profit": 0.0}
    assert tr.settle(_trade(), 2, 1, void=True)["profit"] == 0.0


def test_clv_needs_the_whole_market():
    close = {"home": 2.0, "draw": 3.5, "away": 4.0}
    v = tr.clv(2.5, "home", close)
    assert v is not None and v > 0  # 2.5 is a better price than the 2.0 close
    assert tr.clv(2.5, "home", {"home": 2.0}) is None
    assert tr.clv(1.9, "over25", {"over25": 1.9, "under25": 1.9}) == pytest.approx(-0.05)


def test_summary_and_drawdown():
    df = pd.DataFrame(
        {
            "kickoff": pd.date_range("2025-08-01", periods=4, freq="7D", tz="UTC"),
            "status": ["won", "lost", "lost", "open"],
            "odds": [3.0, 2.0, 2.0, 2.0],
            "stake": 10.0,
            "edge": [0.15, 0.13, 0.12, 0.2],
            "profit": [20.0, -10.0, -10.0, None],
            "clv_dk": [0.05, -0.02, 0.01, None],
        }
    )
    s = tr.summarize(df)
    assert s["trades"] == 4 and s["open"] == 1 and s["settled"] == 3
    assert s["profit"] == 0.0 and s["roi"] == 0.0
    assert s["max_drawdown"] == 20.0
    assert s["beat_close_dk"] == pytest.approx(2 / 3)
    assert s["breakeven"] == pytest.approx((1 / 3 + 0.5 + 0.5) / 3)
    rep = tr.report(df)
    assert {r["group"] for r in rep["breakdowns"]["edge_bucket"]} == {"12–15%", "15–20%", "20%+"}


def test_app_filter_presets_match():
    js = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text()
    steps = re.search(r"const EDGE_STEPS = \[([^\]]*)\]", js).group(1)
    assert tuple(float(x) for x in steps.split(",")) == tr.FILTER_PRESETS
    # No fixed default in the app: the minimum edge comes from the backtest's edge_threshold
    # (EDGE_STEPS are only for "Explore other edges").
    assert f"const PAPER_EDGE = {tr.PAPER_EDGE}" in js


# ---------- the live paper rule switch (owner's live test, 10 Oct 2026) ----------

SIX = ("E0", "SP1", "D1", "I1", "F1", "E1")
CLUBS = {
    "E0": ("Arsenal", "Leeds"),
    "SP1": ("Real Madrid", "Getafe"),
    "D1": ("Dortmund", "Werder Bremen"),
    "I1": ("Inter", "Lecce"),
    "F1": ("Lyon", "Lens"),
    "E1": ("Leicester", "Hull"),
}


def _six_league_data():
    from test_paper import card, src

    fixtures = []
    for lg, (h, a) in CLUBS.items():
        # The raw model sees a 20% edge on the draw (0.24 x 5.0); the blend sees none.
        c = card(home=h, away=a, league=lg)
        c["p_bet"] = {"home": 0.70, "draw": 0.19, "away": 0.11, "over25": 0.55, "under25": 0.45}
        fixtures.append(c)
    return {
        "fixtures": fixtures,
        "odds_source": src(),
        "odds_sources": {lg: {"league": lg, **src()} for lg in SIX},
        "leagues": [{"code": lg, "fixtures": 1} for lg in SIX],
        "portfolio": {},
    }


def _null_levels(tmp_path, monkeypatch):
    """data-log as it is today: E0_dk.json with min_edge null, lab levels null."""
    import json

    from soccer_stats import paper

    log = tmp_path / "log"
    (log / "backtest").mkdir(parents=True)
    null = {"min_edge": None, "note": "Too few bets."}
    (log / "backtest" / "E0_dk.json").write_text(
        json.dumps({"summary": {}, "edge_threshold": null})
    )
    lab = tmp_path / "min_edge.json"
    lab.write_text(json.dumps({"leagues": {lg: null for lg in SIX[1:]}}))
    monkeypatch.setattr(paper, "LAB_LEVELS", lab)
    return log


def _opens(log, lg):
    import json

    lines = (log / "paper_trades" / f"{lg}_2627.jsonl").read_text().splitlines()
    return sum(json.loads(x).get("type") == "open" for x in lines if x.strip())


def test_fixed_raw_is_the_live_rule_and_the_same_for_every_league():
    assert tr.PAPER_RULE == "fixed_raw"
    learned_null = {"edge_threshold": {"min_edge": None, "note": "x"}}
    for lg in SIX:
        for dk in (None, learned_null, {"edge_threshold": {"min_edge": 0.03}}):
            assert tr.paper_threshold(dk, lg) == {
                "threshold": 0.12,
                "source": "owner_fixed",
                "note": tr.OWNER_FIXED_NOTE,
                "rule": "fixed_raw",
                "p_source": "model",
            }
    assert "fixed_raw (12% on the model's own chance, all leagues)" in tr.paper_rule_line()
    assert tr.paper_rule_line("learned").startswith("Paper rule: learned")
    with pytest.raises(ValueError):
        tr.paper_threshold(None, rule="nope")


def test_fixed_raw_opens_every_live_league_on_the_raw_chance(tmp_path, monkeypatch):
    from test_paper import NOW

    from soccer_stats import paper

    log = _null_levels(tmp_path, monkeypatch)
    d = _six_league_data()
    assert paper.run(d, log, None, now=NOW) == 6
    for lg, (h, a) in CLUBS.items():
        led = paper.load_ledger(log, lg)
        assert list(led) == [f"{lg}|2627|{h}|{a}"]  # each league into its own ledger file
        assert (log / "paper_trades" / f"{lg}_2627.jsonl").exists()
        t = led[f"{lg}|2627|{h}|{a}"]
        assert t["market"] == "draw" and t["model_p"] == 0.24  # the raw p, not p_bet
        assert t["edge"] == pytest.approx(0.24 * 5.0 - 1)  # against the quoted price
        assert t["threshold"] == 0.12 and t["stake"] == 10.0
        assert t["rule"] == "fixed_raw" and t["p_source"] == "model"
        assert t["model_ref"]["probs"] == "raw"
        assert set(tr.ENTRY_FIELDS) <= set(t)
    p = d["portfolio"]
    assert p["rule"]["rule"] == "fixed_raw" and p["rule"]["p_source"] == "model"
    assert p["rule"]["threshold"] == 0.12 and p["rule"]["note"] == tr.OWNER_FIXED_NOTE
    assert p["rule"]["threshold_source"] == "owner_fixed"
    assert all(p["rules"][lg]["rule"] == "fixed_raw" for lg in SIX)
    ml = next(x for x in p["portfolios"] if x["id"] == "moneyline")
    assert ml["live"]["rule"]["p_source"] == "model" and ml["live"]["note"] == tr.OWNER_FIXED_NOTE
    assert ml["live"]["summary"]["trades"] == 6

    # A rebuild never opens a trade twice.
    d2 = _six_league_data()
    paper.run(d2, log, None, now=NOW + pd.Timedelta(hours=1))
    for lg in SIX:
        assert _opens(log, lg) == 1


def test_an_existing_open_trade_is_not_duplicated_under_fixed_raw(tmp_path, monkeypatch):
    """The four E0 trades opened on 5 Oct (no rule/p_source fields) stay one trade each."""
    import json

    from test_paper import NOW

    from soccer_stats import paper

    log = _null_levels(tmp_path, monkeypatch)
    old = {
        "type": "open",
        **tr.new_trade(
            {"market": "draw", "odds": 4.9, "model_p": 0.2441, "edge": 0.1961},
            source="live",
            league="E0",
            home="Arsenal",
            away="Leeds",
            kickoff=pd.Timestamp("2026-10-10 14:00", tz="UTC"),
            opened_at=pd.Timestamp("2026-10-05 22:58", tz="UTC"),
            odds_fetched_at=None,
            threshold=0.12,
        ),
    }
    for k in ("rule", "p_source"):
        old.pop(k)
    (log / "paper_trades").mkdir()
    (log / "paper_trades" / "E0_2627.jsonl").write_text(json.dumps(old) + "\n")
    paper.run(_six_league_data(), log, None, now=NOW)
    led = paper.load_ledger(log, "E0")
    t = led["E0|2627|Arsenal|Leeds"]
    assert t["odds"] == 4.9 and t["model_p"] == 0.2441 and "rule" not in t  # entry untouched
    assert _opens(log, "E0") == 1


def test_switching_to_learned_restores_the_null_behaviour(tmp_path, monkeypatch):
    from test_paper import NOW

    from soccer_stats import paper

    monkeypatch.setattr(tr, "PAPER_RULE", "learned")
    log = _null_levels(tmp_path, monkeypatch)
    d = _six_league_data()
    assert paper.run(d, log, None, now=NOW) == 0  # every learned level is null
    assert not (log / "paper_trades").exists() or not list((log / "paper_trades").iterdir())
    for lg in SIX:
        r = d["portfolio"]["rules"][lg]
        assert r["threshold"] is None and r["rule"] == "learned" and r["p_source"] == "blend"
    assert "No new match paper trades" in d["portfolio"]["rule"]["note"]
    # With a learned level the blend decides: p_bet's draw is 0.19 x 5.0 - 1 = -5%, no trade.
    ev, _ = paper.update_ledger(
        {},
        d["fixtures"][:1],
        d["odds_source"],
        None,
        NOW,
        threshold=0.02,
        rule=tr.paper_threshold({"edge_threshold": {"min_edge": 0.02}}),
    )
    assert ev == []
    raw = tr.paper_threshold(None, rule="fixed_raw")
    ev, _ = paper.update_ledger(
        {}, d["fixtures"][:1], d["odds_source"], None, NOW, threshold=0.12, rule=raw
    )
    assert [e["p_source"] for e in ev] == ["model"]


def test_the_app_flags_keep_the_learned_levels_under_fixed_raw(tmp_path, monkeypatch):
    """edge_threshold.by_league (what the app flags) is unchanged by the paper switch."""
    import json

    from test_paper import NOW

    from soccer_stats import paper

    log = _null_levels(tmp_path, monkeypatch)
    path = log / "backtest" / "E0_dk.json"
    bt = json.loads(path.read_text())
    bt["edge_threshold"]["by_bucket"] = []
    path.write_text(json.dumps(bt))
    d = _six_league_data()
    paper.run(d, log, None, now=NOW)
    ml = next(x for x in d["portfolio"]["portfolios"] if x["id"] == "moneyline")
    by = ml["backtest"]["edge_threshold"]["by_league"]
    assert all(by[lg]["min_edge"] is None for lg in SIX)
    assert all(tr.OWNER_FIXED_NOTE not in (by[lg].get("note") or "") for lg in SIX)


def test_fixed_raw_keeps_the_kickoff_freshness_and_odds_gates(tmp_path, monkeypatch):
    """Lead review: the switch only changes the chance and threshold; every other gate
    (before kickoff, fresh DraftKings odds, a priced card, enough data) still holds."""
    from test_paper import NOW

    from soccer_stats import paper

    log = _null_levels(tmp_path, monkeypatch)
    d = _six_league_data()
    by = {c["league"]: c for c in d["fixtures"]}
    by["SP1"]["kickoff"] = (NOW - pd.Timedelta(minutes=1)).isoformat()  # kicked off
    stale = (NOW - pd.Timedelta(hours=paper.FRESH_HOURS, minutes=1)).isoformat()
    d["odds_sources"]["D1"]["fetched_at"] = stale  # odds older than FRESH_HOURS
    del d["odds_sources"]["I1"]  # a league without an odds source
    by["F1"]["odds"] = {}  # a card without prices
    by["E1"]["low_data"] = True
    # A second card for the E0 match (same names) still opens one trade.
    d["fixtures"].append(dict(by["E0"]))
    assert paper.run(d, log, None, now=NOW) == 1
    assert list(paper.load_ledger(log, "E0")) == ["E0|2627|Arsenal|Leeds"]
    for lg in SIX[1:]:
        assert paper.load_ledger(log, lg) == {}
        assert not (log / "paper_trades" / f"{lg}_2627.jsonl").exists()
