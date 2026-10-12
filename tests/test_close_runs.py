import json

import pandas as pd

from soccer_stats import close_runs as cr
from soccer_stats.team_totals import due_snapshot


def T(s):
    return pd.Timestamp(s, tz="UTC")


def test_one_run_per_cluster_of_kickoffs():
    now = T("2026-10-17 10:00")
    ks = [
        T("2026-10-17 14:00"),
        T("2026-10-17 14:00"),
        T("2026-10-17 14:05"),
        T("2026-10-17 15:30"),
    ]
    p = cr.plan(ks, now)
    assert [d["at"] for d in p["dispatch"]] == [T("2026-10-17 13:45"), T("2026-10-17 15:15")]
    assert p["dispatch"][0]["kickoffs"] == [T("2026-10-17 14:00"), T("2026-10-17 14:05")]


def test_kickoffs_too_far_apart_get_their_own_run():
    now = T("2026-10-17 10:00")
    p = cr.plan([T("2026-10-17 14:00"), T("2026-10-17 14:13")], now)
    assert [d["at"] for d in p["dispatch"]] == [T("2026-10-17 13:45"), T("2026-10-17 13:58")]


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
    for d in cr.plan(ks, now)["dispatch"]:
        for delay in (0, 3, 6):
            for k in d["kickoffs"]:
                assert due_snapshot(k, d["at"] + pd.Timedelta(minutes=delay), runs=[]) == "close"


def test_late_planner_dispatches_now_or_skips():
    k = T("2026-10-17 15:00")
    p = cr.plan([k], T("2026-10-17 14:52"))  # 8 minutes left: still worth a run
    assert [d["at"] for d in p["dispatch"]] == [T("2026-10-17 14:52")]
    assert cr.plan([k], T("2026-10-17 14:56"))["dispatch"] == []  # too close
    assert cr.plan([k], T("2026-10-17 15:30"))["dispatch"] == []  # already kicked off


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
    assert j["continue"] is False and j["next_kickoff"] is None
    assert "13:45" in cr.describe(p)


def test_cli_writes_the_plan(tmp_path, capsys):
    from soccer_stats.cli import main

    logs = tmp_path / "odds_log"
    logs.mkdir()
    (logs / "E0_2026-10.jsonl").write_text(json.dumps({"kickoff": "2026-10-17T14:00:00Z"}) + "\n")
    out = tmp_path / "plan.json"
    main(["close-plan", "--log-dir", str(logs), "--out", str(out), "--now", "2026-10-17T10:00Z"])
    assert json.loads(out.read_text())["dispatch"][0]["kickoffs"] == ["2026-10-17T14:00+00:00"]
    assert "publish at 2026-10-17 13:45 UTC" in capsys.readouterr().out
