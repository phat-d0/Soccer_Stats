"""A club renamed only in case opens no second trade (lead, 28 Oct): ESPN listed Viking FK
as "VIKING FK" in one build and "Viking FK" from the next, and the Champions League card,
which takes ESPN's names, opened a second Model 12% trade and draw no bet trade."""

import pandas as pd
from test_paper import NOW, card, src

from soccer_stats import odds_feed, paper, publish
from soccer_stats import odds_log as ol
from soccer_stats import paper_dnb as pdnb
from soccer_stats import trades as tr

KO = NOW + pd.Timedelta(days=2)


def ucl(away):
    return card(home="Aston Villa", away=away, kickoff=KO, league="UCL")


def test_holds_ignores_case_only():
    led = {"UCL|2627|Aston Villa|VIKING FK": {}}
    assert tr.holds(led, "UCL|2627|Aston Villa|VIKING FK")
    assert tr.holds(led, "UCL|2627|Aston Villa|Viking FK")
    assert not tr.holds(led, "UCL|2627|Aston Villa|Viking")
    assert tr.holds({"a|B"}, "A|b") and not tr.holds({}, "A|b")


def test_both_spellings_open_one_trade_per_strategy(tmp_path):
    ledger: dict = {}
    for i, away in enumerate(["VIKING FK", "Viking FK"]):
        now = NOW + pd.Timedelta(hours=2 * i)
        source = src(now - pd.Timedelta(minutes=10))
        paper.update_ledger(ledger, [ucl(away)], source, None, now, league="UCL")
    assert list(ledger) == ["UCL|2627|Aston Villa|VIKING FK"]  # the first spelling's only

    rules = {"edge12_dnb": tr.dnb_rule(tr.paper_threshold(None), "edge12_dnb")}
    dnb: dict = {}
    for i, away in enumerate(["VIKING FK", "Viking FK"]):
        now = NOW + pd.Timedelta(hours=2 * i)
        source = src(now - pd.Timedelta(minutes=10))
        pdnb.update_ledger(dnb, [ucl(away)], source, None, now, "UCL", rules)
    assert list(dnb) == ["UCL|2627|Aston Villa|VIKING FK|dnb"]


def test_a_case_only_split_shows_as_a_duplicate_fixture():
    cards = [ucl("VIKING FK"), ucl("Viking FK")]
    assert publish.duplicate_fixtures(cards) == [
        ("UCL", "Aston Villa", "Viking FK", KO.isoformat())
    ]


def test_domestic_feed_names_already_fold_case():
    # Domestic cards take football-data's schedule names; the feed's are matched to them
    # without case, so a feed spelling can't split a domestic card.
    known = {"Arsenal", "Leeds", "Nott'm Forest"}
    assert odds_feed._team("ARSENAL", known) == "Arsenal"
    assert odds_feed._team("leeds", known) == "Leeds"


# Closes and results match names without case too (lead, 5 Nov), so a case-only rename
# after a trade opens neither loses its close nor voids it for want of a result.

AFTER = KO + pd.Timedelta(hours=3)


def viking_results(away="Viking FK", hg=2, ag=0):
    return pd.DataFrame(
        {
            "league": ["UCL"],
            "season": ["2627"],
            "date": [pd.Timestamp(KO.date())],
            "kickoff": [KO],
            "home": ["Aston Villa"],
            "away": [away],
            "home_goals": [hg],
            "away_goals": [ag],
            "close_home": [1.30],
            "close_draw": [5.5],
            "close_away": [9.0],
            "result_source": ["espn"],
        }
    )


def odds_log(away="Viking FK"):
    fetched = KO - pd.Timedelta(minutes=20)
    return pd.DataFrame(
        {
            "home": ["Aston Villa"],
            "away": [away],
            "market": ["h2h"],
            "kickoff": [KO],
            "fetched_at": [fetched],
            "downloaded_at": [fetched],
            "prices": [{"home": 1.33, "draw": 5.25, "away": 8.5}],
            "fair": [{"home": 0.72, "draw": 0.18, "away": 0.10}],
        }
    )


