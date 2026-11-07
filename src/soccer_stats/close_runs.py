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

# Settlement runs (18 Oct): one more dispatch SETTLE_AFTER after the last kickoff of each
# cluster, when every match in it should be final (90 minutes, half time and stoppages
# take about 1 h 55 min), so the paper step settles its trades from ESPN at once instead
# of waiting for a throttled scheduled run. Kickoffs within SPAN_MINUTES share one, and a
# planned close run starting within SETTLE_MERGE after that time does the job instead.
SETTLE_AFTER = pd.Timedelta(hours=2, minutes=15)
SETTLE_MERGE = pd.Timedelta(minutes=20)

# Half-time in-play snapshots (lead brief 2, 28 Oct; inplay_log.py): inplay.yml is
# dispatched for each cluster at its first kickoff + INPLAY[kind] + INPLAY_SHIFT, so the
# call (1-3 minutes after the dispatch) lands within about 8 minutes before and 6 after
# the target for every kickoff of the cluster (it spans SPAN_MINUTES): well inside
# inplay_log.WINDOWS. A planner that starts late still dispatches while INPLAY_LATE is left.
INPLAY = {"inplay55": 55, "inplay75": 75}
INPLAY_SHIFT = pd.Timedelta(minutes=3)
INPLAY_LATE = pd.Timedelta(minutes=6)

# Goalscorer entry runs (lead, 7 Nov; player_goal_live, §16 step 2): the live entry snapshot
# wants a publish 20-30 minutes before kickoff (ENTRY_LATEST takes it then even without
# confirmed lineups), which the -15 close run misses, so 9 of the first 26 matches had no
# entry. One more publish at the first kickoff of each cluster in GOALSCORER_LEAGUES minus
# GOALSCORER_LEAD, the cluster spanning GOALSCORER_SPAN: with the usual 1-3 minute start
# the fetch lands 22-29 minutes before every kickoff it covers. A publish already planned
# from GOALSCORER_MERGE[0] before to GOALSCORER_MERGE[1] after that time takes the entry
# instead (a later one would land too near kickoff). It never replaces a close run. A late
# planner still dispatches while GOALSCORER_MIN_LEFT is left before the first kickoff.
GOALSCORER_LEAGUES = ("E0", "SP1", "D1", "I1", "F1")  # player_goal_live.LEAGUES
GOALSCORER_LEAD = pd.Timedelta(minutes=25)
GOALSCORER_SPAN = pd.Timedelta(minutes=5)
GOALSCORER_MERGE = (pd.Timedelta(minutes=5), pd.Timedelta(minutes=2))
GOALSCORER_MIN_LEFT = pd.Timedelta(minutes=23)

WORKFLOWS = {"close": "publish.yml", "settle": "publish.yml", "goalscorer": "publish.yml"} | (
    dict.fromkeys(INPLAY, "inplay.yml")
)
PUBLISH_KINDS = ("close", "settle", "goalscorer")


def kickoffs(log_dir) -> list[pd.Timestamp]:
    """Every distinct kickoff in data-log's odds_log files (the DraftKings log lists each
    priced match, the team-total and corner logs the matches they snapshot). The odds-only
    competitions' events files are left out: they get in-play runs only (inplay_kickoffs)."""
    from soccer_stats.inplay_events import EVENTS_RE

    d = Path(log_dir)
    files = (
        [p for p in sorted(d.glob("*.jsonl")) if not EVENTS_RE.match(p.name)] if d.is_dir() else []
    )
    rows = _read_jsonl(files)
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


def goalscorer_kickoffs(log_dir) -> list[pd.Timestamp]:
    """Kickoffs in the goalscorer leagues: the DraftKings logs <code>_<YYYY-MM>.jsonl of
    GOALSCORER_LEAGUES (every priced match there)."""
    import re

    d = Path(log_dir)
    if not d.is_dir():
        return []
    name = re.compile(rf"^({'|'.join(GOALSCORER_LEAGUES)})_\d{{4}}-\d{{2}}\.jsonl$")
    out = set()
    for r in _read_jsonl([p for p in sorted(d.glob("*.jsonl")) if name.match(p.name)]):
        k = r.get("kickoff") if isinstance(r, dict) else None
        if not k:
            continue
        try:
            out.add(_ts(k))
        except (ValueError, TypeError):
            continue
    return sorted(out)


def inplay_kickoffs(log_dir) -> list[pd.Timestamp]:
    """The odds-only competitions' kickoffs (inplay_events): in-play runs only, never a
    close or settlement run."""
    from soccer_stats.inplay_events import kickoffs as event_kickoffs

    d = Path(log_dir)
    return sorted({k for ks in event_kickoffs(d).values() for k in ks}) if d.is_dir() else []


