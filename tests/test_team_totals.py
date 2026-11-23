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
    def __init__(self, b, left=19999, cost=2, status=200):
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
    assert tt.due_snapshot(K, K - pd.Timedelta(minutes=29), []) == "close"
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


def test_lineup_and_goalscorer_runs_take_no_close():
    """Lead, 16 Nov: from 7 Nov the -25 goalscorer run took most closes (median 24 minutes
    before kickoff); lineup and goalscorer runs now leave the close to the -15 run."""
    assert tt.NO_CLOSE_KINDS == ("lineup", "goalscorer")
    for minutes in (29, 24, 20, 19):
        at = K - pd.Timedelta(minutes=minutes)
        assert tt.due_snapshot(K, at, [], kind="settle") == "close"
        assert tt.due_snapshot(K, at, kind="close", until=K) == "close"
        assert tt.due_snapshot(K, at, kind="lineup") is None
        assert tt.due_snapshot(K, at, kind="goalscorer") is None
    # A close is never lost: with 18 minutes or less left no -15 close run can follow, so a
    # lineup or goalscorer run takes it (a late-started planner, say).
    for minutes in (18, 12, 5):
        at = K - pd.Timedelta(minutes=minutes)
        for kind in ("close", "lineup", "goalscorer", "scheduled"):
            assert tt.due_snapshot(K, at, kind=kind) == "close"
    # The last-chance close (no later scheduled run) waits for the planner's -15 close run
    # in a lineup run; a run without a kind (cron, a manual dispatch) still takes it.
    k = pd.Timestamp("2026-10-17 08:00", tz="UTC")
    assert tt.due_snapshot(k, k - pd.Timedelta(minutes=53), kind="lineup") is None
    assert tt.due_snapshot(k, k - pd.Timedelta(minutes=53)) == "close"
    # Looks are unchanged in every kind.
    for kind in ("close", "lineup", "goalscorer", "settle"):
        assert tt.due_snapshot(K, K - pd.Timedelta(hours=24), kind=kind) == "look"


def test_run_kind_from_the_environment():
    assert tt.run_kind({}) == "scheduled"
    assert tt.run_kind({"PUBLISH_RUN_KIND": ""}) == "scheduled"
    assert tt.run_kind({"PUBLISH_RUN_KIND": "close"}) == "close"
    assert tt.close_until({}) is None and tt.close_until({"PUBLISH_CLOSE_UNTIL": "x"}) is None
    assert tt.close_until({"PUBLISH_CLOSE_UNTIL": "2026-11-20T19:45+00:00"}) == pd.Timestamp(
        "2026-11-20 19:45", tz="UTC"
    )
    assert tt.run_kind({"PUBLISH_RUN_KIND": " Lineup "}) == "lineup"
    assert tt.run_kind({"PUBLISH_RUN_KIND": "goalscorer"}) == "goalscorer"


def test_a_lineup_run_writes_no_close_row_and_the_close_run_does(tmp_path, monkeypatch):
    raw, out = tmp_path / "raw", tmp_path / "out"
    raw.mkdir()
    dk_cache(raw)
    calls = []
    for kind in ("lineup", "goalscorer"):
        monkeypatch.setenv("PUBLISH_RUN_KIND", kind)
        s = tt.run([card()], out, [], raw, "k", K - pd.Timedelta(minutes=27), fake_get(calls))
        assert s["calls"] == 0 and s["run_kind"] == kind and not calls
    monkeypatch.setenv("PUBLISH_RUN_KIND", "close")
    monkeypatch.setenv("PUBLISH_CLOSE_UNTIL", K.isoformat())
    s = tt.run([card()], out, [], raw, "k", K - pd.Timedelta(minutes=13), fake_get(calls))
    assert s["calls"] == 1 and s["run_kind"] == "close"
    rows = [json.loads(x) for x in (out / "rows.jsonl").read_text().splitlines()]
    assert {r["snapshot"] for r in rows} == {"close"}
    assert {r["minutes_before"] for r in rows} == {13.0}


def test_a_late_goalscorer_run_still_takes_the_close(tmp_path, monkeypatch):
    """Close never lost (Corners, 16 Nov): a goalscorer run that starts with 16 minutes
    left (no -15 run can follow it) takes the close."""
    raw, out = tmp_path / "raw", tmp_path / "out"
    raw.mkdir()
    dk_cache(raw)
    calls = []
    monkeypatch.setenv("PUBLISH_RUN_KIND", "goalscorer")
    s = tt.run([card()], out, [], raw, "k", K - pd.Timedelta(minutes=16), fake_get(calls))
    assert s["calls"] == 1
    rows = [json.loads(x) for x in (out / "rows.jsonl").read_text().splitlines()]
    assert {r["snapshot"] for r in rows} == {"close"}


