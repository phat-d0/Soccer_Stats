"""Blend Lean's odds range (owner, 24 Oct; docs/leagues.md "Blend Lean odds range"): a Lean
trade opens only at 1.50 < odds <= 3.00, a pick outside it opens nothing and becomes a
shadow entry in its own ledger file, and the old rule (all odds) is tracked as a variant.
Synthetic data only."""

import numpy as np
import pandas as pd
import pytest
from test_blend_lean import ODDS_IN, P_BET_IN, SIGMA, lean
from test_paper import NOW, card, src

from soccer_stats import paper
from soccer_stats import trades as tr
from soccer_stats.publish import add_lean

# Highest z on the draw at 5.0 (z = 3.0, outside); home at 1.80 also clears 1 sigma
# (z = 2.2, inside): the rule must not fall back to it.
P_BET_OUT = {"home": 0.60, "draw": 0.26, "away": 0.14, "over25": None, "under25": None}
LEAN_ID = "E0|2627|Arsenal|Leeds|lean"
SHADOW_ID = LEAN_ID + tr.SHADOW_SUFFIX
LATER = pd.Timestamp("2026-10-11 12:00", tz="UTC")


def draw_result():
    return pd.DataFrame(
        {
            "season": ["2627"],
            "date": [pd.Timestamp("2026-10-10")],
            "home": ["Arsenal"],
            "away": ["Leeds"],
            "home_goals": [1],
            "away_goals": [1],
            "close_home": [1.85],
            "close_draw": [4.6],
            "close_away": [7.0],
        }
    )


def test_the_range_is_open_below_and_closed_above():
    assert tr.LEAN_ODDS_RANGE == (1.50, 3.00)
    assert not tr.in_lean_range(1.50)  # -200 is out
    assert tr.in_lean_range(1.5001) and tr.in_lean_range(2.2)
    assert tr.in_lean_range(3.00)  # +200 stays in
    assert not tr.in_lean_range(3.0001) and not tr.in_lean_range(1.4)
    assert not tr.in_lean_range(None) and not tr.in_lean_range(float("nan"))
    rule = lean()
    assert rule["odds_range"] == [1.5, 3.0] and rule["rule_version"] == 2
    assert rule["odds_range_from"] == tr.LEAN_RANGE_FROM
    assert "-200 and +200" in rule["odds_range_note"]


@pytest.mark.parametrize(
    ("price", "opens"), [(1.50, False), (1.51, True), (3.00, True), (3.01, False)]
)
def test_a_lean_trade_opens_only_inside_the_range(price, opens):
    # The blend puts the home side 1.2 sigma over its break-even; the others below theirs.
    pb = {"home": 1 / price + 1.2 * SIGMA, "draw": 0.10, "away": 0.05}
    odds = {"home": price, "draw": 9.0, "away": 15.0}
    led, shadow = {}, {}
    paper.update_ledger(
        led,
        [card(p_bet=pb, odds=odds)],
        src(),
        None,
        NOW,
        threshold=None,
        lean=lean(),
        shadow=shadow,
    )
    assert (LEAN_ID in led) is opens
    assert (SHADOW_ID in shadow) is not opens


def test_no_fallback_to_another_side_and_the_pick_becomes_a_shadow():
    c = card(p_bet=P_BET_OUT, odds=ODDS_IN)
    z = tr.side_z(P_BET_OUT, ODDS_IN, SIGMA)
    assert z["home"] >= 1 and tr.in_lean_range(ODDS_IN["home"])  # a qualifying in-range side
    led, shadow = {}, {}
    ev, _ = paper.update_ledger(
        led, [c], src(), None, NOW, threshold=None, lean=lean(), shadow=shadow
    )
    assert led == {} and list(shadow) == [SHADOW_ID]
    s = shadow[SHADOW_ID]
    assert s["market"] == "draw" and s["odds"] == 5.0 and s["z"] == pytest.approx(3.0)
    assert s["shadow"] is True and s["excluded_by"] == "odds_range"
    assert s["strategy"] == "lean" and s["rule_version"] == 2 and s["close_odds"] == 5.0
    assert [e["id"] for e in ev] == [SHADOW_ID]
    # Without a shadow ledger the out-of-range pick just opens nothing.
    ev, _ = paper.update_ledger({}, [c], src(), None, NOW, threshold=None, lean=lean())
    assert ev == []


