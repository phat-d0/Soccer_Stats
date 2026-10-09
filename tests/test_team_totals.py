import json

import numpy as np
import pandas as pd
import pytest

from soccer_stats import odds_log as ol
from soccer_stats import team_totals as tt

K = pd.Timestamp("2026-10-17 14:00", tz="UTC")  # a Saturday 14:00 kickoff
CDF = [0.25, 0.6, 0.85, 0.95, 0.99, 1.0, 1.0]


def card(home="Arsenal", away="Leeds", league="E0", kickoff=K):
    return {
        "league": league,
        "home": home,
        "away": away,
        "kickoff": kickoff.isoformat(),
        "goals_cdf": {"home": CDF, "away": [0.4, 0.75, 0.93, 0.98, 1.0, 1.0, 1.0]},
    }


def dk_cache(raw, league="E0", events=(("ev1", "Arsenal", "Leeds United", K),), credits=20000):
    body = [
        {"id": i, "home_team": h, "away_team": a, "commence_time": k.isoformat()}
        for i, h, a, k in events
    ]
    (raw / f"odds_api_{league}_draftkings.json").write_text(json.dumps(body))
    meta = {"fetched_at": (K - pd.Timedelta(days=2)).isoformat(), "credits_left": credits}
    (raw / f"odds_api_{league}_draftkings.meta.json").write_text(json.dumps(meta))


def body(home="Arsenal", away="Leeds United", books=("fanduel",)):
    def outcomes(team, line, o, u):
        return [
            {"name": "Over", "description": team, "price": o, "point": line},
            {"name": "Under", "description": team, "price": u, "point": line},
        ]

    return {
        "id": "ev1",
        "bookmakers": [
            {
                "key": b,
                "last_update": (K - pd.Timedelta(hours=1)).isoformat(),
                "markets": [
                    {
                        "key": "team_totals",
                        "outcomes": outcomes(home, 1.5, 1.9, 1.9) + outcomes(away, 0.5, 1.6, 2.2),
                    }
                ],
            }
            for b in books
        ],
    }


class Resp:
    def __init__(self, b, left=19999, cost=1, status=200):
        self.b, self.status_code, self.ok = b, status, status == 200
        self.headers = {"x-requests-remaining": str(left), "x-requests-last": str(cost)}

    def json(self):
        return self.b


def fake_get(calls, b=None, **kw):
    def get(url, params=None, timeout=None):
        calls.append((url, dict(params)))
        return Resp(b if b is not None else body(), **kw)

    return get


# ---------- timing ----------


def test_due_snapshot_windows():
    at = lambda h: K - pd.Timedelta(hours=h)  # noqa: E731
    assert tt.due_snapshot(K, at(31)) is None
    assert tt.due_snapshot(K, at(30)) == "look"
    assert tt.due_snapshot(K, at(24)) == "look"
    assert tt.due_snapshot(K, at(18)) == "look"
    assert tt.due_snapshot(K, at(17.9)) is None
    assert tt.due_snapshot(K, at(3)) is None
    assert tt.due_snapshot(K, K - pd.Timedelta(minutes=29)) == "close"
    assert tt.due_snapshot(K, K) is None and tt.due_snapshot(K, K + pd.Timedelta(minutes=5)) is None


def test_close_is_taken_early_only_when_no_later_scheduled_run():
    # 14:00 kickoff, run at 13:07: 13:22, 13:37 and 13:52 are still to come, so wait.
    assert tt.due_snapshot(K, K - pd.Timedelta(minutes=53)) is None
    # 08:00 kickoff (hourly runs only, at :07): 07:07 is the last run before kickoff.
    k = pd.Timestamp("2026-10-17 08:00", tz="UTC")
    assert tt.due_snapshot(k, k - pd.Timedelta(minutes=53)) == "close"
    # Skipped runs: whatever run comes first inside a window takes it.
    assert tt.due_snapshot(K, K - pd.Timedelta(hours=19, minutes=5)) == "look"
    assert tt.due_snapshot(K, K - pd.Timedelta(minutes=4)) == "close"


# ---------- one publish run ----------