def plan(ks, now, horizon_hours: float = HORIZON_HOURS, inplay_ks=(), goalscorer_ks=()) -> dict:
    """The dispatch times for kickoffs whose close falls in the next `horizon_hours`.

    Greedy over sorted kickoffs: a dispatch at the first uncovered kickoff minus
    LEAD_MINUTES (or now, if the planner started late) covers every kickoff up to
    SPAN_MINUTES after that one. `continue` says whether a later kickoff is due within
    CONTINUE_HOURS, so the workflow starts the next planner run itself instead of waiting
    for a throttled cron. `inplay_ks` (the odds-only competitions' kickoffs) get in-play
    runs and keep the planner going, but no close or settlement run. `goalscorer_ks` (the
    goalscorer leagues' kickoffs, also in `ks`) get an entry publish GOALSCORER_LEAD before
    each cluster unless a planned publish starts close enough to that time.
    """
    now = _ts(now)
    end = now + pd.Timedelta(hours=horizon_hours)
    lead = pd.Timedelta(minutes=LEAD_MINUTES)
    span = pd.Timedelta(minutes=SPAN_MINUTES)
    soon = now + pd.Timedelta(minutes=MIN_LEFT_MINUTES)
    ks = sorted({_ts(k) for k in ks})
    every = sorted(set(ks) | {_ts(k) for k in inplay_ks})
    due = [k for k in ks if k >= soon and k - lead < end]
    dispatch = []
    i = 0
    while i < len(due):
        first = due[i]
        covered = [k for k in due[i:] if k <= first + span]
        dispatch.append({"at": max(first - lead, now), "kickoffs": covered, "kind": "close"})
        i += len(covered)
    # Settlement runs: kickoffs whose settle time falls in this window.
    settle = [k for k in ks if now <= k + SETTLE_AFTER < end]
    i = 0
    while i < len(settle):
        first = settle[i]
        covered = [k for k in settle[i:] if k <= first + span]
        at = covered[-1] + SETTLE_AFTER
        i += len(covered)
        if any(at <= d["at"] <= at + SETTLE_MERGE for d in dispatch if d["kind"] == "close"):
            continue
        dispatch.append({"at": at, "kickoffs": covered, "kind": "settle"})
    # In-play snapshots: kickoffs grouped as the close runs are, from every kickoff whose
    # snapshot time falls in this window (a late planner still sends one INPLAY_LATE late).
    for kind, minutes in INPLAY.items():
        after = pd.Timedelta(minutes=minutes) + INPLAY_SHIFT
        live = [k for k in every if now - INPLAY_LATE <= k + after < end]
        i = 0
        while i < len(live):
            first = live[i]
            covered = [k for k in live[i:] if k <= first + span]
            i += len(covered)
            dispatch.append({"at": max(first + after, now), "kickoffs": covered, "kind": kind})
    # Goalscorer entry runs: clusters of GOALSCORER_SPAN, merged into a nearby publish.
    gks = sorted({_ts(k) for k in goalscorer_ks})
    gdue = [k for k in gks if k - now >= GOALSCORER_MIN_LEFT and k - GOALSCORER_LEAD < end]
    i = 0
    while i < len(gdue):
        first = gdue[i]
        covered = [k for k in gdue[i:] if k <= first + GOALSCORER_SPAN]
        i += len(covered)
        at = max(first - GOALSCORER_LEAD, now)
        lo, hi = at - GOALSCORER_MERGE[0], at + GOALSCORER_MERGE[1]
        if any(lo <= d["at"] <= hi for d in dispatch if d["kind"] in PUBLISH_KINDS):
            continue
        dispatch.append({"at": at, "kickoffs": covered, "kind": "goalscorer"})
    dispatch.sort(key=lambda d: d["at"])
    later = [k for k in every if k - lead >= end and k <= now + pd.Timedelta(hours=CONTINUE_HOURS)]
    # A cluster whose settlement falls after this window hands over to the next planner too.
    unsettled = [k for k in ks if end <= k + SETTLE_AFTER and k - lead < end]
    return {
        "now": now,
        "horizon_end": end,
        "dispatch": dispatch,
        "continue": bool(later or unsettled),
        "settle_pending": bool(unsettled),
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
            {
                **t(d["at"]),
                "kind": d.get("kind", "close"),
                "workflow": WORKFLOWS[d.get("kind", "close")],
                "kickoffs": [k.isoformat(timespec="minutes") for k in d["kickoffs"]],
            }
            for d in p["dispatch"]
        ],
    }


def describe(p: dict) -> str:
    lines = [f"Close runs planned from {p['now']:%Y-%m-%d %H:%M} to {p['horizon_end']:%H:%M} UTC:"]
    for d in p["dispatch"]:
        ks = ", ".join(f"{k:%H:%M}" for k in d["kickoffs"])
        kind = d.get("kind", "close")
        if kind in INPLAY:
            lines.append(
                f"  in-play snapshot at {d['at']:%Y-%m-%d %H:%M} UTC "
                f"(+{INPLAY[kind]} min) for kickoffs {ks}"
            )
            continue
        why = {"settle": "settle kickoffs", "goalscorer": "goalscorer entry for kickoffs"}.get(
            kind, "for kickoffs"
        )
        lines.append(f"  publish at {d['at']:%Y-%m-%d %H:%M} UTC {why} {ks}")
    if not p["dispatch"]:
        lines.append("  none (no kickoff in this window)")
    if p["continue"] and p["next_kickoff"] is not None:
        lines.append(
            f"Next kickoff {p['next_kickoff']:%Y-%m-%d %H:%M} UTC: a new planner run follows"
        )
    elif p["continue"]:
        lines.append("A settlement run falls after this window: a new planner run follows")
    else:
        lines.append(
            f"No kickoff within {CONTINUE_HOURS:g} h after this window: "
            "the cron starts the next planner"
        )
    return "\n".join(lines)
