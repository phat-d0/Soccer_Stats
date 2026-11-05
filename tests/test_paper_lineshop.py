"""Line shop vs Pinnacle (docs/lab.md, pre-registered 5 Nov): the paper track's rule."""

import json

import pandas as pd
import pytest
from test_blend_lean import lean

from soccer_stats import paper
from soccer_stats import paper_lineshop as ls
from soccer_stats import trades as tr

KICKOFF = "2026-11-08T15:00:00+00:00"
T0 = pd.Timestamp("2026-11-07T09:00:00", tz="UTC")
PIN = {"home": 2.0, "draw": 3.6, "away": 4.2}  # Pinnacle: fair ~ 0.487 / 0.271 / 0.233


def row(book, prices, logged, age=1.0, home="Arsenal", away="Leeds", kickoff=KICKOFF):
    logged = pd.Timestamp(logged)
    return {
        "league": "E0",
        "home": home,
        "away": away,
        "kickoff": kickoff,
        "market": "h2h",
        "line": None,
        "book": book,
        "prices": prices,
        "fetched_at": (logged - pd.Timedelta(minutes=age)).isoformat(),
        "logged_at": logged.isoformat(),
    }


def df(rows):
    out = pd.DataFrame(rows)
    out["kickoff"] = pd.to_datetime(out["kickoff"], utc=True)
    out["fetched_at"] = pd.to_datetime(out["fetched_at"], utc=True)
    return out.sort_values(["fetched_at", "logged_at"]).reset_index(drop=True)


def fair_home():
    return ls._fair(PIN)["home"]


def home_at(gap):
    """A home price giving `gap` against Pinnacle's fair home chance."""
    return round((1 + gap) / fair_home(), 4)


def moment(i):
    return T0 + pd.Timedelta(hours=3 * i)


def test_entry_is_the_first_moment_each_threshold_is_reached():
    rows = []
    for i, gap in enumerate([0.005, 0.015, 0.025, 0.06]):
        rows += [
            row("pinnacle", PIN, moment(i)),
            row("betrivers", {**PIN, "home": home_at(gap)}, moment(i)),
        ]
    found = {e["key"]: e for e in ls.first_entries(df(rows), ls.THRESHOLDS)}
    assert found[ls.SHADOW_KEY]["moment"] == moment(1)  # 1.5% clears the 1% shadow first
    main = found[ls.KEY]
    assert main["moment"] == moment(2)  # never the best moment (6% came later)
    assert main["odds"] == home_at(0.025) and main["book"] == "betrivers"
    assert main["market"] == "home" and main["gap"] == pytest.approx(0.025, abs=1e-4)


def test_tied_prices_go_to_the_first_book_and_the_larger_gap_wins():
    p = home_at(0.03)
    rows = [
        row("pinnacle", PIN, T0),
        row("betrivers", {**PIN, "home": p}, T0),
        row("fanduel", {**PIN, "home": p}, T0),
        row("betmgm", {**PIN, "home": p}, T0),
    ]
    e = ls.first_entries(df(rows), {ls.KEY: 0.02})[0]
    assert e["book"] == "fanduel"  # DraftKings, FanDuel, BetMGM, BetRivers
    # Two outcomes qualify at once: the larger gap.
    away = round(1.05 / ls._fair(PIN)["away"], 4)
    rows.append(row("draftkings", {**PIN, "away": away}, T0))
    e = ls.first_entries(df(rows), {ls.KEY: 0.02})[0]
    assert (e["market"], e["book"]) == ("away", "draftkings")
    # Pinnacle itself is never bet, however high its price.
    only_pin = [row("pinnacle", {**PIN, "home": 9.0}, T0)]
    assert ls.first_entries(df(only_pin), {ls.KEY: 0.02}) == []


def test_both_quotes_must_be_fresh():
    good = home_at(0.05)
    stale_book = [row("pinnacle", PIN, T0), row("betmgm", {**PIN, "home": good}, T0, age=16)]
    assert ls.first_entries(df(stale_book), {ls.KEY: 0.02}) == []
    stale_pin = [row("pinnacle", PIN, T0, age=20), row("betmgm", {**PIN, "home": good}, T0)]
    assert ls.first_entries(df(stale_pin), {ls.KEY: 0.02}) == []
    fresh = [row("pinnacle", PIN, T0, age=15), row("betmgm", {**PIN, "home": good}, T0, age=14)]
    assert len(ls.first_entries(df(fresh), {ls.KEY: 0.02})) == 1
    # A quote unchanged since an earlier moment ages: logged once, stale three hours on.
    later = [row("pinnacle", PIN, T0), row("pinnacle", PIN, moment(1))]
    later.append(row("betmgm", {**PIN, "home": PIN["home"]}, T0))  # no gap at T0
    later.append(row("betrivers", {**PIN, "home": good}, T0 - pd.Timedelta(hours=1), age=5))
    assert ls.first_entries(df(later), {ls.KEY: 0.02}) == []