def test_run_fetches_each_snapshot_once(tmp_path):
    raw, out = tmp_path / "raw", tmp_path / "out"
    raw.mkdir()
    dk_cache(raw)
    calls = []
    cards = [card(), card("Real Madrid", "Getafe", "E1")]  # the Championship never fetches
    s = tt.run(cards, out, [], raw, "k", K - pd.Timedelta(hours=24), fake_get(calls))
    assert s["calls"] == 1 and s["credits"] == 1 and s["rows"] == 2 and s["stop"] is None
    url, params = calls[0]
    assert "/events/ev1/odds" in url and "soccer_epl" in url
    assert params["markets"] == "team_totals" and params["bookmakers"] == "fanduel,bovada"
    # An hour later the look is already taken: no call, even with no data-log state.
    s = tt.run(cards, out, [], raw, "k", K - pd.Timedelta(hours=23), fake_get(calls))
    assert s["calls"] == 0 and len(calls) == 1
    # The close is a separate snapshot, taken once.
    s = tt.run(cards, out, [], raw, "k", K - pd.Timedelta(minutes=20), fake_get(calls))
    assert s["calls"] == 1 and len(calls) == 2
    s = tt.run(cards, out, [], raw, "k", K - pd.Timedelta(minutes=5), fake_get(calls))
    assert s["calls"] == 0 and len(calls) == 2
    rows = [json.loads(x) for x in (out / "rows.jsonl").read_text().splitlines()]
    assert {r["snapshot"] for r in rows} == {"look", "close"}
    r = next(r for r in rows if r["team"] == "Arsenal" and r["snapshot"] == "look")
    assert r["side"] == "home" and r["line"] == 1.5 and r["book"] == "fanduel"
    assert r["fair_over"] + r["fair_under"] == pytest.approx(1, abs=1e-6)
    assert r["p_model_over"] == pytest.approx(1 - 0.6)  # P(home goals > 1.5) from the cdf
    assert r["minutes_before"] == 24 * 60 and r["time_source"] == "last_update"
    a = next(r for r in rows if r["team"] == "Leeds" and r["snapshot"] == "look")
    assert a["side"] == "away" and a["p_model_over"] == pytest.approx(0.6)


def test_state_from_data_log_and_empty_answers_count(tmp_path):
    raw, out, state = tmp_path / "raw", tmp_path / "out", tmp_path / "state"
    raw.mkdir()
    state.mkdir()
    dk_cache(raw)
    done = {
        "event_id": "ev1",
        "snapshot": "look",
        "fetched_at": (K - pd.Timedelta(hours=25)).isoformat(),
        "cost": 1,
    }
    (state / "team_totals_calls_2026-10.jsonl").write_text(json.dumps(done) + "\n")
    calls = []
    s = tt.run([card()], out, [state], raw, "k", K - pd.Timedelta(hours=20), fake_get(calls))
    assert s["calls"] == 0 and calls == []
    # A call that returned no prices is still recorded, so it isn't repeated.
    s = tt.run(
        [card()],
        out,
        [],
        raw,
        "k",
        K - pd.Timedelta(minutes=10),
        fake_get(calls, {"bookmakers": []}),
    )
    assert s["calls"] == 1 and s["rows"] == 0
    s = tt.run([card()], out, [], raw, "k", K - pd.Timedelta(minutes=3), fake_get(calls))
    assert s["calls"] == 0 and len(calls) == 1


def test_monthly_cap_and_reserve(tmp_path):
    raw, out, state = tmp_path / "raw", tmp_path / "out", tmp_path / "state"
    raw.mkdir()
    state.mkdir()
    dk_cache(raw)
    month = [
        {
            "event_id": f"x{i}",
            "snapshot": "look",
            "fetched_at": "2026-10-02T12:00:00+00:00",
            "cost": 1,
        }
        for i in range(tt.MONTHLY_CAP)
    ]
    path = state / "team_totals_calls_2026-10.jsonl"
    path.write_text("\n".join(json.dumps(c) for c in month) + "\n")
    calls = []
    s = tt.run([card()], out, [state], raw, "k", K - pd.Timedelta(hours=24), fake_get(calls))
    assert calls == [] and "monthly cap" in s["stop"]
    # Last month's calls don't count against this month.
    for c in month:
        c["fetched_at"] = "2026-09-20T12:00:00+00:00"
    path.write_text("\n".join(json.dumps(c) for c in month) + "\n")
    s = tt.run([card()], out, [state], raw, "k", K - pd.Timedelta(hours=24), fake_get(calls))
    assert s["calls"] == 1
    # Reserve: the freshest balance (here a DraftKings meta newer than any call) is too low.
    raw2 = tmp_path / "raw2"
    raw2.mkdir()
    dk_cache(raw2, credits=3000)
    meta = {"fetched_at": (K - pd.Timedelta(hours=25)).isoformat(), "credits_left": 3000}
    (raw2 / "odds_api_E0_draftkings.meta.json").write_text(json.dumps(meta))
    s = tt.run([card()], out, [], raw2, "k", K - pd.Timedelta(hours=24), fake_get(calls))
    assert s["calls"] == 0 and "reserve" in s["stop"]
    # The reserve is judged after each call too: the header's balance stops the next one.
    raw3 = tmp_path / "raw3"
    raw3.mkdir()
    two = (("ev1", "Arsenal", "Leeds United", K), ("ev2", "Chelsea", "Everton", K))
    dk_cache(raw3, events=two)
    get = fake_get(calls, left=3000)
    s = tt.run(
        [card(), card("Chelsea", "Everton")], out, [], raw3, "k", K - pd.Timedelta(hours=24), get
    )
    assert s["calls"] == 1 and "reserve" in s["stop"]


