"""Every book in the live h2h call (owner, 28 Oct): parsing, the every-book log, the best
US price and Pinnacle's live close on the paper trades."""

import numpy as np
import pandas as pd
import pytest
from test_paper import KICKOFF, NOW, card, src

from soccer_stats import odds_feed as feed
from soccer_stats import odds_log as ol
from soccer_stats import paper
from soccer_stats import paper_dnb as pdnb
from soccer_stats import trades as tr
from soccer_stats.odds import devig_shin

TIP = KICKOFF.isoformat()


def book(key, h, d, a, updated="2026-10-08T11:00:00Z", full=True):
    outcomes = [{"name": "Arsenal", "price": h}, {"name": "Leeds", "price": a}]
    if full:
        outcomes.append({"name": "Draw", "price": d})
    return {
        "key": key,
        "last_update": updated,
        "markets": [{"key": "h2h", "last_update": updated, "outcomes": outcomes}],
    }


def event(*books):
    return {
        "home_team": "Arsenal",
        "away_team": "Leeds",
        "commence_time": TIP,
        "bookmakers": list(books),
    }


EV = event(
    book("draftkings", 1.40, 5.0, 7.5),
    book("pinnacle", 1.45, 4.8, 7.6, updated="2026-10-08T11:30:00Z"),
    book("fanduel", 1.42, 5.2, 7.0),
    book("betmgm", 1.38, 5.0, 8.0),
    book("bovada", 1.50, 4.0, 6.0, full=False),  # no draw: left out
)


def test_every_book_parsed_and_the_best_us_price_skips_pinnacle():
    b = feed.book_prices(EV)
    assert set(b) == {"draftkings", "pinnacle", "fanduel", "betmgm"}
    assert b["fanduel"] == {
        "home": 1.42,
        "draw": 5.2,
        "away": 7.0,
        "last_update": "2026-10-08T11:00:00Z",
    }
    best = feed.best_prices(b)
    assert best["home"] == {"odds": 1.42, "book": "fanduel"}  # Pinnacle's 1.45 isn't a US book
    assert best["draw"] == {"odds": 5.2, "book": "fanduel"} and best["away"]["book"] == "betmgm"
    assert feed.best_prices(None) is None and feed.best_prices({}) is None
    df = feed.parse_odds([EV])
    assert df.loc[0, "odds_home"] == 1.40 and set(df.loc[0, "books"]) == set(b)
    fx = pd.DataFrame([{"home": "Arsenal", "away": "Leeds", "kickoff": KICKOFF}])
    out = feed.apply_odds(fx, df)
    assert out.loc[0, "books"]["pinnacle"]["home"] == 1.45
    assert feed.apply_odds(fx, df.iloc[0:0]).loc[0, "books"] is None


FETCHED = NOW - pd.Timedelta(minutes=30)


def data_with_books(books, fetched=FETCHED):
    c = card()
    c["books"] = books
    c["best_us"] = feed.best_prices(books)
    return {"fixtures": [c], "odds_source": src(fetched), "league_code": "E0"}


def test_the_every_book_log_is_its_own_file_and_never_mixes_with_draftkings(tmp_path):
    data = data_with_books(feed.book_prices(EV))
    rows = ol.book_rows_from_data(data, NOW)
    assert {r["book"] for r in rows} == {"draftkings", "pinnacle", "fanduel", "betmgm"}
    pin = next(r for r in rows if r["book"] == "pinnacle")
    assert pin["prices"] == {"home": 1.45, "draw": 4.8, "away": 7.6}
    assert pin["fetched_at"] == "2026-10-08T11:30:00+00:00" and pin["time_source"] == "last_update"
    f = devig_shin(np.array([1.45, 4.8, 7.6]))
    assert pin["fair"]["home"] == pytest.approx(f[0], abs=1e-6)
    assert ol.append(tmp_path, rows, kind=ol.BOOKS) == 4
    assert ol.append(tmp_path, rows, kind=ol.BOOKS) == 0  # deduplicated
    assert (tmp_path / "odds_log" / "E0_books_2026-10.jsonl").exists()
    assert ol.load(tmp_path).empty  # the DraftKings log reads none of them
    books = ol.load(tmp_path, kind=ol.BOOKS)
    r = ol.last_before(books, "Arsenal", "Leeds", TIP, "h2h", book="pinnacle")
    assert r["book"] == "pinnacle" and r["prices"]["home"] == 1.45
    assert ol.last_before(books, "Arsenal", "Leeds", TIP, "h2h", book="caesars") is None
    # A price quoted at or after kickoff isn't logged.
    late = data_with_books({"pinnacle": {"home": 1.2, "draw": 6, "away": 12, "last_update": TIP}})
    assert ol.book_rows_from_data(late, NOW) == []


