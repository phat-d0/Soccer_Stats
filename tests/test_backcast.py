"""Backcasts from 14 Oct on the odds log (owner, 28 Oct): the draw no bet tracks replayed
before their defined_at, marked, closed and settled as live, never judged."""

import json

import pandas as pd
import pytest

from soccer_stats import backcast as bc
from soccer_stats import odds_log as ol
from soccer_stats import paper
from soccer_stats import paper_dnb as pdnb
from soccer_stats import trades as tr


def T(s):
    return pd.Timestamp(s, tz="UTC")


KO = T("2026-10-18 14:00")
EDGE12_ID = "E0|2627|Arsenal|Leeds|dnb"
LEAN_ID = "E0|2627|Arsenal|Leeds|lean|dnb"
P = {"home": 0.61, "draw": 0.24, "away": 0.15}
SIGMA_DNB = 0.03


def row(logged, prices, p=P, p_bet=None, kickoff=KO, downloaded=None, home="Arsenal"):
    logged = T(logged)
    return {
        "league": "E0",
        "home": home,
        "away": "Leeds",
        "kickoff": kickoff.isoformat(),
        "market": "h2h",
        "line": None,
        "prices": prices,
        "fair": {},
        "bookmaker": "DraftKings",
        "fetched_at": (logged - pd.Timedelta(minutes=5)).isoformat(),
        "time_source": "last_update",
        "downloaded_at": (T(downloaded) if downloaded else logged).isoformat(),
        "logged_at": logged.isoformat(),
        "p": p,
        "p_bet": p_bet,
    }


def write_log(root, rows):
    d = ol.log_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "E0_2026-10.jsonl", "a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return ol.load(root, "E0")


def rules(sigma=SIGMA_DNB, made="2026-10-26T06:00:00+00:00"):
    lean = {"min_z": tr.LEAN_Z, "sigma_dnb": sigma, "fit_made_at": made}
    return {
        "lean_dnb": tr.dnb_rule(lean, "lean_dnb"),
        "edge12_dnb": tr.dnb_rule(tr.paper_threshold(None), "edge12_dnb"),
    }


NO_EDGE = {"home": 1.4, "draw": 5.0, "away": 5.0}  # away DNB 4.0 x 19.7% < 12%
EDGE = {"home": 1.4, "draw": 5.0, "away": 7.5}  # away DNB 6.0 x 19.7% = +18%


def test_observations_take_the_window_in_logged_order(tmp_path):
    log = write_log(
        tmp_path,
        [
            row("2026-10-15 09:00", EDGE),
            row("2026-10-13 23:00", EDGE),  # before 14 Oct
            row("2026-10-26 09:00", EDGE),  # after defined_at
            row("2026-10-14 08:00", NO_EDGE),
        ],
    )
    seen = list(bc.observations(log, tr.DNB_FROM))
    assert [n for n, _c, _d in seen] == [T("2026-10-14 08:00"), T("2026-10-15 09:00")]
    now, c, down = seen[1]
    assert c["odds"] == EDGE and c["p"] == P and c["kickoff"] == KO.isoformat()
    assert c["odds_updated"] == "2026-10-15T08:55:00+00:00" and down == now


def test_the_first_logged_moment_the_rule_fires_opens_a_marked_trade(tmp_path):
    log = write_log(
        tmp_path,
        [
            row("2026-10-14 08:00", NO_EDGE),
            row("2026-10-15 09:00", EDGE, downloaded="2026-10-15 04:00"),  # stale download
            row("2026-10-16 09:00", EDGE),
            row("2026-10-17 09:00", {"home": 1.4, "draw": 5.0, "away": 9.0}),
            row("2026-10-19 09:00", EDGE, kickoff=T("2026-10-19 08:00")),  # after kickoff
        ],
    )
    ledger = {}
    ev = pdnb.backcast(ledger, log, "E0", rules(), {"name": "test"})
    assert [e["id"] for e in ev] == [EDGE12_ID]
    t = ledger[EDGE12_ID]
    assert t["opened_at"] == "2026-10-16T09:00:00+00:00" and t["odds"] == 6.0
    assert t["backcast"] is True and t["backcast_from"] == "odds_log"
    assert t["source"] == "live" and t["strategy"] == "edge12_dnb" and "sigma_from" not in t
    assert t["model_p"] == pytest.approx(0.15 / 0.76, abs=1e-4)  # the logged raw p
    assert t["close_fetched_at"] == "2026-10-16T08:55:00+00:00"
    # Idempotent: the same log opens nothing again.
    assert pdnb.backcast(ledger, log, "E0", rules()) == []