def test_entry_reads_nothing_logged_after_the_moment():
    rows = [
        row("pinnacle", PIN, T0),
        row("betrivers", {**PIN, "home": home_at(0.0)}, T0),
        row("pinnacle", PIN, moment(1)),
        # Quoted before T0 but logged only at moment(1): it can't count at T0.
        {
            **row("fanduel", {**PIN, "home": home_at(0.04)}, moment(1)),
            "fetched_at": (moment(1) - pd.Timedelta(minutes=2)).isoformat(),
        },  # fmt: skip
    ]
    e = ls.first_entries(df(rows), {ls.KEY: 0.02})[0]
    assert e["moment"] == moment(1) and e["book"] == "fanduel"
    # Moments at or after kickoff are never entries.
    ko = pd.Timestamp(KICKOFF)
    late = [row("pinnacle", PIN, ko), row("betmgm", {**PIN, "home": home_at(0.05)}, ko)]
    assert ls.first_entries(df(late), {ls.KEY: 0.02}) == []


def books_log():
    return df(
        [
            row("pinnacle", PIN, moment(0)),
            row("betrivers", {**PIN, "home": home_at(0.03)}, moment(0)),
            row("pinnacle", PIN, moment(1), home="Chelsea", away="Spurs"),
            row("betmgm", {**PIN, "home": home_at(0.03)}, moment(1), home="Chelsea", away="Spurs"),
            # Closes: Pinnacle's and BetRivers' last quotes before kickoff.
            row("pinnacle", {"home": 1.9, "draw": 3.7, "away": 4.6}, "2026-11-08T14:50:00+00:00"),
            row("betrivers", {"home": 1.95, "draw": 3.6, "away": 4.4}, "2026-11-08T14:50:00+00:00"),
        ]
    )


def test_the_backcast_flag_and_one_trade_per_match_and_track():
    defined = moment(1) - pd.Timedelta(minutes=30)  # between the two matches' entries
    ledger: dict = {}
    now = moment(2)
    ev = ls.open_trades(ledger, books_log(), "E0", defined, now)
    ids = {e["id"]: e for e in ev}
    a = ids["E0|2627|Arsenal|Leeds|lineshop"]
    assert a["backcast"] is True and a["backcast_from"] == "books_log"
    b = ids["E0|2627|Chelsea|Spurs|lineshop"]
    assert not b.get("backcast")
    shadow = ids["E0|2627|Arsenal|Leeds|lineshop1"]
    assert shadow["variant_of"] == ls.KEY and shadow["descriptive"] is True
    assert "shadow" not in shadow  # the app reads `shadow` as "no trade opened" (Lean's)
    assert a["book"] == "betrivers" and a["book_name"] == "BetRivers"
    assert a["price_age_min"] == 1.0 and a["pinnacle_age_min"] == 1.0
    assert a["model_p"] == pytest.approx(fair_home(), abs=1e-4) == a["pinnacle_fair"]
    assert a["edge"] == pytest.approx(0.03, abs=1e-4) and a["stake"] == 10.0
    assert a["opened_at"] == moment(0).isoformat(timespec="seconds")
    assert tr.portfolio_of(a) == "moneyline"
    assert ls.open_trades(ledger, books_log(), "E0", defined, now) == []  # never twice


def test_close_clv_settlement_and_its_own_ledger(tmp_path):
    ledger: dict = {}
    res = pd.DataFrame(
        {
            "season": ["2627"],
            "date": [pd.Timestamp("2026-11-08")],
            "home": ["Arsenal"],
            "away": ["Leeds"],
            "home_goals": [2],
            "away_goals": [0],
        }  # fmt: skip
    )
    after = pd.Timestamp("2026-11-08T18:00:00", tz="UTC")
    ev = ls.update_ledger(ledger, books_log(), res, after, "E0", after)
    t = ledger["E0|2627|Arsenal|Leeds|lineshop"]
    close = {"home": 1.9, "draw": 3.7, "away": 4.6}
    assert t["close_prices"] == close
    assert t["clv_pinnacle"] == pytest.approx(tr.clv(t["odds"], "home", close))
    assert t["book_close_odds"] == 1.95 and t["clv_book"] is not None
    assert t["status"] == "won" and t["profit"] == pytest.approx(10 * (t["odds"] - 1), abs=0.01)
    ls.append_events(tmp_path, ev)
    names = sorted(p.name for p in (tmp_path / "paper_trades").iterdir())
    assert names == ["E0_lineshop_2627.jsonl"]
    assert paper.load_ledger(tmp_path, "E0") == {}  # never in Moneyline's record
    back = ls.load_ledger(tmp_path, "E0")
    assert back[t["id"]]["status"] == "won" and back[t["id"]]["clv_pinnacle"] == t["clv_pinnacle"]