def test_trades_record_the_best_us_price_and_pinnacles_live_close(tmp_path):
    books = feed.book_prices(EV)
    c = card()
    c["books"], c["best_us"] = books, feed.best_prices(books)
    led: dict = {}
    ev, _ = paper.update_ledger(led, [c], src(), None, NOW)
    t = led["E0|2627|Arsenal|Leeds"]
    assert t["market"] == "draw" and t["best_us_odds"] == 5.2 and t["best_us_book"] == "fanduel"
    ol.append(tmp_path, ol.book_rows_from_data(data_with_books(books), NOW), kind=ol.BOOKS)
    log = ol.load(tmp_path, kind=ol.BOOKS)
    ev, _ = paper.update_ledger(led, [], src(), None, NOW, books_log=log)
    pin = {"home": 1.45, "draw": 4.8, "away": 7.6}
    assert t["pinnacle_live_close_prices"] == pin
    assert t["clv_pinnacle_live"] == pytest.approx(tr.clv(5.0, "draw", pin))
    assert t["pinnacle_live_minutes_before"] == pytest.approx(
        (KICKOFF - pd.Timestamp("2026-10-08T11:30Z")) / pd.Timedelta(minutes=1)
    )
    assert any(e.get("clv_pinnacle_live") is not None for e in ev)
    # Nothing newer: no event; an older quote never replaces it.
    assert paper.update_ledger(led, [], src(), None, NOW, books_log=log)[0] == []
    old = {
        "pinnacle": {"home": 1.3, "draw": 6.0, "away": 9.0, "last_update": "2026-10-08T09:00:00Z"}
    }
    ol.append(tmp_path, ol.book_rows_from_data(data_with_books(old), NOW), kind=ol.BOOKS)
    log2 = ol.load(tmp_path, kind=ol.BOOKS)
    assert paper.update_ledger(led, [], src(), None, NOW, books_log=log2)[0] == []
    assert t["pinnacle_live_close_prices"] == pin
    s = tr.summarize(pd.DataFrame([{**t, "status": "won", "profit": 40.0}]))
    assert s["clv_pinnacle_live"] == pytest.approx(t["clv_pinnacle_live"])


def test_a_draw_no_bet_trade_gets_pinnacles_live_close_as_draw_no_bet(tmp_path):
    books = feed.book_prices(EV)
    c = card()
    c["books"] = books
    rules = {"edge12_dnb": tr.dnb_rule(tr.paper_threshold(None), "edge12_dnb")}
    led: dict = {}
    pdnb.update_ledger(led, [c], src(), None, NOW, "E0", rules)
    ol.append(tmp_path, ol.book_rows_from_data(data_with_books(books), NOW), kind=ol.BOOKS)
    log = ol.load(tmp_path, kind=ol.BOOKS)
    pdnb.update_ledger(led, [], src(), None, NOW, "E0", rules, books_log=log)
    t = led["E0|2627|Arsenal|Leeds|dnb"]
    want = tr.dnb_clv(t["odds"], t["market"], {"home": 1.45, "draw": 4.8, "away": 7.6})[0]
    assert t["clv_pinnacle_live"] == pytest.approx(want)
