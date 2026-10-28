"""A club renamed only in case opens no second trade (lead, 28 Oct): ESPN listed Viking FK
as "VIKING FK" in one build and "Viking FK" from the next, and the Champions League card,
which takes ESPN's names, opened a second Model 12% trade and draw no bet trade."""

import pandas as pd
from test_paper import NOW, card, src

from soccer_stats import odds_feed, paper, publish
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
