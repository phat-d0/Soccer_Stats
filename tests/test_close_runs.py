import json

import pandas as pd

from soccer_stats import close_runs as cr
from soccer_stats.team_totals import due_snapshot


def T(s):
    return pd.Timestamp(s, tz="UTC")


def closes(p):
    return [d for d in p["dispatch"] if d["kind"] == "close"]


def publishes(p):
    """The publish runs (closes and settlements), without the in-play snapshots."""
    return [d for d in p["dispatch"] if d["kind"] not in cr.INPLAY]


def test_one_run_per_cluster_of_kickoffs():
    now = T("2026-10-17 10:00")
    ks = [
        T("2026-10-17 14:00"),
        T("2026-10-17 14:00"),
        T("2026-10-17 14:05"),
        T("2026-10-17 15:30"),
    ]
    p = cr.plan(ks, now)
    assert [d["at"] for d in publishes(p)] == [T("2026-10-17 13:45"), T("2026-10-17 15:15")]
    assert publishes(p)[0]["kickoffs"] == [T("2026-10-17 14:00"), T("2026-10-17 14:05")]


def test_kickoffs_too_far_apart_get_their_own_run():
    now = T("2026-10-17 10:00")
    p = cr.plan([T("2026-10-17 14:00"), T("2026-10-17 14:13")], now)
    assert [d["at"] for d in publishes(p)] == [T("2026-10-17 13:45"), T("2026-10-17 13:58")]


def test_every_covered_kickoff_gets_a_close_from_the_dispatched_run():
    """The run's fetch lands up to a few minutes after the dispatch; each kickoff it
    covers must then still be due a close (not a look, not past kickoff)."""
    now = T("2026-10-17 08:00")
    ks = [
        T("2026-10-17 11:30"),
        T("2026-10-17 11:42"),
        T("2026-10-17 12:00"),
        T("2026-10-17 13:30"),
    ]
    for d in closes(cr.plan(ks, now)):
        for delay in (0, 3, 6):
            for k in d["kickoffs"]:
                assert due_snapshot(k, d["at"] + pd.Timedelta(minutes=delay), runs=[]) == "close"


def test_late_planner_dispatches_now_or_skips():
    k = T("2026-10-17 15:00")
    p = cr.plan([k], T("2026-10-17 14:52"))  # 8 minutes left: still worth a run
    assert [d["at"] for d in closes(p)] == [T("2026-10-17 14:52")]
    assert closes(cr.plan([k], T("2026-10-17 14:56"))) == []  # too close
    assert closes(cr.plan([k], T("2026-10-17 15:30"))) == []  # already kicked off


def test_horizon_and_handover():
    now = T("2026-10-17 06:00")
    inside = T("2026-10-17 11:44")  # dispatch at 11:29, before 11:30
    outside = T("2026-10-17 11:46")  # dispatch at 11:31: the next planner's job
    p = cr.plan([inside, outside], now)
    assert [d["kickoffs"] for d in p["dispatch"]] == [[inside]]
    assert p["horizon_end"] == T("2026-10-17 11:30")
    assert p["continue"] is True and p["next_kickoff"] == outside

    far = T("2026-10-18 13:00")  # 31 h away: the cron starts the next planner
    q = cr.plan([far], now)
    assert q["dispatch"] == [] and q["continue"] is False


def test_kickoffs_read_from_every_log_file(tmp_path):
    (tmp_path / "E0_2026-10.jsonl").write_text(
        json.dumps({"league": "E0", "kickoff": "2026-10-17T14:00:00+00:00"})
        + "\n"
        + json.dumps({"league": "E0", "kickoff": "2026-10-17T14:00:00+00:00"})
        + "\nnot json\n"
    )
    (tmp_path / "team_totals_calls_2026-10.jsonl").write_text(
        json.dumps({"league": "SP1", "kickoff": "2026-10-17T16:30:00Z", "snapshot": "look"})
        + "\n"
        + json.dumps({"league": "SP1", "kickoff": None})
        + "\n"
        + json.dumps({"league": "SP1", "kickoff": "not a time"})
        + "\n"
    )
    (tmp_path / "notes.txt").write_text("ignored")
    assert cr.kickoffs(tmp_path) == [T("2026-10-17 14:00"), T("2026-10-17 16:30")]
    assert cr.kickoffs(tmp_path / "missing") == []


def test_plan_json_for_the_workflow():
    p = cr.plan([T("2026-10-17 14:00")], T("2026-10-17 10:00"))
    j = cr.to_json(p)
    assert j["dispatch"][0]["epoch"] == int(T("2026-10-17 13:45").timestamp())
    assert j["dispatch"][0]["kickoffs"] == ["2026-10-17T14:00+00:00"]
    assert j["horizon_end"]["epoch"] == int(T("2026-10-17 15:30").timestamp())
    assert j["dispatch"][0]["kind"] == "close"
    # Its settlement run (16:15) falls after the window: the next planner dispatches it.
    assert j["continue"] is True and j["next_kickoff"] is None
    assert "13:45" in cr.describe(p) and "settlement run" in cr.describe(p)