def test_last_before_ignores_case():
    log = odds_log("Viking FK")
    r = ol.last_before(log, "Aston Villa", "VIKING FK", KO, "h2h")
    assert r is not None and r["away"] == "Viking FK" and r["minutes_before"] == 20.0
    assert ol.last_before(log, "aston villa", "viking fk", KO, "h2h") is not None
    assert ol.last_before(log, "Aston Villa", "Viking", KO, "h2h") is None


def test_a_renamed_club_settles_once_from_either_source():
    for source in ("results", "espn"):
        ledger: dict = {}
        now = NOW
        paper.update_ledger(ledger, [ucl("VIKING FK")], src(now), None, now, league="UCL")
        (tid,) = ledger
        assert tid == "UCL|2627|Aston Villa|VIKING FK"
        # The next build lists the club as "Viking FK": the card is the same match, so
        # neither a second trade nor a moved-match void.
        later = now + pd.Timedelta(hours=2)
        ev, _ = paper.update_ledger(
            ledger, [ucl("Viking FK")], src(later), None, later, league="UCL"
        )
        assert list(ledger) == [tid] and ledger[tid]["status"] == "open"
        assert not [e for e in ev if e.get("status") == "void"]
        res = viking_results()
        kw = {"espn": res} if source == "espn" else {}
        ev, _ = paper.update_ledger(
            ledger,
            [],
            {},
            None if source == "espn" else res,
            AFTER,
            league="UCL",
            odds_log=odds_log(),
            **kw,
        )
        t = ledger[tid]
        assert t["status"] in ("won", "lost") and t["score"] == "2-0", source
        assert t["close_fetched_at"] is not None and t.get("clv_dk") is not None, source
        again, _ = paper.update_ledger(
            ledger, [], {}, None if source == "espn" else res, AFTER, league="UCL", **kw
        )
        assert not [e for e in again if "status" in e], source  # settled once


def test_a_renamed_club_settles_its_draw_no_bet_trade_once():
    rules = {"edge12_dnb": tr.dnb_rule(tr.paper_threshold(None), "edge12_dnb")}
    dnb: dict = {}
    pdnb.update_ledger(dnb, [ucl("VIKING FK")], src(NOW), None, NOW, "UCL", rules)
    (tid,) = dnb
    later = NOW + pd.Timedelta(hours=2)
    ev = pdnb.update_ledger(dnb, [ucl("Viking FK")], src(later), None, later, "UCL", rules)
    assert list(dnb) == [tid] and dnb[tid]["status"] == "open"
    res = viking_results()
    pdnb.update_ledger(dnb, [], {}, res, AFTER, "UCL", rules)
    assert dnb[tid]["status"] in ("won", "lost") and dnb[tid]["score"] == "2-0"
    ev = pdnb.update_ledger(dnb, [], {}, res, AFTER, "UCL", rules)
    events = ev[0] if isinstance(ev, tuple) else ev
    assert not [e for e in events if "status" in e]


def test_a_renamed_club_finds_its_corner_close_and_result():
    t = {
        "home": "Aston Villa",
        "away": "VIKING FK",
        "team_side": "home",
        "line": 4.5,
        "kickoff": KO.isoformat(),
        "season": "2627",
        "league": "UCL",
    }
    q = {"downloaded_at": (KO - pd.Timedelta(minutes=5)).isoformat(), "over": 1.9}
    quotes = {("UCL", "Aston Villa", "Viking FK", KO, "home", 4.5): q}
    assert paper._quote_at(quotes, t) is q
    res = viking_results().assign(home_corners=[7], away_corners=[2])
    got = paper._corner_result(res, t)
    assert got is not None and got["home_corners"] == 7


def test_espn_matches_a_trade_whose_club_was_renamed_in_case():
    from soccer_stats import espn_results as er

    match = {"home": "Aston Villa", "away": "VIKING FK", "kickoff": KO}
    ev = {"home": "Aston Villa", "away": "Viking FK", "kickoff": KO}
    assert er.match_events(match, [ev], {"Aston Villa", "Viking FK"}) == ("matched", ev)


