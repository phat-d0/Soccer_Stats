"""Phone alerts when a lead strategy opens a paper trade (owner, 5 Nov; push/README.md).

Run by publish.yml right after `paper`, before the data-log commit (CLI `bet-alerts
--log-dir ../log`). It finds the trades this run opened (an "open" event whose id is in the
data-log working tree's ledgers but not in HEAD's), keeps the two lead strategies'
(`eligible`):

* Blend Lean: real live trades of the current rule (`rule_version` 2, inside the odds range);
* Model (f) vs DraftKings corners that pass the "under 60% to win" filter
  (trades.CORNER_VARIANTS["draftkings"]),

and POSTs one alert each to the Cloudflare Worker (ALERT_URL + "/alert", Bearer
ALERT_TOKEN), which sends it to every subscribed phone. Never backcasts, shadows, draw no
bet tracks, Model 12%, Pinnacle corners or settlements.

One alert per trade id, ever: every id sent is recorded in data-log `alerts/sent.jsonl`
and, for a retry within the same run (a data-log push race starts the step again from a
fresh checkout), in a state file outside the worktree (`--state`). Skipped quietly without
the secrets or off the default branch, and it never raises: a failing endpoint costs only
the alert, and the publish goes on.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from . import trades as tr

SENT_FILE = Path("alerts") / "sent.jsonl"
TIMEOUT = 10  # seconds per request
BUDGET = 60  # seconds for the whole step
MAX_ALERTS = 30  # a run that opens more than this sends the first 30 (something is off)

LEAN = "lean"
CORNERS_DK = "corners_dk"
LABELS = {LEAN: "Blend Lean", CORNERS_DK: "Corners (DraftKings)"}
DK_BELOW60 = next(v for v in tr.CORNER_VARIANTS["draftkings"] if v["key"].endswith("_below60"))


# ---------- which trades are new ----------


def _opens(text: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type", "open") == "open" and ev.get("id"):
            out.setdefault(ev["id"], ev)
    return out


def _git(log_dir: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(log_dir), *args], capture_output=True, text=True, timeout=30
    )


def new_opens(log_dir: Path) -> list[dict] | None:
    """The open events in the working tree's ledgers whose ids HEAD's don't have, oldest
    first. None when there is no HEAD to compare with (a brand-new data-log): alerting on
    every trade ever opened would be wrong."""
    log_dir = Path(log_dir)
    if _git(log_dir, "rev-parse", "--verify", "-q", "HEAD").returncode != 0:
        return None
    found: list[dict] = []
    for path in sorted((log_dir / "paper_trades").glob("*.jsonl")):
        rel = path.relative_to(log_dir).as_posix()
        old = _git(log_dir, "show", f"HEAD:{rel}")
        before = _opens(old.stdout) if old.returncode == 0 else {}
        for tid, ev in _opens(path.read_text()).items():
            if tid not in before:
                found.append(ev)
    return sorted(found, key=lambda e: (e.get("opened_at") or "", e["id"]))


# ---------- which trades alert ----------


def kind_of(t: dict) -> str | None:
    """LEAN, CORNERS_DK or None (no alert)."""
    if t.get("backcast") or t.get("shadow") or t.get("source", "live") != "live":
        return None
    tid = str(t.get("id", ""))
    if tid.endswith(tr.SHADOW_SUFFIX) or tid.endswith(tr.DNB_SUFFIX):
        return None
    if t.get("bet_type") == "match" and tr.strategy_of(t) == "lean":
        if t.get("rule_version") != tr.LEAN_RULE_VERSION or not tr.in_lean_range(t.get("odds")):
            return None
        return LEAN
    if t.get("bet_type") == "corners" and tr.corner_strategy_of(t) == "draftkings":
        return CORNERS_DK if tr.passes_filter(t, DK_BELOW60["filter"]) else None
    return None


# ---------- the message ----------


def american(d: float) -> str:
    a = round((d - 1) * 100) if d >= 2 else round(-100 / (d - 1))
    return f"+{a}" if a > 0 else f"−{abs(a)}"


def pick_text(t: dict) -> str:
    if t.get("bet_type") == "corners":
        team = t.get("team") or (t["away"] if t.get("team_side") == "away" else t["home"])
        line = t.get("line")
        line = int(line) if isinstance(line, float) and line.is_integer() else line
        return f"{team} {t.get('side')} {line} corners"
    m = t.get("market")
    return {"home": f"{t['home']} to win", "away": f"{t['away']} to win", "draw": "Draw"}.get(
        m, str(m)
    )


def message(t: dict, kind: str) -> dict:
    """The alert: title "Blend Lean: Arsenal v Leeds" or "Corners (DraftKings): Arsenal
    over 5.5"; body pick, odds American and decimal, the stake; `kickoff` (ISO) for the
    phone to show in its own time zone; `url` opens the app on the trade's sheet."""
    odds = float(t["odds"])
    if kind == CORNERS_DK:
        title = f"{LABELS[kind]}: {pick_text(t).removesuffix(' corners')}"
    else:
        title = f"{LABELS[kind]}: {t['home']} v {t['away']}"
    stake = t.get("stake") or 10
    body = f"{pick_text(t)} · {american(odds)} ({odds:.2f}) · ${stake:g} stake"
    if kind == CORNERS_DK:
        body += f" · {t['home']} v {t['away']}"
    return {
        "title": title,
        "body": body,
        "kickoff": t.get("kickoff"),
        "url": f"./?trade={_quote(t['id'])}",
        "tag": t["id"],
    }


