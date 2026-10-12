"""When to start an extra publish run so each match's close snapshot lands in its window.

GitHub throttles scheduled workflows: from 10 to 12 Oct 2026 there were 18 scheduled
publish runs instead of about 120, with gaps of up to 6.5 hours, so only 12 of 41 matches
with a team-total look got a close (team_totals.due_snapshot wants a run within
CLOSE_MINUTES of kickoff), and 2 of 10 corner matches.

closes.yml reads the coming kickoffs from data-log's logs, sleeps until each time planned
here and dispatches publish.yml (workflow_dispatch runs aren't throttled like cron). The
dispatched run is an ordinary publish: the call records still never fetch a snapshot twice
and every cap and reserve applies, so a close costs what it always did. Only the timing
moves.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from soccer_stats.team_totals import CLOSE_MINUTES, _read_jsonl, _ts

# A dispatched run reaches the snapshot fetch about 1–3 minutes after it is requested
# (more if it queues behind a running publish), so ask LEAD_MINUTES before the first
# kickoff of a cluster and let one run cover kickoffs up to SPAN_MINUTES after it: the
# fetch then lands 9–27 minutes before each of them, inside the 30-minute close window,
# with a few minutes to spare for a slow start.
LEAD_MINUTES = 15
SPAN_MINUTES = 12
# A planner that starts late still dispatches while this much time is left before the
# kickoff; any closer and the run would reach the fetch after it.
MIN_LEFT_MINUTES = 6
# One planner run plans this far ahead (a GitHub job may run for 6 hours at most) ...
HORIZON_HOURS = 5.5
# ... and hands over to a new planner run when a kickoff is due within this many hours.
CONTINUE_HOURS = 30.0

assert LEAD_MINUTES + SPAN_MINUTES < CLOSE_MINUTES


def kickoffs(log_dir) -> list[pd.Timestamp]:
    """Every distinct kickoff in data-log's odds_log files (the DraftKings log lists each
    priced match, the team-total and corner logs the matches they snapshot)."""
    d = Path(log_dir)
    rows = _read_jsonl(sorted(d.glob("*.jsonl"))) if d.is_dir() else []
    out = set()
    for r in rows:
        k = r.get("kickoff") if isinstance(r, dict) else None
        if not k:
            continue
        try:
            out.add(_ts(k))
        except (ValueError, TypeError):
            continue
    return sorted(out)


def plan(ks, now, horizon_hours: float = HORIZON_HOURS) -> dict:
    """The dispatch times for kickoffs whose close falls in the next `horizon_hours`.

    Greedy over sorted kickoffs: a dispatch at the first uncovered kickoff minus
    LEAD_MINUTES (or now, if the planner started late) covers every kickoff up to
    SPAN_MINUTES after that one. `continue` says whether a later kickoff is due within
    CONTINUE_HOURS, so the workflow starts the next planner run itself instead of waiting
    for a throttled cron.
    """
    now = _ts(now)
    end = now + pd.Timedelta(hours=horizon_hours)
    lead = pd.Timedelta(minutes=LEAD_MINUTES)
    span = pd.Timedelta(minutes=SPAN_MINUTES)
    soon = now + pd.Timedelta(minutes=MIN_LEFT_MINUTES)
    ks = sorted({_ts(k) for k in ks})
    due = [k for k in ks if k >= soon and k - lead < end]
    dispatch = []
    i = 0
    while i < len(due):
        first = due[i]
        covered = [k for k in due[i:] if k <= first + span]
        dispatch.append({"at": max(first - lead, now), "kickoffs": covered})
        i += len(covered)
    later = [k for k in ks if k - lead >= end and k <= now + pd.Timedelta(hours=CONTINUE_HOURS)]
    return {
        "now": now,
        "horizon_end": end,
        "dispatch": dispatch,
        "continue": bool(later),
        "next_kickoff": later[0] if later else None,
    }


def to_json(p: dict) -> dict:
    """The plan for the workflow: ISO times plus epoch seconds for `sleep`."""

    def t(x):
        return (
            None
            if x is None
            else {"iso": x.isoformat(timespec="seconds"), "epoch": int(x.timestamp())}
        )

    return {
        "now": t(p["now"]),
        "horizon_end": t(p["horizon_end"]),
        "continue": p["continue"],
        "next_kickoff": t(p["next_kickoff"]),
        "dispatch": [
            {**t(d["at"]), "kickoffs": [k.isoformat(timespec="minutes") for k in d["kickoffs"]]}
            for d in p["dispatch"]
        ],
    }


def describe(p: dict) -> str:
    lines = [f"Close runs planned from {p['now']:%Y-%m-%d %H:%M} to {p['horizon_end']:%H:%M} UTC:"]
    for d in p["dispatch"]:
        ks = ", ".join(f"{k:%H:%M}" for k in d["kickoffs"])
        lines.append(f"  publish at {d['at']:%Y-%m-%d %H:%M} UTC for kickoffs {ks}")
    if not p["dispatch"]:
        lines.append("  none (no kickoff in this window)")
    if p["continue"]:
        lines.append(
            f"Next kickoff {p['next_kickoff']:%Y-%m-%d %H:%M} UTC: a new planner run follows"
        )
    else:
        lines.append(
            f"No kickoff within {CONTINUE_HOURS:g} h after this window: "
            "the cron starts the next planner"
        )
    return "\n".join(lines)