def test_a_settlement_run_after_each_cluster_of_kickoffs():
    now = T("2026-10-17 12:00")  # window to 17:30
    ks = [T("2026-10-17 11:00"), T("2026-10-17 14:00"), T("2026-10-17 14:05")]
    p = cr.plan(ks, now)
    settles = [d for d in p["dispatch"] if d["kind"] == "settle"]
    # 11:00 kicked off before the planner started: still settled at 13:15; the 14:00 and
    # 14:05 kickoffs share one run 2 h 15 min after the later one.
    assert [d["at"] for d in settles] == [T("2026-10-17 13:15"), T("2026-10-17 16:20")]
    assert settles[1]["kickoffs"] == [T("2026-10-17 14:00"), T("2026-10-17 14:05")]
    assert [d["at"] for d in p["dispatch"]] == sorted(d["at"] for d in p["dispatch"])
    assert p["continue"] is False  # nothing left for a later planner
    j = cr.to_json(p)
    kinds = [d["kind"] for d in j["dispatch"] if d["kind"] not in cr.INPLAY]
    assert kinds == ["settle", "close", "settle"]
    assert "settle kickoffs 14:00, 14:05" in cr.describe(p)
    # A settlement already past is skipped (the scheduled runs have it).
    late = cr.plan([T("2026-10-17 09:00")], now)
    assert late["dispatch"] == [] and late["continue"] is False


def test_a_close_run_just_after_full_time_does_the_settling():
    now = T("2026-10-17 12:00")
    # 13:00 settles at 15:15; the 15:30 kickoff's close run at 15:15 covers it.
    p = cr.plan([T("2026-10-17 13:00"), T("2026-10-17 15:30")], now)
    assert [(d["at"], d["kind"]) for d in publishes(p)] == [
        (T("2026-10-17 12:45"), "close"),
        (T("2026-10-17 15:15"), "close"),
    ]
    assert p["continue"] is True  # 15:30's own settlement (17:45) is after the window


def test_cli_writes_the_plan(tmp_path, capsys):
    from soccer_stats.cli import main

    logs = tmp_path / "odds_log"
    logs.mkdir()
    (logs / "E0_2026-10.jsonl").write_text(json.dumps({"kickoff": "2026-10-17T14:00:00Z"}) + "\n")
    out = tmp_path / "plan.json"
    main(["close-plan", "--log-dir", str(logs), "--out", str(out), "--now", "2026-10-17T10:00Z"])
    assert json.loads(out.read_text())["dispatch"][0]["kickoffs"] == ["2026-10-17T14:00+00:00"]
    assert "publish at 2026-10-17 13:45 UTC" in capsys.readouterr().out


def test_in_play_snapshots_at_55_and_75_minutes_per_cluster():
    """Lead brief 2 (28 Oct): inplay.yml at each cluster's first kickoff + 55 and + 75
    minutes (+ INPLAY_SHIFT), so the call lands inside inplay_log's window for every
    kickoff of the cluster."""
    from soccer_stats import inplay_log as il

    now = T("2026-10-17 12:00")
    ks = [T("2026-10-17 14:00"), T("2026-10-17 14:12"), T("2026-10-17 16:30")]
    p = cr.plan(ks, now)
    inplay = [d for d in p["dispatch"] if d["kind"] in cr.INPLAY]
    assert [(d["at"], d["kind"]) for d in inplay] == [
        (T("2026-10-17 14:58"), "inplay55"),
        (T("2026-10-17 15:18"), "inplay75"),
        (T("2026-10-17 17:28"), "inplay55"),
    ]  # 16:30's +75 (17:48) falls after the window: the next planner's
    assert inplay[0]["kickoffs"] == [T("2026-10-17 14:00"), T("2026-10-17 14:12")]
    for d in inplay:
        for delay in (1, 3):
            at = d["at"] + pd.Timedelta(minutes=delay)
            for k in d["kickoffs"]:
                assert il.kind_of((at - k) / pd.Timedelta(minutes=1)) == d["kind"]
    j = cr.to_json(p)
    assert {d["workflow"] for d in j["dispatch"] if d["kind"] in cr.INPLAY} == {"inplay.yml"}
    assert {d["workflow"] for d in j["dispatch"] if d["kind"] not in cr.INPLAY} == {"publish.yml"}
    assert "in-play snapshot at 2026-10-17 14:58 UTC (+55 min) for kickoffs 14:00, 14:12" in (
        cr.describe(p)
    )
    # A planner starting late still sends one within INPLAY_LATE; later, none.
    assert [
        d["at"]
        for d in cr.plan([T("2026-10-17 14:00")], T("2026-10-17 15:02"))["dispatch"]
        if d["kind"] == "inplay55"
    ] == [T("2026-10-17 15:02")]
    assert not [
        d
        for d in cr.plan([T("2026-10-17 14:00")], T("2026-10-17 15:05"))["dispatch"]
        if d["kind"] == "inplay55"
    ]