def test_shadow_entries_go_only_to_their_own_file(tmp_path):
    led, shadow = {}, {}
    ev, _ = paper.update_ledger(
        led,
        [card(p_bet=P_BET_OUT, odds=ODDS_IN)],
        src(),
        None,
        NOW,
        threshold=0.12,
        rule=tr.paper_threshold(None, "E0"),
        lean=lean(),
        shadow=shadow,
    )
    paper.append_events(tmp_path, ev)
    files = sorted(p.name for p in paper.ledger_dir(tmp_path).iterdir())
    assert files == ["E0_2627.jsonl", "E0_lean_shadow_2627.jsonl"]
    assert list(paper.load_ledger(tmp_path)) == ["E0|2627|Arsenal|Leeds"]  # the 12% trade
    assert list(paper.load_lean_shadow(tmp_path, "E0")) == [SHADOW_ID]
    assert paper.ledger_leagues(tmp_path) == ["E0"]


def run(tmp_path, fixtures, now=NOW, res=None):
    blends = {"leagues": {"E0": {"coef": [0, 0, 1, 0], "sigma": SIGMA, "matches": 900}}}
    data = {
        "fixtures": fixtures,
        "odds_source": src(now - pd.Timedelta(minutes=30)),
        "portfolio": {},
        "match_blends": blends,
    }
    written = paper.run(data, tmp_path, res, now=now)
    ml = next(p for p in data["portfolio"]["portfolios"] if p["id"] == "moneyline")
    return written, data, ml


def test_paper_run_keeps_shadows_out_of_the_real_record_and_settles_them(tmp_path):
    written, data, ml = run(tmp_path, [card(p_bet=P_BET_OUT, odds=ODDS_IN)])
    assert written == 2  # the 12% trade and the shadow entry
    live = ml["live"]
    assert [t["id"] for t in live["trades"]] == ["E0|2627|Arsenal|Leeds"]
    assert live["summary"]["trades"] == 1 and data["portfolio"]["live"]["summary"]["trades"] == 1
    assert [t["id"] for t in live["shadow_trades"]] == [SHADOW_ID]
    sts = {s["key"]: s for s in live["strategies"]}
    assert [s["key"] for s in live["strategies"]] == ["lean", "lean_all_odds", "edge12"]
    assert sts["lean"]["summary"]["trades"] == 0
    old = sts["lean_all_odds"]
    assert old["trade_ids"] == [SHADOW_ID] and old["summary"]["trades"] == 1
    assert old["variant_of"] == "lean" and old["adds"] == "shadow"
    assert old["defined_at"] == tr.LEAN_RANGE_FROM and old["label"].endswith("(old rule)")
    assert old["rule"]["odds_range"] == [1.5, 3.0] and old["rule"]["variant_of"] == "lean"
    # Idempotent: the same build again writes nothing.
    assert run(tmp_path, [card(p_bet=P_BET_OUT, odds=ODDS_IN)])[0] == 0
    # Settled by the same code as a trade (draw 1-1: the shadow won), in its own file.
    written, data, ml = run(tmp_path, [], now=LATER, res=draw_result())
    assert written == 2
    s = paper.load_lean_shadow(tmp_path, "E0")[SHADOW_ID]
    assert s["status"] == "won" and s["profit"] == pytest.approx(40.0)
    assert s["clv_pinnacle"] is not None
    live = ml["live"]
    # Both bet the draw at 5.0 and won $40: the real record holds only the 12% trade.
    assert live["summary"]["settled"] == 1 and live["summary"]["profit"] == pytest.approx(40.0)
    assert [t["id"] for t in live["trades"]] == ["E0|2627|Arsenal|Leeds"]
    old = next(x for x in live["strategies"] if x["key"] == "lean_all_odds")
    assert old["summary"]["settled"] == 1 and old["summary"]["profit"] == pytest.approx(40.0)
    assert old["trade_ids"] == [SHADOW_ID]
    assert run(tmp_path, [], now=LATER, res=draw_result())[0] == 0


