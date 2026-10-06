import json

import pandas as pd
import pytest
from test_paper import FETCHED, KICKOFF, NOW, card, results, src

from soccer_stats import odds_log as ol
from soccer_stats import paper


def data(odds_draw=5.0, fetched=FETCHED, **kw):
    return {"fixtures": [card(odds_draw=odds_draw, **kw)], "odds_source": src(fetched)}


def test_rows_from_data():
    rows = ol.rows_from_data(data(odds_updated="2026-10-08T11:20:00Z"), NOW)
    assert [r["market"] for r in rows] == ["h2h", "totals"]
    h2h, tot = rows
    assert h2h["prices"] == {"home": 1.4, "draw": 5.0, "away": 7.5}
    assert sum(h2h["fair"].values()) == pytest.approx(1, abs=1e-5)
    assert h2h["fetched_at"] == "2026-10-08T11:20:00+00:00"
    assert h2h["time_source"] == "last_update"
    assert h2h["downloaded_at"] == "2026-10-08T11:30:00+00:00"
    assert h2h["p"] == {"home": 0.61, "draw": 0.24, "away": 0.15} and h2h["p_bet"] is None
    assert h2h["kickoff"] == "2026-10-10T14:00:00+00:00" and h2h["bookmaker"] == "DraftKings"
    assert tot["line"] == 2.5 and set(tot["prices"]) == {"over25", "under25"}
    # No last_update: the download time; p_bet carried when set.
    r = ol.rows_from_data(data(p_bet={"home": 0.6, "draw": 0.25, "away": 0.15}), NOW)[0]
    assert r["fetched_at"] == "2026-10-08T11:30:00+00:00" and r["time_source"] == "download"
    assert r["p_bet"] == {"home": 0.6, "draw": 0.25, "away": 0.15}


def test_rows_skip_non_draftkings_inplay_and_missing_prices():
    assert ol.rows_from_data({"fixtures": [card()], "odds_source": {"name": "x"}}, NOW) == []
    inplay = data(fetched=KICKOFF + pd.Timedelta(minutes=5))
    assert ol.rows_from_data(inplay, NOW) == []
    c = card()
    c["odds"]["over25"] = None
    rows = ol.rows_from_data({"fixtures": [c], "odds_source": src()}, NOW)
    assert [r["market"] for r in rows] == ["h2h"]


def test_append_is_deduplicated_and_monthly(tmp_path):
    rows = ol.rows_from_data(data(), NOW)
    assert ol.append(tmp_path, rows) == 2
    assert ol.append(tmp_path, rows) == 0  # cached build: same prices, same time
    later = NOW + pd.Timedelta(hours=1)
    assert ol.append(tmp_path, ol.rows_from_data(data(fetched=later), later)) == 2
    moved = ol.rows_from_data(data(odds_draw=4.8, fetched=later), later)
    assert ol.append(tmp_path, moved) == 1  # only h2h changed
    files = sorted(p.name for p in (tmp_path / "odds_log").iterdir())
    assert files == ["E0_2026-10.jsonl"]
    log = ol.load(tmp_path)
    assert len(log) == 5 and log["fetched_at"].is_monotonic_increasing
    assert str(log["kickoff"].dt.tz) == "UTC"


def test_last_before_kickoff(tmp_path):
    t1 = KICKOFF - pd.Timedelta(hours=5)
    t2 = KICKOFF - pd.Timedelta(minutes=40)
    ol.append(tmp_path, ol.rows_from_data(data(fetched=t1), t1))
    ol.append(tmp_path, ol.rows_from_data(data(odds_draw=4.5, fetched=t2), t2))
    log = ol.load(tmp_path)
    r = ol.last_before(log, "Arsenal", "Leeds", KICKOFF, "h2h")
    assert r["prices"]["draw"] == 4.5 and r["minutes_before"] == 40.0
    # Before the second quote existed, the first is the close.
    r = ol.last_before(log, "Arsenal", "Leeds", t2, "h2h")
    assert r["prices"]["draw"] == 5.0
    assert ol.last_before(log, "Arsenal", "Leeds", t1, "h2h") is None
    assert ol.last_before(log, "Leeds", "Arsenal", KICKOFF, "h2h") is None
    assert ol.last_before(ol.load(tmp_path / "none"), "Arsenal", "Leeds", KICKOFF, "h2h") is None