def test_holds_maps_a_renamed_club_to_football_datas_name():
    # DraftKings' "Racing Santander" became football-data's "Santander" on 5 Nov.
    led = {"SP1|2627|Racing Santander|Real Madrid|dnb": {}}
    assert tr.holds(led, "SP1|2627|Santander|Real Madrid|dnb")
    assert tr.holds({"SP1|2627|Santander|Espanol": {}}, "SP1|2627|Racing Santander|Espanol")
    assert not tr.holds(led, "SP1|2627|Santander|Real Madrid")  # another track's id
    assert not tr.holds(led, "SP1|2627|Santander|Getafe|dnb")
    assert tr.match_key("SP1|2627|Racing Santander|X|lean") == tr.match_key(
        "sp1|2627|SANTANDER|x|LEAN"
    )
    assert tr.match_key("odd") == "odd"


def test_duplicate_twins_keep_the_first_live_trade():
    led = {
        "SP1|2627|Racing Santander|Real Madrid|dnb": {
            "opened_at": "2026-10-19T06:07:33+00:00",
            "status": "lost",
        },
        "SP1|2627|Santander|Real Madrid|dnb": {
            "opened_at": "2026-11-01T11:15:45+00:00",
            "status": "lost",
        },
        # An old-name single is not a double.
        "SP1|2627|Racing Santander|Espanol|dnb": {
            "opened_at": "2026-10-16T17:20:43+00:00",
            "status": "won",
        },
        # A twin already voided for another reason: the next one stands (Aston Villa, 18 Oct).
        "UCL|2627|Aston Villa|VIKING FK": {
            "opened_at": "2026-10-18T17:25:48+00:00",
            "status": "void",
        },
        "UCL|2627|Aston Villa|Viking FK": {
            "opened_at": "2026-10-18T19:35:48+00:00",
            "status": "lost",
        },
    }
    assert tr.duplicate_twins(led) == [
        ("SP1|2627|Santander|Real Madrid|dnb", "SP1|2627|Racing Santander|Real Madrid|dnb")
    ]
    # Once swept, nothing more: the twin is void.
    led["SP1|2627|Santander|Real Madrid|dnb"]["status"] = "void"
    assert tr.duplicate_twins(led) == []


def test_duplicate_sweep_voids_the_later_twin_once(tmp_path):
    import json

    import pandas as pd

    from soccer_stats import paper, paper_dnb

    now = pd.Timestamp("2026-11-09 12:00", tz="UTC")
    d = tmp_path / "paper_trades"
    d.mkdir()
    rows = [
        {
            "type": "open",
            "id": "SP1|2627|Racing Santander|Real Madrid|dnb",
            "opened_at": "2026-10-19T06:07:33+00:00",
            "status": "open",
            "stake": 10.0,
        },
        {
            "type": "update",
            "id": "SP1|2627|Racing Santander|Real Madrid|dnb",
            "status": "lost",
            "profit": -10.0,
        },
        {
            "type": "open",
            "id": "SP1|2627|Santander|Real Madrid|dnb",
            "opened_at": "2026-11-01T11:15:45+00:00",
            "status": "open",
            "stake": 10.0,
        },
        {
            "type": "update",
            "id": "SP1|2627|Santander|Real Madrid|dnb",
            "status": "lost",
            "profit": -10.0,
        },
    ]
    (d / "SP1_dnb_2627.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    led = paper_dnb.load_ledger(tmp_path, "SP1")
    ev = paper.duplicate_sweep(led, now)
    assert len(ev) == 1 and ev[0]["id"] == "SP1|2627|Santander|Real Madrid|dnb"
    assert ev[0]["status"] == "void" and ev[0]["profit"] == 0.0
    assert ev[0]["void_reason"] == tr.DUPLICATE_REASON
    assert ev[0]["duplicate_of"] == "SP1|2627|Racing Santander|Real Madrid|dnb"
    paper_dnb.append_events(tmp_path, ev)
    again = paper_dnb.load_ledger(tmp_path, "SP1")
    twin = again["SP1|2627|Santander|Real Madrid|dnb"]
    assert twin["status"] == "void" and twin["profit"] == 0.0
    assert twin["void_reason"] == tr.DUPLICATE_REASON
    assert again["SP1|2627|Racing Santander|Real Madrid|dnb"]["status"] == "lost"  # untouched
    assert paper.duplicate_sweep(again, now) == []  # once only