FRI = pd.Timestamp("2026-11-20 19:30", tz="UTC")


def test_a_close_run_closes_only_its_own_cluster():
    """Lead, 21 Nov: on 20 Nov (D1 19:30, F1 19:45, SP1 and E2 20:00; close runs at 19:15,
    19:30, 19:45, starting about a minute later) the 19:15 and 19:30 runs took the next
    cluster's closes 29 minutes out. Each close run now takes only its own."""
    m = lambda x: pd.Timedelta(minutes=x)  # noqa: E731
    d1, f1, sp1 = FRI, FRI + m(15), FRI + m(30)
    clusters = [(FRI - m(15), d1), (f1 - m(15), f1), (sp1 - m(15), sp1)]
    for start in (1, 3):  # each run starts 1-3 minutes after its dispatch
        taken = {}
        for at, last in clusters:
            now = at + m(start)
            for k in (d1, f1, sp1):
                if k not in taken and tt.due_snapshot(k, now, [], "close", last) == "close":
                    taken[k] = k - now
        assert taken == {k: m(15 - start) for k in (d1, f1, sp1)}
    # Without the cluster (a watchdog re-send): 28 minutes, not the next cluster's 29.
    assert tt.due_snapshot(f1, d1 - m(14), [], "close") is None
    assert tt.due_snapshot(d1, d1 - m(14), [], "close") == "close"
    # A close run never takes a last-chance close 30-75 minutes out.
    k = pd.Timestamp("2026-10-17 08:00", tz="UTC")
    assert tt.due_snapshot(k, k - m(53), [], "close", k) is None


def test_a_cluster_spanning_12_minutes_is_closed_by_its_own_run():
    m = lambda x: pd.Timedelta(minutes=x)  # noqa: E731
    first, last = FRI, FRI + m(12)
    for start in (1, 2, 3):
        now = first - m(15) + m(start)
        for k in (first, first + m(5), last):
            assert tt.due_snapshot(k, now, [], "close", last) == "close"
            assert tt.due_snapshot(k, now, [], "close") == "close"  # 24-27 min <= 28


def test_a_lost_close_run_is_still_covered_by_a_scheduled_run():
    """Runs with no kind (cron, heartbeat, manual) take a close as the last chance (no later
    scheduled run before kickoff), 30-75 minutes out or inside 30; settle runs keep the
    30-minute window and the last chance."""
    m = lambda x: pd.Timedelta(minutes=x)  # noqa: E731
    k = FRI + m(30)
    assert tt.due_snapshot(k, k - m(29), []) == "close"  # kind "scheduled", no later run
    assert tt.due_snapshot(k, k - m(29), kind="settle") == "close"
    assert tt.due_snapshot(k, k - m(29), [], "close", FRI + m(15)) is None
    early = pd.Timestamp("2026-10-17 08:00", tz="UTC")
    assert tt.due_snapshot(early, early - m(53), []) == "close"
    # 08:00 kickoff, hourly runs only (at :07): a cron at 07:35 has no later run, so it
    # takes the close 25 minutes out.
    assert tt.due_snapshot(early, early - m(25)) == "close"


SUN = pd.Timestamp("2026-11-22 11:30", tz="UTC")


def test_a_cron_run_leaves_the_close_to_the_close_run():
    """Lead, 23 Nov: on 22 Nov the 11:05 cron took I1's 11:30 closes 24 minutes out, before
    the 11:15 close run. A scheduled run now takes a close only with NO_CLOSE_UNTIL (18
    minutes) or less left, unless no later scheduled run is left (the last chance)."""
    m = lambda x: pd.Timedelta(minutes=x)  # noqa: E731
    cron = SUN - m(25)  # 11:05 (scheduled runs at 11:07, 11:22: one later before 11:25)
    assert tt.due_snapshot(SUN, cron) is None
    assert tt.due_snapshot(SUN, cron, kind="scheduled") is None
    assert tt.due_snapshot(SUN, SUN - m(14), kind="close", until=SUN) == "close"
    for minutes in (29, 24, 19):
        assert tt.due_snapshot(SUN, SUN - m(minutes)) is None
    # A cron 15 minutes out with no close run planned (or one lost) still takes it.
    for minutes in (18, 15, 5):
        assert tt.due_snapshot(SUN, SUN - m(minutes)) == "close"
    # The last chance: no later scheduled run before kickoff.
    assert tt.due_snapshot(SUN, cron, []) == "close"
    assert tt.due_snapshot(SUN, SUN - m(53), []) == "close"
    # Settle runs are unchanged.
    assert tt.due_snapshot(SUN, cron, kind="settle") == "close"


# ---------- one publish run ----------


