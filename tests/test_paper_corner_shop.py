"""The corner line shop vs Pinnacle paper track (paper_corner_shop; owner, 24 Nov 2026):
look only, same-call fresh quotes, bettable books only (Bovada recorded, never bet), 2% judged
and 1% shadow with their own entries, one per match x team, counted from 8 Nov (no backcast),
closes per PR #258, the book's own close, settlement and the paper.run wiring. No outcome is read
to enter."""

import json

import pandas as pd
import pytest

from soccer_stats import paper
from soccer_stats import paper_corner_shop as pcs
from soccer_stats import trades as tr

K = pd.Timestamp("2026-11-15T15:00:00Z")
LOOK = K - pd.Timedelta(hours=20)
CLOSE = K - pd.Timedelta(minutes=15)
NOW = K + pd.Timedelta(hours=4)
DEFINED = pd.Timestamp("2026-11-25T12:00:00Z")


def row(book, over, under, side="home", line=4.5, at=LOOK, snapshot="look", age_min=5, **kw):
    r = {
        "market": "team_corners",
        "league": "SP1",
        "home": "Sevilla",
        "away": "Getafe",
        "kickoff": K.isoformat(),
        "event_id": "ev1",
        "snapshot": snapshot,
        "book": book,
        "team": "Sevilla" if side == "home" else "Getafe",
        "side": side,
        "line": line,
        "over": over,
        "under": under,
        "downloaded_at": at.isoformat(),
        "fetched_at": (at - pd.Timedelta(minutes=age_min)).isoformat(),
    }
    r.update(kw)
    return r


def base(fd_over=2.20):
    """Pinnacle 2.00/1.85 (fair over ~0.4805); FanDuel's over at `fd_over` (2.20: a 5.7% gap)."""
    return [
        row("pinnacle", 2.00, 1.85),
        row("fanduel", fd_over, 1.70),
        row("draftkings", 1.95, 1.80),
        row("bovada", 2.40, 1.60),  # the best price, but Bovada is never bet
        row("pinnacle", 1.95, 1.90, at=CLOSE, snapshot="close"),
        row("fanduel", 2.00, 1.75, at=CLOSE, snapshot="close"),
    ]


def results(hc=7, ac=3):
    return pd.DataFrame(
        {"league": ["SP1"], "season": ["2627"], "date": [pd.Timestamp("2026-11-15")],
         "home": ["Sevilla"], "away": ["Getafe"], "home_corners": [hc], "away_corners": [ac]}
    )  # fmt: skip


def fair_over():
    return tr.devig_pair(2.00, 1.85)[0]


def test_bets_the_best_bettable_book_never_bovada_with_its_gap():
    (e,) = pcs.entries(base())[pcs.KEY]
    assert e["book"] == "fanduel" and e["bet"] == "over" and e["odds"] == 2.20
    assert e["gap"] == pytest.approx(2.20 * fair_over() - 1)
    assert e["gaps"]["bovada"]["over"] > e["gap"]  # recorded, not bet


def test_threshold_two_percent_and_the_one_percent_shadow():
    t = 1.015 / fair_over()  # a 1.5% gap: the shadow only
    got = pcs.entries(base(fd_over=round(t, 4)))
    assert not got[pcs.KEY] and len(got[pcs.SHADOW_KEY]) == 1


def test_stale_or_other_call_quotes_never_count():
    rows = base()
    rows[1] = row("fanduel", 2.20, 1.70, age_min=200)  # updated over 3 h before download
    assert not pcs.entries(rows)[pcs.KEY]
    rows[1] = row("fanduel", 2.20, 1.70, at=LOOK + pd.Timedelta(minutes=1))  # another call
    assert not pcs.entries(rows)[pcs.KEY]
    rows = base()
    rows[0] = row("pinnacle", 2.00, None)  # Pinnacle one-sided: no fair price
    assert not pcs.entries(rows)[pcs.KEY]


def test_close_snapshot_and_whole_lines_never_enter():
    rows = [r for r in base() if r["snapshot"] == "close"]
    rows += [row("pinnacle", 2.0, 1.85, line=5.0), row("fanduel", 2.3, 1.7, line=5.0)]
    assert pcs.entries(rows) == {pcs.KEY: [], pcs.SHADOW_KEY: []}


def test_before_the_shop_books_were_logged_nothing_enters():
    early = pd.Timestamp("2026-11-07T10:00:00Z")
    rows = [row("pinnacle", 2.0, 1.85, at=early), row("fanduel", 2.3, 1.7, at=early)]
    for r in rows:
        r["kickoff"] = (early + pd.Timedelta(hours=20)).isoformat()
    assert not pcs.entries(rows)[pcs.KEY]


def test_one_per_team_the_larger_gap():
    rows = base() + [
        row("pinnacle", 1.80, 2.05, line=5.5),
        row("betmgm", 1.85, 2.30, line=5.5),  # a bigger gap on the under
    ]
    (e,) = pcs.entries(rows)[pcs.KEY]
    assert e["call"]["line"] == 5.5 and e["book"] == "betmgm" and e["bet"] == "under"


