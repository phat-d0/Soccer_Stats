"""DraftKings beside Pinnacle in the live team-corner test (owner, 20 Oct): one call for
both books, DraftKings rows (several lines, one-sided ones), a second corner strategy with
its own ids, closes and both CLVs, and the Pinnacle track exactly as before. No network."""

import json

import pandas as pd
import pytest
from test_corners_live import (
    LOOK,
    TID,
    K,
    Resp,
    _record,
    body,
    card,
    corner_results,
    dk_cache,
    fake_get,
    quote,
)

from soccer_stats import corners_live as cl
from soccer_stats import paper
from soccer_stats import trades as tr

DK_TID = TID + "|dk"


def outcomes(team, line, o=None, u=None):
    out = []
    if o is not None:
        out.append({"name": "Over", "description": team, "price": o, "point": line})
    if u is not None:
        out.append({"name": "Under", "description": team, "price": u, "point": line})
    return out


def dk_book(*outs):
    return {
        "key": "draftkings",
        "last_update": (K - pd.Timedelta(hours=24, minutes=5)).isoformat(),
        "markets": [{"key": cl.MARKET, "outcomes": [x for o in outs for x in o]}],
    }


def both_body(dk=None):
    b = body()
    if dk is None:
        dk = dk_book(
            outcomes("Arsenal", 4.5, 2.1, 1.7),
            outcomes("Arsenal", 6.5, 3.4),  # one-sided: over only
            outcomes("Leeds United", 3.5, 1.9, 1.85),
            outcomes("Leeds United", 2.5, 1.0, 1.4),  # a dead over price: dropped
        )
    b["bookmakers"].insert(1, dk)
    return b


def dk_quote(line=4.5, over=2.2, under=1.65, **kw):
    if over is not None and under is not None:
        return quote(line=line, over=over, under=under, book="draftkings", **kw)
    r = quote(line=line, book="draftkings", **kw)
    r.update(over=over, under=under, fair_over=None, fair_under=None, margin=None)
    return r


# ---------- rows ----------


def test_pinnacle_rows_are_byte_for_byte_unchanged_by_draftkings():
    before = cl.rows_from_body(body(), card(), "ev1", "look", LOOK)
    after = cl.rows_from_body(both_body(), card(), "ev1", "look", LOOK)
    pin = [r for r in after if r["book"] == "pinnacle"]
    assert [json.dumps(r) for r in pin] == [json.dumps(r) for r in before]
    assert after[: len(before)] == before  # Pinnacle's rows come first, in the same order


def test_draftkings_rows_keep_every_line_and_one_sided_ones():
    rows = [
        r
        for r in cl.rows_from_body(both_body(), card(), "ev1", "look", LOOK)
        if r["book"] != "pinnacle"
    ]
    keys = list(cl.rows_from_body(body(), card(), "ev1", "look", LOOK)[0])
    assert all(list(r) == keys for r in rows)  # the same schema, in the same order
    got = {(r["team"], r["line"]): r for r in rows}
    assert set(got) == {("Arsenal", 4.5), ("Arsenal", 6.5), ("Leeds", 3.5), ("Leeds", 2.5)}
    two = got[("Arsenal", 4.5)]
    assert two["book"] == "draftkings" and two["over"] == 2.1 and two["under"] == 1.7
    assert two["fair_over"] + two["fair_under"] == pytest.approx(1)
    assert two["margin"] == pytest.approx(1 / 2.1 + 1 / 1.7 - 1, abs=1e-6)
    assert two["p_model_over"] == pytest.approx(0.60)
    one = got[("Arsenal", 6.5)]
    assert one["over"] == 3.4 and one["under"] is None
    assert one["fair_over"] is None and one["fair_under"] is None and one["margin"] is None
    dead = got[("Leeds", 2.5)]  # 1.0 isn't a price: only the under side is kept
    assert dead["over"] is None and dead["under"] == 1.4 and dead["margin"] is None