def test_run_fetches_each_snapshot_once(tmp_path):
    raw, out = tmp_path / "raw", tmp_path / "out"
    raw.mkdir()
    dk_cache(raw)
    calls = []
    cards = [card(), card("Real Madrid", "Getafe", "E1")]  # the Championship never fetches
    s = tt.run(cards, out, [], raw, "k", K - pd.Timedelta(hours=24), fake_get(calls))
    assert s["calls"] == 1 and s["credits"] == 2 and s["rows"] == 2 and s["stop"] is None
    url, params = calls[0]
    assert "/events/ev1/odds" in url and "soccer_epl" in url
    assert params["markets"] == "team_totals,totals"
    assert params["bookmakers"] == "fanduel,bovada,pinnacle"
    # An hour later the look is already taken: no call, even with no data-log state.
    s = tt.run(cards, out, [], raw, "k", K - pd.Timedelta(hours=23), fake_get(calls))
    assert s["calls"] == 0 and len(calls) == 1
    # The close is a separate snapshot, taken once.
    s = tt.run(cards, out, [], raw, "k", K - pd.Timedelta(minutes=15), fake_get(calls))
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
    s = tt.run(cards, out, [], raw, "k", K - pd.Timedelta(minutes=15), lambda *a, **k: odd)
    assert s["calls"] == 2 and s["credits"] == 2 * tt.COST_PER_CALL and s["rows"] == 0
    assert s["stop"] is None


def test_price_files_in_the_state_folder_never_count_as_calls(tmp_path):
    """publish.yml copies data-log's `<code>_team_totals_*.jsonl` price rows into the same
    state folder as the call record (for the match sheet). They must not change the call
    dedup or the monthly spend: only `team_totals_calls_*.jsonl` (and the local record) count."""
    call = {
        "event_id": "e1",
        "snapshot": "look",
        "cost": 1,
        "fetched_at": "2026-10-09T15:00:00+00:00",
    }
    (tmp_path / "team_totals_calls_2026-10.jsonl").write_text(json.dumps(call) + "\n")
    price = {
        "event_id": "e2",
        "snapshot": "close",
        "cost": 7,
        "fetched_at": "2026-10-09T15:00:00+00:00",
        "book": "fanduel",
        "side": "home",
        "line": 1.5,
    }
    for name in ("E0_team_totals_2026-10.jsonl", "SP1_team_totals_2026-09.jsonl"):
        (tmp_path / name).write_text(json.dumps(price) + "\n")
    calls = tt.load_calls(tmp_path)
    assert [(c["event_id"], c["snapshot"]) for c in calls] == [("e1", "look")]
    assert tt.month_spend(calls, pd.Timestamp("2026-10-20", tz="UTC")) == 1


# ---------- match totals beside team totals (owner-approved 10 Oct) ----------


def mixed_body():
    """FanDuel quotes team totals and the 2.5 total; Pinnacle only a 2.75 total."""
    b = body()
    upd = (K - pd.Timedelta(hours=1)).isoformat()
    b["bookmakers"][0]["markets"].append(
        {
            "key": "totals",
            "outcomes": [
                {"name": "Over", "price": 1.95, "point": 2.5},
                {"name": "Under", "price": 1.87, "point": 2.5},
            ],
        }
    )
    b["bookmakers"].append(
        {
            "key": "pinnacle",
            "last_update": upd,
            "markets": [
                {
                    "key": "totals",
                    "last_update": upd,
                    "outcomes": [
                        {"name": "Over", "price": 2.02, "point": 2.75},
                        {"name": "Under", "price": 1.89, "point": 2.75},
                    ],
                }
            ],
        }
    )
    return b


def test_cap_is_20000_and_a_call_costs_two():
    assert tt.MONTHLY_CAP == 20000 and tt.COST_PER_CALL == 2  # 800 until 28 Oct
    assert tt.MARKETS == ("team_totals", "totals")
    assert "pinnacle" in tt.BOOKS and len(tt.BOOKS) <= 10  # one region


@pytest.mark.parametrize(
    "spent, allowed", [(tt.MONTHLY_CAP - 2, True), (tt.MONTHLY_CAP - 1, False)]
)
def test_cap_counts_the_two_credit_call(tmp_path, spent, allowed):
    raw, out, state = tmp_path / "raw", tmp_path / "out", tmp_path / "state"
    raw.mkdir()
    state.mkdir()
    dk_cache(raw)
    done = {"event_id": "old", "snapshot": "look", "fetched_at": "2026-10-03T12:00:00+00:00"}
    (state / "team_totals_calls_2026-10.jsonl").write_text(json.dumps({**done, "cost": spent}))
    calls = []
    s = tt.run([card()], out, [state], raw, "k", K - pd.Timedelta(hours=24), fake_get(calls))
    if allowed:
        assert s["calls"] == 1 and s["month_spent"] == tt.MONTHLY_CAP and s["stop"] is None
    else:
        cap = tt.MONTHLY_CAP
        assert calls == [] and f"monthly cap: {cap - 1} of {cap}" in s["stop"]