def test_ties_go_to_the_first_book_in_order():
    rows = base() + [row("draftkings", 2.20, 1.70)]
    rows = [r for r in rows if not (r["book"] == "draftkings" and r["over"] == 1.95)]
    (e,) = pcs.entries(rows)[pcs.KEY]
    assert e["book"] == "draftkings"


def test_from_the_log_then_close_book_close_and_settle(tmp_path):
    led: dict = {}
    ev = pcs.update_ledger(led, base(), results(), NOW, "SP1", DEFINED)
    t = led["SP1|2627|Sevilla|Getafe|cornershop|home"]
    assert t["from_log"] and not t.get("backcast")  # counted: the rule predates the prices
    assert t["bet_type"] == pcs.BET_TYPE and t["portfolio"] == "corners"
    assert (
        t["book"] == "fanduel"
        and t["odds"] == 2.20
        and t["model_p"] == pytest.approx(fair_over(), abs=1e-4)
    )
    assert t["clv_pinnacle"] == pytest.approx(2.20 * tr.devig_pair(1.95, 1.90)[0] - 1)
    assert t["clv_book"] == pytest.approx(2.20 * tr.devig_pair(2.00, 1.75)[0] - 1)
    assert not t["close_missing"] and t["status"] == "won" and t["profit"] == pytest.approx(12)
    shadow = led["SP1|2627|Sevilla|Getafe|cornershop1|home"]
    assert shadow["variant_of"] == pcs.KEY and shadow["descriptive"]
    assert sum(e["type"] == "open" for e in ev) == 2
    # Nothing opens twice.
    assert not [e for e in pcs.update_ledger(led, base(), results(), NOW, "SP1", DEFINED)
                if e["type"] == "open"]  # fmt: skip


def test_no_pinnacle_close_at_the_line_keeps_close_missing():
    led: dict = {}
    rows = [r for r in base() if r["snapshot"] == "look"]
    pcs.update_ledger(led, rows, None, LOOK + pd.Timedelta(hours=1), "SP1", DEFINED)
    t = led["SP1|2627|Sevilla|Getafe|cornershop|home"]
    assert t["close_missing"] == paper.NO_CLOSE_AT_LINE and t.get("clv_pinnacle") is None


def test_a_live_look_opens_live_only_before_kickoff():
    led: dict = {}
    pcs.update_ledger(led, base(), None, LOOK + pd.Timedelta(minutes=10), "SP1", LOOK)
    assert not led["SP1|2627|Sevilla|Getafe|cornershop|home"].get("from_log")
    led = {}
    pcs.update_ledger(led, base(), None, NOW, "SP1", LOOK)  # seen only after kickoff
    assert not led


def test_run_and_paper_wiring(tmp_path):
    od = tmp_path / "odds_log"
    od.mkdir()
    (od / "SP1_corners_2026-11.jsonl").write_text("\n".join(json.dumps(r) for r in base()) + "\n")
    (tmp_path / "paper_trades").mkdir()
    (tmp_path / "paper_trades" / "SP1_corners_2627.jsonl").write_text("")  # SP1 in play
    data = {"fixtures": [], "portfolio": {}}
    paper.run(data, tmp_path, None, now=NOW + pd.Timedelta(days=10), corner_results=results())
    c = next(p for p in data["portfolio"]["portfolios"] if p["id"] == "corners")["live"]
    keys = [s["key"] for s in c["strategies"]]
    assert keys[-2:] == [pcs.KEY, pcs.SHADOW_KEY]
    st = {s["key"]: s for s in c["strategies"]}
    assert st[pcs.KEY]["books"]["fanduel"] == 1 and st[pcs.KEY]["seen_books"]["bovada_at_threshold"]
    assert st[pcs.KEY]["judged"]["target"] == 300 and st[pcs.KEY]["from_log"]["trades"] == 1
    assert st[pcs.KEY]["judged"]["since"] == pcs.JUDGED_FROM and st[pcs.KEY]["judged"]["bets"] == 1
    assert st[pcs.KEY]["sizing"]["kelly"]["log"][0]["chance"] == pytest.approx(
        fair_over(), abs=1e-3
    )
    assert c["corner_shop_trades"] and c["summary"]["trades"] == 0
    assert (tmp_path / "paper_trades" / pcs.DEFINED_FILE).exists()
    assert paper.load_ledger(tmp_path, "SP1") == {}  # never read as a match ledger
    json.dumps(data, default=str)


def test_a_failure_costs_only_the_track(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(pcs, "run", boom)
    data = {"fixtures": [], "portfolio": {}}
    (tmp_path / "odds_log").mkdir()
    paper.run(data, tmp_path, None, now=NOW)
    c = next(p for p in data["portfolio"]["portfolios"] if p["id"] == "corners")["live"]
    assert pcs.KEY not in [s["key"] for s in c["strategies"]] and c["error"] is None


def test_entries_from_the_log_count_toward_the_judged_300():
    led: dict = {}
    pcs.update_ledger(led, base(), results(), NOW, "SP1", DEFINED)
    main = [t for t in led.values() if t["strategy"] == pcs.KEY]
    from soccer_stats import paper_corner_lean as pcl

    r = pcl.judged(main, pcs.JUDGED_FROM, NOW, None, pcs.VERDICT_FILE)
    assert r["bets"] == 1 and r["final"] == 1 and r["verdict"] is None