def test_no_draftkings_in_the_body_means_pinnacle_rows_only(tmp_path):
    raw, out = tmp_path / "raw", tmp_path / "out"
    raw.mkdir()
    dk_cache(raw)
    s = cl.run([card()], out, raw_dir=raw, api_key="k", now=LOOK, get=fake_get([]))
    assert s["rows"] == 2 and s["rows_by_book"] == {"pinnacle": 2, "draftkings": 0}
    assert "(2 Pinnacle, 0 DraftKings)" in cl.summary_line(s)
    rec = json.loads((out / "calls.jsonl").read_text())
    assert rec["books"] == "pinnacle,draftkings" and rec["cost"] == 1


def test_one_call_asks_both_books_and_logs_both(tmp_path):
    raw, out, root = tmp_path / "raw", tmp_path / "out", tmp_path / "log"
    raw.mkdir()
    dk_cache(raw)
    calls = []
    s = cl.run([card()], out, raw_dir=raw, api_key="k", now=LOOK, get=fake_get(calls, both_body()))
    assert len(calls) == 1 and calls[0][1]["bookmakers"] == "pinnacle,draftkings"
    assert calls[0][1]["markets"] == cl.MARKET
    assert s["credits"] == 1 and s["rows_by_book"] == {"pinnacle": 2, "draftkings": 4}
    assert cl.log(out, root) == (6, 1)
    assert cl.log(out, root) == (0, 0)
    rows = cl.load_rows(root / "odds_log")
    assert sorted({r["book"] for r in rows}) == ["draftkings", "pinnacle"]


# ---------- budget ----------


def test_expected_cost_follows_the_dearest_corner_call_this_month():
    at = LOOK.isoformat()
    calls = [
        {"books": "fanduel,bovada,pinnacle", "cost": 3, "fetched_at": at},  # not a corner call
        {"books": "pinnacle,draftkings", "cost": 1, "fetched_at": at},
    ]
    assert cl.expected_cost(calls, LOOK) == 1
    calls.append({"books": "pinnacle,draftkings", "cost": 2, "fetched_at": at})
    assert cl.expected_cost(calls, LOOK) == 2
    # Pinnacle-only calls (League One and Two from 24 Oct) count too.
    assert cl.expected_cost([*calls, {"books": "pinnacle", "cost": 3, "fetched_at": at}], LOOK) == 3
    last_month = (LOOK - pd.Timedelta(days=40)).isoformat()
    old = [{"books": "pinnacle,draftkings", "cost": 5, "fetched_at": last_month}]
    assert cl.expected_cost(old, LOOK) == cl.COST_PER_CALL


@pytest.mark.parametrize("spent, allowed", [(748, True), (749, False)])
def test_cap_holds_if_the_two_book_call_costs_more(tmp_path, spent, allowed):
    raw, state = tmp_path / "raw", tmp_path / "state"
    raw.mkdir()
    state.mkdir()
    dk_cache(raw)
    row = {
        "event_id": "old",
        "snapshot": "look",
        "fetched_at": (LOOK - pd.Timedelta(hours=2)).isoformat(),
        "cost": 2,
        "books": "pinnacle,draftkings",
    }
    filler = {"event_id": "older", "snapshot": "look", "fetched_at": row["fetched_at"]}
    filler["cost"] = spent - 2
    (state / "corners_calls_2026-10.jsonl").write_text(
        json.dumps(row) + "\n" + json.dumps(filler) + "\n"
    )
    calls = []
    s = cl.run(
        [card()], tmp_path / "o", [state], raw_dir=raw, api_key="k", now=LOOK, get=fake_get(calls)
    )
    assert (len(calls) == 1) == allowed  # 748 + 2 <= 750; 749 + 2 > 750
    if not allowed:
        assert s["stop"].startswith("monthly cap")