def test_defined_at_is_written_once(tmp_path):
    first = ls.defined_at(tmp_path, moment(0))
    assert first == moment(0).isoformat(timespec="seconds")
    assert ls.defined_at(tmp_path, moment(3)) == first
    rec = json.loads((tmp_path / "paper_trades" / ls.DEFINED_FILE).read_text())
    assert rec["defined_at"] == first
    assert ls.defined_at(None, moment(3)) == moment(3).isoformat(timespec="seconds")


def test_sections_follow_blend_lean_with_the_shadow_and_stay_out_of_the_totals():
    ledger: dict = {}
    defined = moment(1) - pd.Timedelta(minutes=30)
    ls.open_trades(ledger, books_log(), "E0", defined, moment(2))
    trades = list(ledger.values())
    rule = tr.paper_threshold(None, "E0")
    pf = paper.portfolios_section(
        {},
        [],
        {},
        rule,
        None,
        paper.match_strategies({"E0": lean()}, rule),
        lineshop={"trades": trades, "defined_at": defined.isoformat()},
    )
    live = next(p for p in pf if p["id"] == "moneyline")["live"]
    keys = [s["key"] for s in live["strategies"]]
    i = keys.index(ls.KEY)
    assert keys[i + 1] == ls.SHADOW_KEY and keys.index("lean") < i < keys.index("edge12")
    main, shadow = live["strategies"][i], live["strategies"][i + 1]
    assert main["backcast"]["trades"] == 1 and "28 Oct" in main["backcast"]["note"]
    assert main["judged"]["bets"] == 1 and main["judged"]["verdict"] is None
    assert main["odds_bands"]["clv_field"] == "clv_pinnacle" and main["by_odds_band"]
    assert main["sizing"]["kelly"]["p_source"] == "model"
    assert shadow["variant_of"] == ls.KEY and shadow["adds"] == "threshold"
    assert shadow["compare"]["descriptive"]
    assert live["summary"]["trades"] == 0 and len(live["lineshop_trades"]) == len(trades)


DEFINED = "2026-11-06T00:00:00+00:00"
KO = "2026-11-10T15:00:00+00:00"


def bet(i, clv, kickoff=KO, status="open", opened="2026-11-08T00:00:00+00:00"):
    return {"id": f"E0|2627|H{i}|A{i}|lineshop", "opened_at": opened, "kickoff": kickoff,
            "status": status, "clv_pinnacle": clv}  # fmt: skip


def test_an_interim_close_never_counts():
    """Before kickoff the close is the newest quote so far (right after entry: the entry gap,
    at least 2%), so no verdict is read until the first 300 bets' closes are final."""
    before = pd.Timestamp("2026-11-10T12:00:00", tz="UTC")
    many = [bet(i, 0.025) for i in range(300)]
    r = ls.judged(many, DEFINED, before)
    assert r["verdict"] is None and "clv" not in r
    assert r["bets"] == 300 and r["final"] == 0
    # Kicked off, but under an hour ago: still not final.
    assert (
        ls.judged(many, DEFINED, pd.Timestamp("2026-11-10T15:30:00", tz="UTC"))["verdict"] is None
    )
    # One of the first 300 still to kick off holds the read back.
    late = [*many[:299], bet(299, 0.025, kickoff="2026-11-20T15:00:00+00:00")]
    after = pd.Timestamp("2026-11-11T00:00:00", tz="UTC")
    assert ls.judged(late, DEFINED, after)["verdict"] is None
    # Backcast and voided bets are never part of it.
    extra = [{**bet(900 + i, 0.5), "backcast": True} for i in range(5)]
    voids = [bet(950, None, status="void")]
    r = ls.judged([*many, *extra, *voids], DEFINED, after)
    assert r["verdict"] == "pass" and r["read"] == 300 and r["clv"] == pytest.approx(0.025)


def test_the_verdict_is_frozen_once_written(tmp_path):
    after = pd.Timestamp("2026-11-11T00:00:00", tz="UTC")
    vals = [bet(i, 0.02 + i % 3 * 0.001) for i in range(300)]
    first = ls.judged(vals, DEFINED, after, tmp_path)
    assert first["verdict"] == "pass" and first["frozen"]
    rec = json.loads((tmp_path / "paper_trades" / ls.VERDICT_FILE).read_text())
    assert len(rec["trades"]) == 300 and rec["read_at"] == after.isoformat(timespec="seconds")
    # Later closes or bets change nothing: the stored read is returned as it is.
    worse = [{**t, "clv_pinnacle": -0.5} for t in vals] + [bet(400, -0.9)]
    again = ls.judged(worse, DEFINED, after + pd.Timedelta(days=30), tmp_path)
    assert again["verdict"] == "pass" and again["clv"] == first["clv"] and again["frozen"]
    assert "trades" not in again


def test_defined_at_is_not_written_without_a_books_log(tmp_path):
    assert ls.defined_at(tmp_path, moment(0), write=False) == moment(0).isoformat(
        timespec="seconds"
    )
    assert not (tmp_path / "paper_trades").exists()