def test_the_old_rule_is_every_real_lean_trade_plus_the_shadows():
    def t(i, shadow=False, strategy="lean", match=None):
        m = match if match is not None else i
        return {
            "id": f"E0|2627|H{m}|A{m}|lean" + (tr.SHADOW_SUFFIX if shadow else ""),
            "league": "E0",
            "season": "2627",
            "home": f"H{m}",
            "away": f"A{m}",
            "strategy": strategy,
            **({"shadow": True} if shadow else {}),
        }

    real = [t(1), t(2), t(3, strategy="edge12"), t(4)]
    shadow = [t(5, shadow=True), t(4, shadow=True)]  # match 4: shadow first, real later
    old = paper.old_rule_trades(real, shadow)
    assert {x["id"] for x in old} == {
        "E0|2627|H1|A1|lean",
        "E0|2627|H2|A2|lean",
        "E0|2627|H5|A5|lean|shadow",
        "E0|2627|H4|A4|lean|shadow",
    }
    assert {x["id"] for x in paper.old_rule_trades(real, [])} == {
        "E0|2627|H1|A1|lean",
        "E0|2627|H2|A2|lean",
        "E0|2627|H4|A4|lean",
    }


def lean_trade(i, odds, status, clv, hours, *, shadow=False, version=2):
    opened = pd.Timestamp(tr.LEAN_RANGE_FROM) + pd.Timedelta(hours=hours)
    t = {
        "id": f"E0|2627|H{i}|A{i}|lean" + (tr.SHADOW_SUFFIX if shadow else ""),
        "source": "live",
        "league": "E0",
        "season": "2627",
        "home": f"H{i}",
        "away": f"A{i}",
        "market": "home",
        "kickoff": (opened + pd.Timedelta(days=1)).isoformat(),
        "opened_at": opened.isoformat(),
        "odds": odds,
        "stake": 10.0,
        "edge": 0.05,
        "model_p": 0.5,
        "status": status,
        "profit": {"won": 10 * (odds - 1), "lost": -10.0}.get(status, 0.0),
        "clv_dk": clv,
        "strategy": "lean",
    }
    if version:
        t["rule_version"] = version
    if shadow:
        t.update(shadow=True, excluded_by="odds_range")
    return t


def test_since_defined_judges_trades_opened_under_the_new_rule_only():
    real = [
        lean_trade(1, 2.0, "won", 0.02, 1),
        lean_trade(2, 2.5, "lost", -0.04, 2),
        lean_trade(3, 4.0, "lost", -0.10, 0.5, version=None),  # the old code, after the start
        lean_trade(4, 2.2, "won", 0.05, -10, version=None),  # before the rule: shown only
    ]
    shadow = [
        lean_trade(5, 4.5, "lost", -0.08, 3, shadow=True),
        lean_trade(6, 1.3, "won", -0.02, 4, shadow=True),
        lean_trade(7, 6.0, "open", None, 5, shadow=True),
    ]
    sec = paper.lean_shadow_section(real, shadow, lean(), NOW)
    assert sec["summary"]["trades"] == 7 and sec["shadow_trades"] == 3
    sd = sec["since_defined"]
    assert sd["defined_at"] == tr.LEAN_RANGE_FROM and sd["old_code"] == 1
    assert sd["parent"]["trades"] == 2 and sd["variant"]["trades"] == 5
    d = sd["clv_diff"]["clv_dk"]
    pool = [0.02, -0.04, -0.08, -0.02]  # won/lost with a value, as the summaries
    assert d["trades"] == 4 and d["kept"] == 2
    assert d["mean_all"] == pytest.approx(np.mean(pool))
    assert d["mean_all"] == pytest.approx(sd["variant"]["summary"]["clv_dk"])
    assert d["mean_kept"] == pytest.approx(sd["parent"]["summary"]["clv_dk"])
    assert d["diff"] == pytest.approx(np.mean([0.02, -0.04]) - np.mean(pool))
    assert d["kept_is"].startswith("in range")