def test_no_key_and_no_event_mean_no_call(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    dk_cache(raw)
    calls = []
    s = tt.run([card()], tmp_path / "o", [], raw, "", K - pd.Timedelta(hours=24), fake_get(calls))
    assert calls == [] and s["stop"] == "no ODDS_API_KEY"
    s = tt.run(
        [card("Burnley", "Fulham")],
        tmp_path / "o",
        [],
        raw,
        "k",
        K - pd.Timedelta(hours=24),
        fake_get(calls),
    )
    assert calls == [] and s["calls"] == 0


def test_summary_line_never_prints_the_key(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    dk_cache(raw)
    s = tt.run(
        [card()], tmp_path / "o", [], raw, "SECRET", K - pd.Timedelta(hours=24), fake_get([])
    )
    assert "SECRET" not in tt.summary_line(s)
    assert "SECRET" not in (tmp_path / "o" / "calls.jsonl").read_text()
    assert "SECRET" not in (raw / tt.LOCAL_CALLS).read_text()


# ---------- data-log ----------


def test_log_is_deduplicated_and_kept_apart_from_the_draftkings_log(tmp_path):
    raw, out, root = tmp_path / "raw", tmp_path / "out", tmp_path / "log"
    raw.mkdir()
    dk_cache(raw)
    tt.run([card()], out, [], raw, "k", K - pd.Timedelta(hours=24), fake_get([]))
    assert tt.log(out, root) == (2, 1)
    assert tt.log(out, root) == (0, 0)
    names = sorted(p.name for p in (root / "odds_log").iterdir())
    assert names == ["E0_team_totals_2026-10.jsonl", "team_totals_calls_2026-10.jsonl"]
    assert ol.load(root).empty  # the DraftKings log reader skips team-total files
    assert len(tt.load_rows(root)) == 2


# ---------- analysis ----------


def _logged(n_matches):
    rows, res = [], []
    rng = np.random.default_rng(1)
    for i in range(n_matches):
        k = (K + pd.Timedelta(days=i)).isoformat()
        for snap, fo in (("look", 0.5), ("close", 0.52)):
            rows.append(
                {
                    "league": "E0",
                    "home": f"H{i}",
                    "away": f"A{i}",
                    "kickoff": k,
                    "event_id": f"e{i}",
                    "snapshot": snap,
                    "book": "fanduel",
                    "team": f"H{i}",
                    "side": "home",
                    "line": 1.5,
                    "over": 1.9,
                    "under": 1.9,
                    "fair_over": fo,
                    "fair_under": 1 - fo,
                    "minutes_before": 1440 if snap == "look" else 20,
                    "p_model_over": 0.55,
                }
            )
        res.append(
            {
                "league": "E0",
                "home": f"H{i}",
                "away": f"A{i}",
                "date": k[:10],
                "home_goals": int(rng.integers(0, 4)),
                "away_goals": 1,
            }
        )
    return pd.DataFrame(rows), pd.DataFrame(res)


def test_report_waits_for_enough_settled_matches():
    rows, res = _logged(10)
    out = tt.report(rows, res)
    assert out["matches"] == 10 and "not enough data yet" in out["note"] and "model" not in out
    assert "not enough" in tt.report(pd.DataFrame(), res)["note"]


def test_report_scores_against_the_close():
    rows, res = _logged(60)
    out = tt.report(rows, res)
    assert out["matches"] == 60 and out["fanduel_rows"] == 60
    assert out["model"]["rows"] == 60 and "gain_vs_market" in out["model"]
    assert out["median_close_minutes"] == 20
    assert out["calibration_model"] and out["calibration_close"]


def test_a_crash_part_way_keeps_the_calls_already_made(tmp_path):
    """Lead review: an unexpected error after a paid call must not lose its record (the
    next run would pay again), must not stop the build, and must not print the key."""
    raw, out = tmp_path / "raw", tmp_path / "out"
    raw.mkdir()
    evs = (("ev1", "Arsenal", "Leeds United", K), ("ev2", "Chelsea", "Fulham", K))
    dk_cache(raw, events=evs)
    calls = []

    def get(url, params=None, timeout=None):
        calls.append(url)
        if len(calls) == 2:
            raise RuntimeError(f"boom {params['apiKey']}")
        return Resp(body())

    cards = [card(), card("Chelsea", "Fulham")]
    s = tt.run(cards, out, [], raw, "secret-key", K - pd.Timedelta(hours=24), get)
    assert s["calls"] == 1 and s["stop"] == "error (RuntimeError)"
    assert "secret-key" not in tt.summary_line(s)
    recorded = [json.loads(x) for x in (out / "calls.jsonl").read_text().splitlines()]
    assert [c["event_id"] for c in recorded] == ["ev1"]

    # A garbled header or body still records the call, counted at the worst case.
    class Odd(Resp):
        def json(self):
            return {"bookmakers": [{"markets": [{"key": "team_totals", "outcomes": [1]}]}]}

    odd = Odd(None)
    odd.headers = {"x-requests-last": "n/a", "x-requests-remaining": ""}
    s = tt.run(cards, out, [], raw, "k", K - pd.Timedelta(minutes=20), lambda *a, **k: odd)
    assert s["calls"] == 2 and s["credits"] == 2 and s["rows"] == 0 and s["stop"] is None