def test_lean_backcast_uses_todays_sigma_dnb_flagged_and_the_range(tmp_path):
    lean_odds = {"home": 2.6, "draw": 3.4, "away": 2.9}
    p_bet = {"home": 0.45, "draw": 0.27, "away": 0.28}
    log = write_log(tmp_path, [row("2026-10-15 09:00", lean_odds, p_bet=p_bet)])
    ledger = {}
    pdnb.backcast(ledger, log, "E0", rules())
    t = ledger[LEAN_ID]
    assert t["market"] == "home" and t["sigma"] == SIGMA_DNB
    assert t["sigma_from"] == "26 Oct refit" and t["backcast"] is True
    assert t["p_bet"] == pytest.approx(0.45 / 0.73, abs=1e-4)
    # No sigma_dnb: no Lean backcast; a DNB price outside 1.50-3.00: none either.
    none = {}
    pdnb.backcast(none, log, "E0", rules(sigma=None))
    assert LEAN_ID not in none
    long_odds = {"home": 4.6, "draw": 3.4, "away": 1.8}
    log2 = write_log(tmp_path / "b", [row("2026-10-15 09:00", long_odds, p_bet=p_bet)])
    out = {}
    pdnb.backcast(out, log2, "E0", rules())
    assert LEAN_ID not in out  # home DNB 4.6 x (1 - 1/3.4) = 3.25: outside the range


def test_a_match_already_traded_live_is_left_alone(tmp_path):
    log = write_log(tmp_path, [row("2026-10-16 09:00", EDGE)])
    live = {EDGE12_ID: {"id": EDGE12_ID, "opened_at": "2026-10-26T09:00:00+00:00"}}
    assert pdnb.backcast(live, log, "E0", rules()) == []
    assert "backcast" not in live[EDGE12_ID]


def test_paper_run_backcasts_closes_settles_and_keeps_them_out_of_the_compare(tmp_path):
    log_rows = [row("2026-10-16 09:00", EDGE), row("2026-10-18 13:30", NO_EDGE)]
    write_log(tmp_path, log_rows)
    now = T("2026-10-28 12:00")
    res = pd.DataFrame(
        {
            "season": ["2627"],
            "date": [pd.Timestamp("2026-10-18")],
            "home": ["Arsenal"],
            "away": ["Leeds"],
            "home_goals": [0],
            "away_goals": [1],
            "close_home": [1.45],
            "close_draw": [4.6],
            "close_away": [7.0],
        }
    )
    data = {
        "fixtures": [],
        "odds_source": {"name": "DraftKings", "fetched_at": now.isoformat()},
        "portfolio": {},
        "match_blends": {"leagues": {"E0": {"coef": [0, 0, 1, 0], "sigma": 0.5, "matches": 9}}},
    }
    paper.run(data, tmp_path, res, now=now)
    t = pdnb.load_ledger(tmp_path, "E0")[EDGE12_ID]
    assert t["backcast"] is True and t["status"] == "won" and t["profit"] == pytest.approx(50.0)
    assert t["close_fetched_at"] == "2026-10-18T13:25:00+00:00"  # the log's last quote
    assert t["clv_pinnacle"] is not None and t["result_source"]
    ml = next(p for p in data["portfolio"]["portfolios"] if p["id"] == "moneyline")
    sec = next(s for s in ml["live"]["strategies"] if s["key"] == "edge12_dnb")
    b = sec["backcast"]
    assert (b["started"], b["until"], b["defined_at"]) == (bc.START, tr.DNB_FROM, tr.DNB_FROM)
    assert b["trades"] == 1 and b["settled"] == 1 and b["descriptive"] is True
    assert b["applied"] and b["from"] == b["to"] == t["opened_at"] and "14 Oct" in b["note"]
    assert sec["summary"]["trades"] == 1  # the line includes it, from 14 Oct
    assert sec["compare"]["track"]["trades"] == 0  # the judged window doesn't
    # A second run opens nothing and settles nothing again.
    assert paper.run(data, tmp_path, res, now=now + pd.Timedelta(hours=1)) == 0


def test_block_shape_is_shared_with_leans_backcast():
    trades = [
        {"opened_at": "2026-10-15T09:00:00+00:00", "status": "void", "push": True},
        {"opened_at": "2026-10-16T09:00:00+00:00", "status": "open"},
        {"opened_at": "2026-10-27T09:00:00+00:00", "status": "won"},
    ]
    b = bc.block(trades, tr.DNB_FROM)
    assert (b["trades"], b["settled"]) == (2, 1)  # a refunded draw is settled
    lean = paper.lean_backcast([], "2026-10-14T07:00:00+00:00")
    assert set(b) <= set(lean) and lean["lean_started"] == lean["started"]
    assert lean["defined_at"] == lean["until"] == tr.LEAN_RANGE_FROM