def test_the_card_says_whether_its_pick_is_in_range(tmp_path):
    blends = {"leagues": {"E0": {"coef": [0.0, 0.0, 0.0, 1.0], "sigma": 0.005, "matches": 900}}}
    out = card()  # the model-only blend picks the draw at 5.0
    inside = card(
        odds={**ODDS_IN, "draw": 2.9},
        away="Spurs",
        p={**out["p"], "draw": 0.40, "home": 0.45, "away": 0.15},
    )
    for c in (out, inside):
        o = c["odds"]
        c["implied"] = {m: 1 / o[m] for m in ("home", "draw", "away")}
    data = {"league_code": "E0", "fixtures": [out, inside]}
    assert add_lean(data, blends) == 2
    a, b = (f["lean_pick"] for f in data["fixtures"])
    assert a["market"] == "draw" and a["in_range"] is False and a["odds_range"] == [1.5, 3.0]
    assert b["market"] == "draw" and b["odds"] == 2.9 and b["in_range"] is True


def test_awaiting_results_asks_espn_about_shadow_entries_too(tmp_path):
    paper.append_events(
        tmp_path, [{"type": "open", **lean_trade(5, 4.5, "open", None, -48, shadow=True)}]
    )
    k = pd.Timestamp(tr.LEAN_RANGE_FROM) - pd.Timedelta(hours=24)
    out = paper.awaiting_results(tmp_path, ["E0"], k + pd.Timedelta(hours=3))
    assert [(x["kind"], x["home"]) for x in out] == [("match", "H5")]


def test_in_range_lean_still_opens_and_the_twelve_percent_is_untouched():
    led, shadow = {}, {}
    rule = tr.paper_threshold(None, "E0")
    ev, _ = paper.update_ledger(
        led,
        [card(p_bet=P_BET_IN, odds=ODDS_IN)],
        src(),
        None,
        NOW,
        threshold=0.12,
        rule=rule,
        lean=lean(),
        shadow=shadow,
    )
    assert set(led) == {LEAN_ID, "E0|2627|Arsenal|Leeds"} and shadow == {}
    assert led["E0|2627|Arsenal|Leeds"]["market"] == "draw"  # 12% bets 5.0, no range


def test_the_new_rule_is_backcast_from_leans_start_and_the_ledger_keeps_everything():
    real = [
        lean_trade(1, 2.0, "won", 0.02, -200, version=None),  # before the rule, in range
        lean_trade(2, 4.0, "lost", -0.06, -150, version=None),  # before, out: cut
        lean_trade(3, 1.4, "won", -0.01, -100, version=None),  # before, out: cut
        lean_trade(4, 2.5, "lost", -0.03, 2),  # the new rule
    ]
    e12 = {**lean_trade(9, 5.0, "lost", -0.1, -120, version=None), "strategy": "edge12"}
    e12["id"] = "E0|2627|H9|A9"
    shadow = [lean_trade(5, 6.0, "lost", -0.08, 3, shadow=True)]
    pf = paper.portfolios_section(
        {},
        [*real, e12],
        {},
        tr.paper_threshold(None, "E0"),
        None,
        paper.match_strategies({"E0": lean()}, tr.paper_threshold(None, "E0")),
        shadow_trades=shadow,
    )
    live = next(p for p in pf if p["id"] == "moneyline")["live"]
    sts = {x["key"]: x for x in live["strategies"]}
    new = sts["lean"]
    assert new["label"] == "Blend Lean (new rule)"
    assert set(new["trade_ids"]) == {real[0]["id"], real[3]["id"]}
    bc = new["backcast"]
    assert bc["applied"] and bc["trades"] == 1 and bc["settled"] == 1
    assert bc["from"] == bc["to"] == real[0]["opened_at"]
    assert bc["lean_started"] == real[0]["opened_at"] and bc["until"] == tr.LEAN_RANGE_FROM
    old = sts["lean_all_odds"]
    assert set(old["trade_ids"]) == {t["id"] for t in real} | {shadow[0]["id"]}
    # The cut trades stay in the ledger's combined record.
    assert live["summary"]["trades"] == 5 and len(live["trades"]) == 5
    pre = old["before_defined"]
    assert pre["descriptive"] and pre["until"] == tr.LEAN_RANGE_FROM
    assert pre["parent"]["trades"] == 1 and pre["variant"]["trades"] == 3
    d = pre["clv_diff"]["clv_dk"]
    assert d["diff"] == pytest.approx(0.02 - np.mean([0.02, -0.06, -0.01]))
    sd = old["since_defined"]
    assert sd["parent"]["trades"] == 1 and sd["variant"]["trades"] == 2