def test_a_dearer_call_mid_run_raises_the_next_calls_budget(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    events = (("ev1", "Arsenal", "Leeds United", K), ("ev2", "Chelsea", "Burnley", K))
    dk_cache(raw, events=events, credits=3003)
    n = []

    def get(url, params=None, timeout=None):
        n.append(url)
        return Resp(both_body(), left=3001, cost="2")  # billed 2, not the 1 assumed

    cards = [card(), card("Chelsea", "Burnley")]
    s = cl.run(cards, tmp_path / "o", raw_dir=raw, api_key="k", now=LOOK, get=get)
    # First call: 3003 - 1 >= 3000, so it runs; it costs 2 and leaves 3001. Budgeted at
    # 1 the second call would still run (3001 - 1 = 3000); at 2 the reserve stops it.
    assert len(n) == 1 and s["stop"].startswith("reserve") and s["credits"] == 2


def test_the_reserve_uses_the_expected_cost(tmp_path):
    raw, state = tmp_path / "raw", tmp_path / "state"
    raw.mkdir()
    state.mkdir()
    dk_cache(raw, credits=3001)
    _record(state, 2)
    rec = json.loads((state / "corners_calls_2026-10.jsonl").read_text())
    rec["books"] = "pinnacle,draftkings"
    (state / "corners_calls_2026-10.jsonl").write_text(json.dumps(rec) + "\n")
    calls = []
    s = cl.run(
        [card()], tmp_path / "o", [state], raw_dir=raw, api_key="k", now=LOOK, get=fake_get(calls)
    )
    assert calls == [] and s["stop"].startswith("reserve")


# ---------- the card ----------


def test_card_shows_draftkings_quotes_beside_pinnacle(tmp_path):
    d = tmp_path / "state"
    d.mkdir()
    rows = [
        quote(over=2.0, under=1.85),
        # A newer DraftKings quote at the same line never replaces Pinnacle's.
        dk_quote(over=2.3, under=1.6, at=LOOK + pd.Timedelta(minutes=30)),
        dk_quote(line=6.5, over=3.4, under=None),
    ]
    (d / "E0_corners_2026-10.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    cards = [card()]
    assert cl.add_quotes(cards, [d]) == 1
    assert cl.add_quotes(cards, [d], book="draftkings") == 1
    pin, dk = cards[0]["corners"]["pinnacle"], cards[0]["corners"]["draftkings"]
    assert pin["book"] == "pinnacle" and [q["over"] for q in pin["home"]] == [2.0]
    assert dk["book"] == "draftkings" and [q["line"] for q in dk["home"]] == [4.5, 6.5]
    assert set(dk["home"][0]) == set(pin["home"][0])  # the same shape
    two, one = dk["home"]
    assert two["over"] == 2.3 and two["pick"]["side"] == "over"
    assert two["pick"]["edge"] == pytest.approx(0.6 * 2.3 - 1)
    assert one["under"] is None and one["fair_over"] is None and one["margin"] is None
    # P(over 6.5) = 1 - 0.70 = 0.30; 0.30 x 3.4 - 1 = 2%: no pick on the one side priced.
    assert one["p_over"] == pytest.approx(0.30) and one["pick"] is None


# ---------- the second corner strategy ----------


def _book(tmp_path, cards, rows, now, results=None):
    ledger = paper.load_corner_ledger(tmp_path, "E0")
    ev = paper.update_corner_ledger(ledger, cards, rows, results, now, "E0")
    paper.append_corner_events(tmp_path, ev)
    return paper.load_corner_ledger(tmp_path, "E0"), ev


NOW = LOOK + pd.Timedelta(minutes=10)


def test_pinnacle_trades_are_unchanged_by_draftkings_rows(tmp_path):
    pin_rows = [quote(), quote(side="away", line=3.5, over=1.95, under=1.9)]
    alone, ev_alone = _book(tmp_path / "a", [card()], pin_rows, NOW)
    both, ev_both = _book(tmp_path / "b", [card()], pin_rows + [dk_quote()], NOW)
    assert both[TID] == alone[TID]
    assert ev_both[0] == ev_alone[0]  # Pinnacle's open event first, identical
    assert alone[TID]["id"] == "E0|2627|Arsenal|Leeds|corners|home|4.5"
    assert tr.corner_strategy_of(alone[TID]) == "pinnacle"


def test_draftkings_trade_has_its_own_id_book_and_strategy(tmp_path):
    rows = [quote(), dk_quote()]
    led, ev = _book(tmp_path, [card()], rows, NOW)
    assert [e["type"] for e in ev] == ["open", "open"] and set(led) == {TID, DK_TID}
    t = led[DK_TID]
    assert t["bookmaker"] == "draftkings" and t["strategy"] == "draftkings"
    assert tr.corner_strategy_of(t) == "draftkings" and tr.portfolio_of(t) == "corners"
    assert t["odds"] == 2.2 and t["edge"] == pytest.approx(0.6 * 2.2 - 1)
    assert (t["threshold"], t["rule"], t["p_source"]) == (0.12, "fixed_raw", "model_f")
    assert t["stake"] == 10.0 and t["side"] == "over" and t["line"] == 4.5
    # Entry price as the close, both yardsticks from the start.
    fair_dk = tr.devig_pair(2.2, 1.65)[0]
    fair_pin = tr.devig_pair(2.0, 1.85)[0]
    assert t["close_odds"] == 2.2 and t["clv_dk"] == pytest.approx(2.2 * fair_dk - 1)
    assert t["clv_pinnacle"] == pytest.approx(2.2 * fair_pin - 1) and t["beat_close_pinnacle"]
    assert t["pinnacle_close_prices"] == {"over": 2.0, "under": 1.85}
    _, ev2 = _book(tmp_path, [card()], rows, NOW + pd.Timedelta(minutes=30))
    assert not any(e["type"] == "open" for e in ev2)  # one per match, team, line and book


def test_draftkings_only_line_trades_without_a_pinnacle_yardstick(tmp_path):
    led, _ = _book(tmp_path, [card()], [dk_quote(line=3.5, over=3.0, under=1.3)], NOW)
    tid = "E0|2627|Arsenal|Leeds|corners|home|3.5|dk"
    t = led[tid]  # P(over 3.5) = 0.78: 0.78 x 3.0 - 1 = 134%
    assert t["clv_pinnacle"] is None and t.get("pinnacle_close_prices") is None
    assert t["clv_dk"] is not None


def test_one_sided_draftkings_quote_trades_but_has_no_fair_close(tmp_path):
    q = dk_quote(line=3.5, over=3.0, under=None)
    led, ev = _book(tmp_path, [card()], [q], NOW)
    t = led["E0|2627|Arsenal|Leeds|corners|home|3.5|dk"]
    assert t["side"] == "over" and t["close_odds"] == 3.0
    assert t["close_prices"] == {"over": 3.0, "under": None}
    assert t["clv_dk"] is None and t["beat_close_dk"] is None
    # An under-only quote can't price an over trade's close: the close isn't moved.
    late = dk_quote(line=3.5, over=None, under=1.4, at=K - pd.Timedelta(minutes=20))
    led, ev = _book(tmp_path, [card()], [q, late], K - pd.Timedelta(minutes=15))
    assert not ev and led["E0|2627|Arsenal|Leeds|corners|home|3.5|dk"]["close_odds"] == 3.0


def test_draftkings_close_and_both_clvs_then_settlement(tmp_path):
    _book(tmp_path, [card()], [quote(), dk_quote()], NOW)
    dk_close = dk_quote(over=1.9, under=1.9, at=K - pd.Timedelta(minutes=20), snapshot="close")
    pin_close = quote(over=1.8, under=2.05, at=K - pd.Timedelta(minutes=21), snapshot="close")
    late = dk_quote(over=1.5, under=2.6, at=K + pd.Timedelta(minutes=5), snapshot="close")
    rows = [quote(), dk_quote(), dk_close, pin_close, late]
    led, ev = _book(tmp_path, [card()], rows, K - pd.Timedelta(minutes=15))
    t, p = led[DK_TID], led[TID]
    fd, fp = tr.devig_pair(1.9, 1.9)[0], tr.devig_pair(1.8, 2.05)[0]
    assert t["close_odds"] == 1.9 and t["close_snapshot"] == "close"
    assert t["close_minutes_before"] == 20.0
    assert t["clv_dk"] == pytest.approx(2.2 * fd - 1) and t["beat_close_dk"]
    assert t["clv_pinnacle"] == pytest.approx(2.2 * fp - 1)
    assert t["pinnacle_close_fetched_at"] == pin_close["downloaded_at"]
    # The Pinnacle trade closes on Pinnacle's quote only, as before.
    assert p["close_odds"] == 1.8 and p["clv_pinnacle"] == pytest.approx(2.0 * fp - 1)
    assert p.get("clv_dk") is None
    assert {e["id"] for e in ev} == {TID, DK_TID}
    led, _ = _book(tmp_path, [], rows, K + pd.Timedelta(hours=3), corner_results(7))
    for tid, odds in ((TID, 2.0), (DK_TID, 2.2)):
        assert led[tid]["status"] == "won" and led[tid]["profit"] == pytest.approx(10 * (odds - 1))
        assert led[tid]["result_source"] == "football-data"
    # The update fields survive a reload from the ledger file.
    assert led[DK_TID]["clv_dk"] == pytest.approx(2.2 * fd - 1)


def test_corner_strategy_of_defaults_to_pinnacle():
    assert tr.corner_strategy_of({}) == "pinnacle"
    assert tr.corner_strategy_of({"bookmaker": "pinnacle"}) == "pinnacle"
    assert tr.corner_strategy_of({"bookmaker": "draftkings"}) == "draftkings"
    assert tr.corner_strategy_of({"strategy": "draftkings", "bookmaker": "x"}) == "draftkings"
    assert tr.corners_rule() == {**tr.corners_rule("pinnacle")}
    assert tr.corners_rule()["book"] == "pinnacle" and tr.corners_rule()["note"] == tr.CORNERS_NOTE
    dk = tr.corners_rule("draftkings")
    assert dk["book"] == "draftkings" and dk["threshold"] == 0.12 and dk["p_source"] == "model_f"


def test_paper_run_splits_corner_strategies(tmp_path):
    from test_paper import card as match_card
    from test_paper import src

    od = tmp_path / "odds_log"
    od.mkdir()
    rows = [quote(), dk_quote(), dk_quote(side="away", line=2.5, over=2.5, under=1.5)]
    (od / "E0_corners_2026-10.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    m = match_card(kickoff=K, league="E0")
    m["corners"] = card()["corners"]
    data = {"fixtures": [m], "odds_source": src(NOW - pd.Timedelta(minutes=30)), "portfolio": {}}
    paper.run(data, tmp_path, None, now=NOW)
    c = next(p for p in data["portfolio"]["portfolios"] if p["id"] == "corners")["live"]
    assert c["summary"]["trades"] == 3 and len(c["trades"]) == 3  # combined, for older app code
    assert c["rule"] == tr.corners_rule()
    st = {s["key"]: s for s in c["strategies"]}
    assert [s["key"] for s in c["strategies"]] == [
        "pinnacle",
        "pinnacle_below60",
        "draftkings",
        "draftkings_below60",
    ]  # each track, then its below-60% shadow (test_corner_variants.py)
    assert st["pinnacle"]["label"] == "Model (f) 12% vs Pinnacle"
    assert st["draftkings"]["label"] == "Model (f) 12% vs DraftKings"
    assert st["pinnacle"]["trade_ids"] == [TID]
    assert set(st["draftkings"]["trade_ids"]) == {
        DK_TID,
        "E0|2627|Arsenal|Leeds|corners|away|2.5|dk",
    }
    assert st["draftkings"]["rule"]["book"] == "draftkings"
    assert st["pinnacle"]["summary"]["trades"] == 1 and st["draftkings"]["summary"]["trades"] == 2
    assert set(st["pinnacle"]) >= {"key", "label", "rule", "summary", "breakdowns", "by_league"}
    json.dumps(data)


def test_corner_ledger_ids_never_collide_across_books(tmp_path):
    led, _ = _book(tmp_path, [card()], [quote(), dk_quote()], NOW)
    lines = (tmp_path / "paper_trades" / "E0_corners_2627.jsonl").read_text().splitlines()
    ids = [json.loads(x)["id"] for x in lines]
    assert ids == [TID, DK_TID] and len(set(ids)) == 2