def test_mixed_rows_keep_the_markets_apart(tmp_path):
    raw, out, root = tmp_path / "raw", tmp_path / "out", tmp_path / "log"
    raw.mkdir()
    dk_cache(raw)
    c = card()
    c["total_goals_cdf"] = [0.07, 0.25, 0.5, 0.72, 0.87, 0.95, 0.98]
    calls = []
    s = tt.run([c], out, [], raw, "k", K - pd.Timedelta(hours=24), fake_get(calls, mixed_body()))
    assert s["calls"] == 1 and s["credits"] == 2 and s["rows"] == 4 and s["totals_rows"] == 2
    assert "(2 match totals)" in tt.summary_line(
        s
    ) and f"of {tt.MONTHLY_CAP} credits" in tt.summary_line(s)
    rows = [json.loads(x) for x in (out / "rows.jsonl").read_text().splitlines()]
    team = [r for r in rows if r["market"] == "team_totals"]
    tot = {r["book"]: r for r in rows if r["market"] == "totals"}
    assert len(team) == 2 and {r["side"] for r in team} == {"home", "away"}
    pin = tot["pinnacle"]
    assert pin["team"] is None and pin["side"] == "match" and pin["line"] == 2.75  # as returned
    assert pin["fair_over"] + pin["fair_under"] == pytest.approx(1, abs=1e-6)
    assert pin["margin"] == pytest.approx(1 / 2.02 + 1 / 1.89 - 1, abs=1e-6)
    assert pin["p_model_over"] == pytest.approx(1 - 0.5)  # P(total > 2) for the 2.75 line
    assert tot["fanduel"]["line"] == 2.5 and tot["fanduel"]["p_model_over"] == pytest.approx(0.5)
    assert pin["snapshot"] == "look" and pin["time_source"] == "last_update"
    # One call record per (event, snapshot), naming both markets; never fetched again.
    rec = [json.loads(x) for x in (out / "calls.jsonl").read_text().splitlines()]
    assert len(rec) == 1 and rec[0]["markets"] == "team_totals,totals" and rec[0]["cost"] == 2
    s = tt.run([c], out, [], raw, "k", K - pd.Timedelta(hours=23), fake_get(calls, mixed_body()))
    assert s["calls"] == 0 and len(calls) == 1
    # data-log: both markets in the league's file, deduplicated; one call row.
    assert tt.log(out, root) == (4, 1)
    assert tt.log(out, root) == (0, 0)
    assert len(tt.load_calls(root / "odds_log")) == 1
    # A row logged before `market` existed reads as a team total.
    old = {k: v for k, v in team[0].items() if k != "market"}
    path = root / "odds_log" / "E0_team_totals_2026-10.jsonl"
    path.write_text(path.read_text() + json.dumps({**old, "line": 3.5}) + "\n")
    df = tt.load_rows(root)
    assert df["market"].value_counts().to_dict() == {"team_totals": 3, "totals": 2}
    assert tt.market_of(old) == "team_totals"


def test_pairs_and_report_tell_the_markets_apart():
    rows, res = _logged(60)
    totals = rows.assign(market="totals", team=None, side="match", line=2.5, book="pinnacle")
    both = pd.concat([rows, totals], ignore_index=True)  # old team rows have no market
    out = tt.report(both, res)
    assert out["fanduel_rows"] == 60 and out["pairs"] == 60  # team totals only
    p = tt.pairs(both, res, market="totals")
    assert len(p) == 60 and set(p["book"]) == {"pinnacle"}
    goals = res.set_index("home").loc[p["home"], ["home_goals", "away_goals"]].sum(axis=1)
    assert (p["y"].to_numpy() == (goals.to_numpy() <= 2.5)).all()
    assert len(tt.pairs(both, res)) == 60


def test_match_sheet_shows_only_fanduel_team_totals(tmp_path):
    from soccer_stats.publish import add_team_totals

    out, raw = tmp_path / "out", tmp_path / "raw"
    raw.mkdir()
    dk_cache(raw)
    c = card()
    c["total_goals_cdf"] = [0.07, 0.25, 0.5, 0.72, 0.87, 0.95, 0.98]
    tt.run([c], out, [], raw, "k", K - pd.Timedelta(hours=24), fake_get([], mixed_body()))
    cards = [card()]
    assert add_team_totals(cards, [out]) == 1
    shown = cards[0]["team_totals"]
    assert set(shown) == {"book", "fetched_at", "home", "away"}
    assert [x["line"] for x in shown["home"]] == [1.5] and [x["line"] for x in shown["away"]] == [
        0.5
    ]