def _quote(s: str) -> str:
    from urllib.parse import quote

    return quote(s, safe="")


# ---------- the sent record ----------


def load_sent(*paths: Path | None) -> set[str]:
    ids: set[str] = set()
    for p in paths:
        if p and Path(p).exists():
            ids |= set(_opens_ids(Path(p).read_text()))
    return ids


def _opens_ids(text: str):
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("id"):
            yield row["id"]


def _record(paths: list[Path], row: dict) -> None:
    line = json.dumps(row, separators=(",", ":")) + "\n"
    for p in paths:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            f.write(line)


# ---------- sending ----------


def _post(url: str, token: str, payload: dict) -> int:
    import requests

    r = requests.post(
        url,
        json=payload,
        headers={"Authorization": f"Bearer {token}", "User-Agent": "soccer-model-alerts"},
        timeout=TIMEOUT,
    )
    return r.status_code


def run(
    log_dir: Path,
    state: Path | None = None,
    env: dict | None = None,
    post: Callable[[str, str, dict], int] = _post,
    now: datetime | None = None,
    opens: list[dict] | None = None,
) -> dict:
    """Send this run's alerts; returns a summary (`line` is the log line). Never raises."""
    env = os.environ if env is None else env
    url, token = (env.get("ALERT_URL") or "").strip(), (env.get("ALERT_TOKEN") or "").strip()
    ref, default = env.get("GITHUB_REF_NAME"), env.get("DEFAULT_BRANCH")
    if not url or not token:
        return _summary(off="ALERT_URL or ALERT_TOKEN not set")
    if ref and default and ref != default:
        return _summary(off="not the default branch")
    log_dir = Path(log_dir)
    try:
        opens = new_opens(log_dir) if opens is None else opens
    except Exception as e:  # noqa: BLE001 - a git problem costs the alerts, never the publish
        return _summary(error=f"reading the ledgers: {type(e).__name__}")
    if opens is None:
        return _summary(off="data-log has no history to compare with")
    sent_paths = [log_dir / SENT_FILE] + ([Path(state)] if state else [])
    sent = load_sent(*sent_paths)
    counts = {LEAN: 0, CORNERS_DK: 0}
    skipped = failed = 0
    errors: list[str] = []
    start = time.monotonic()
    endpoint = url.rstrip("/") + "/alert"
    for t in opens:
        kind = kind_of(t)
        if kind is None or t["id"] in sent or sum(counts.values()) >= MAX_ALERTS:
            skipped += 1
            continue
        if time.monotonic() - start > BUDGET:
            failed += 1
            errors.append("time budget")
            continue
        try:
            status = post(endpoint, token, message(t, kind))
        except Exception as e:  # noqa: BLE001
            status, err = 0, type(e).__name__
        else:
            err = f"HTTP {status}"
        if 200 <= status < 300:
            counts[kind] += 1
            sent.add(t["id"])
            at = (now or datetime.now(UTC)).isoformat(timespec="seconds")
            _record(sent_paths, {"id": t["id"], "kind": kind, "sent_at": at})
        else:
            failed += 1
            errors.append(err)
    return _summary(counts=counts, skipped=skipped, failed=failed, errors=errors)


def _summary(counts=None, skipped=0, failed=0, errors=(), off=None, error=None) -> dict:
    counts = counts or {LEAN: 0, CORNERS_DK: 0}
    n = sum(counts.values())
    if off:
        line = f"Bet alerts: off ({off})"
    elif error:
        line = f"Bet alerts: skipped ({error})"
    else:
        line = (
            f"Bet alerts: {n} sent (Lean {counts[LEAN]}, corners {counts[CORNERS_DK]}), "
            f"{skipped} skipped"
        )
        if failed:
            line += f", {failed} failed ({', '.join(sorted(set(errors)))})"
    return {
        "sent": n,
        "lean": counts[LEAN],
        "corners": counts[CORNERS_DK],
        "skipped": skipped,
        "failed": failed,
        "off": off,
        "error": error,
        "line": line,
    }