def _build(tmp_path, d, now, res=None):
    """One publish run's data-log step: log-odds, then paper (as publish.yml does)."""
    ol.append(tmp_path, ol.rows_from_data(d, now))
    ledger = paper.load_ledger(tmp_path)
    events, _ = paper.update_ledger(
        ledger, d["fixtures"], d["odds_source"], res, now, odds_log=ol.load(tmp_path)
    )
    paper.append_events(tmp_path, events)
    return paper.load_ledger(tmp_path)["E0|2627|Arsenal|Leeds"], events


def test_live_trade_close_comes_from_the_log(tmp_path):
    t, ev = _build(tmp_path, data(), NOW)
    assert ev[0]["type"] == "open" and len(ev) == 1  # the log's quote = the entry close
    entry = {k: t[k] for k in ("odds", "edge", "opened_at", "odds_fetched_at", "model_p")}
    assert t["close_minutes_before"] == pytest.approx((KICKOFF - NOW).total_seconds() / 60 + 30)
    assert t["clv_dk"] < 0 and t["beat_close_dk"] is False  # entry vs its own price: margin

    # The last build before kickoff runs 2 hours out; the price shortened.
    late = KICKOFF - pd.Timedelta(hours=2)
    t, ev = _build(tmp_path, data(odds_draw=4.2, fetched=late - pd.Timedelta(minutes=10)), late)
    assert [e["type"] for e in ev] == ["update"]
    assert t["close_odds"] == 4.2 and t["close_minutes_before"] == 130.0
    assert t["clv_dk"] > 0 and t["beat_close_dk"] is True
    assert {k: t[k] for k in entry} == entry  # entry never edited

    # After kickoff: in-play prices are not logged and don't move the close; settle.
    after = KICKOFF + pd.Timedelta(hours=3)
    t, ev = _build(tmp_path, data(odds_draw=9.0, fetched=after), after, results(1, 1))
    assert t["close_odds"] == 4.2 and t["status"] == "won"
    assert [set(e) & {"close_odds"} for e in ev] == [set()]  # only the settlement

    s = paper.portfolio_section(list(paper.load_ledger(tmp_path).values()))["summary"]
    assert s["clv_dk"] > 0 and s["beat_close_dk"] == 1.0
    assert s["close_over_60min"] == 1  # the close was 130 minutes out


def test_trade_without_log_rows_backfills_once_the_log_covers_it(tmp_path):
    # A trade opened before the log existed (old ledger), then a later logged quote.
    led = paper.load_ledger(tmp_path)
    ev, _ = paper.update_ledger(led, data()["fixtures"], src(), None, NOW)
    paper.append_events(tmp_path, ev)
    late = KICKOFF - pd.Timedelta(minutes=20)
    t, ev = _build(tmp_path, data(odds_draw=5.5, fetched=late), late)
    assert t["close_odds"] == 5.5 and t["close_minutes_before"] == 20.0
    assert t["beat_close_dk"] is False
    t, ev = _build(tmp_path, data(odds_draw=5.5, fetched=late), late)
    assert ev == []  # nothing new


def test_cli_log_odds(tmp_path, capsys):
    from soccer_stats import cli

    site = tmp_path / "site"
    site.mkdir()
    fetched = pd.Timestamp.now(tz="UTC").floor("s")
    d = {"fixtures": [card(kickoff=fetched + pd.Timedelta(days=1))], "odds_source": src(fetched)}
    (site / "data.json").write_text(json.dumps(d))
    cli.main(["log-odds", "--site", str(site), "--log-dir", str(tmp_path / "log")])
    cli.main(["log-odds", "--site", str(site), "--log-dir", str(tmp_path / "log")])
    out = capsys.readouterr().out
    assert "2 new rows" in out and "0 new rows" in out
