"""The logged-only corner books (lead, 8 Nov): FanDuel, BetMGM, BetRivers and Bovada asked
in the same corner call as Pinnacle and DraftKings, one region, 1 credit; dropped for the
month if a call with them bills more. The paper tracks read Pinnacle and DraftKings only."""

import json

import pandas as pd
from test_corners_dk import both_body, outcomes
from test_corners_live import LOOK, K, card, dk_cache, fake_get

from soccer_stats import corners_live as cl


def shop_book(key, *outs):
    return {
        "key": key,
        "last_update": (K - pd.Timedelta(hours=24)).isoformat(),
        "markets": [{"key": cl.MARKET, "outcomes": [x for o in outs for x in o]}],
    }


def shop_body():
    b = both_body()
    b["bookmakers"].append(shop_book("fanduel", outcomes("Arsenal", 4.5, 2.05, 1.8)))
    b["bookmakers"].append(shop_book("bovada", outcomes("Leeds United", 3.5, 1.95)))
    b["bookmakers"].append(shop_book("williamhill_us", outcomes("Arsenal", 4.5, 2.5, 1.5)))
    return b


def test_book_list_is_one_region_and_keeps_the_tracks_books():
    assert cl.BOOKS == ("pinnacle", "draftkings")
    assert cl.LOG_BOOKS[:2] == cl.BOOKS and set(cl.SHOP_BOOKS) <= set(cl.LOG_BOOKS)
    assert len(cl.LOG_BOOKS) <= cl.MAX_BOOKS == 10
    assert cl.books_for("E0") == cl.LOG_BOOKS and cl.books_for("E1") == cl.LOG_BOOKS
    assert cl.books_for("E2") == ("pinnacle",) and cl.books_for("E3") == ("pinnacle",)
    assert cl.books_for("SP1", shop=False) == cl.BOOKS


def test_shop_rows_are_logged_with_their_book_and_others_ignored():
    rows = cl.rows_from_body(shop_body(), card(), "ev1", "look", LOOK)
    by = {}
    for r in rows:
        by.setdefault(r["book"], []).append(r)
    assert set(by) == {"pinnacle", "draftkings", "fanduel", "bovada"}  # not williamhill_us
    fd = by["fanduel"][0]
    assert fd["over"] == 2.05 and fd["under"] == 1.8 and fd["fair_over"] is not None
    bv = by["bovada"][0]  # one-sided: kept, no fair price
    assert bv["team"] == "Leeds" and bv["under"] is None and bv["margin"] is None
    # The tracks' books are exactly as without the extra books.
    base = cl.rows_from_body(both_body(), card(), "ev1", "look", LOOK)
    assert [r for r in rows if r["book"] in cl.BOOKS] == base


def test_one_call_one_credit_and_summary_counts_the_extra_books(tmp_path):
    raw, out = tmp_path / "raw", tmp_path / "out"
    raw.mkdir()
    dk_cache(raw)
    calls = []
    s = cl.run([card()], out, raw_dir=raw, api_key="k", now=LOOK, get=fake_get(calls, shop_body()))
    assert len(calls) == 1 and calls[0][1]["bookmakers"] == cl.LOG_BOOKS_PARAM
    assert s["credits"] == 1 and s["shop_books"] is True
    assert s["rows_by_book"]["fanduel"] == 1 and s["rows_by_book"]["bovada"] == 1
    assert ", 1 fanduel, 1 bovada)" in cl.summary_line(s)
    rec = json.loads((out / "calls.jsonl").read_text())
    assert rec["books"] == cl.LOG_BOOKS_PARAM and rec["cost"] == 1


def test_a_dearer_call_drops_the_extra_books_for_the_month(tmp_path):
    raw, out = tmp_path / "raw", tmp_path / "out"
    raw.mkdir()
    dk_cache(raw)
    calls = []
    s = cl.run(
        [card()], out, raw_dir=raw, api_key="k", now=LOOK, get=fake_get(calls, shop_body(), cost=2)
    )
    assert s["shop_books"] is False and "billed 2" in s["shop_note"]
    assert "extra books off" in cl.summary_line(s)
    rec = [json.loads(x) for x in (out / "calls.jsonl").read_text().splitlines()]
    assert cl.shop_off(rec, LOOK) is True
    assert cl.shop_off(rec, LOOK + pd.Timedelta(days=40)) is False  # next month: asked again
    ok = [{**rec[0], "cost": 1}]
    assert cl.shop_off(ok, LOOK) is False
    old = [{**rec[0], "books": cl.BOOKS_PARAM}]  # a dear call without them doesn't count
    assert cl.shop_off(old, LOOK) is False
