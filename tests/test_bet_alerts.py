"""Bet alerts (owner, 5 Nov; bet_alerts.py): only the lead strategies' new live trades
alert (Blend Lean's current rule, DraftKings corners under 60%), once per trade id, even
across a retried data-log push; off without the secrets or off the default branch; a
failing endpoint never raises. A temporary git repo stands in for data-log."""

import json
import subprocess
from pathlib import Path

import pytest

from soccer_stats import bet_alerts as ba

ENV = {"ALERT_URL": "https://alerts.example.workers.dev/", "ALERT_TOKEN": "tok"}


def lean(home="Arsenal", away="Leeds", **kw):
    t = {
        "type": "open",
        "id": f"E0|2627|{home}|{away}|lean",
        "source": "live",
        "bet_type": "match",
        "league": "E0",
        "opened_at": "2026-11-05T10:00:00+00:00",
        "kickoff": "2026-11-07T15:00:00+00:00",
        "home": home,
        "away": away,
        "market": "home",
        "odds": 2.5,
        "stake": 10.0,
        "strategy": "lean",
        "rule_version": 2,
        "status": "open",
    }
    t.update(kw)
    return t


def corner(team="Arsenal", p=0.55, dk=True, **kw):
    t = {
        "type": "open",
        "id": f"E0|2627|{team}|Leeds|corners|home|5.5" + ("|dk" if dk else ""),
        "source": "live",
        "bet_type": "corners",
        "league": "E0",
        "opened_at": "2026-11-05T10:01:00+00:00",
        "kickoff": "2026-11-07T15:00:00+00:00",
        "home": team,
        "away": "Leeds",
        "market": "team_corners",
        "team": team,
        "team_side": "home",
        "line": 5.5,
        "side": "over",
        "odds": 1.91,
        "stake": 10.0,
        "model_p": p,
        "p_push": 0.0,
        "strategy": "draftkings" if dk else "pinnacle",
        "bookmaker": "draftkings" if dk else "pinnacle",
        "status": "open",
    }
    t.update(kw)
    return t


def git(d: Path, *args):
    subprocess.run(["git", "-C", str(d), *args], check=True, capture_output=True)


def write(d: Path, name: str, rows: list[dict], mode="a"):
    p = d / "paper_trades" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, mode) as f:
        f.write("".join(json.dumps(r) + "\n" for r in rows))


@pytest.fixture
def log(tmp_path):
    d = tmp_path / "log"
    d.mkdir()
    git(d, "init", "-q")
    git(d, "config", "user.email", "t@example.com")
    git(d, "config", "user.name", "t")
    write(d, "E0_2627.jsonl", [lean("Old", "Trade")])  # already in HEAD: never alerts
    git(d, "add", "-A")
    git(d, "commit", "-q", "-m", "base")
    return d


class Poster:
    def __init__(self, status=200, raise_exc=None):
        self.calls, self.status, self.raise_exc = [], status, raise_exc

    def __call__(self, url, token, payload):
        self.calls.append((url, token, payload))
        if self.raise_exc:
            raise self.raise_exc
        return self.status


def test_only_the_lead_strategies_new_live_trades_alert(log):
    write(
        log,
        "E0_2627.jsonl",
        [
            lean(),  # alerts
            lean("Chelsea", "Fulham", rule_version=None),  # the old rule (any odds)
            lean("Spurs", "Wolves", odds=3.4),  # outside the 1.50-3.00 range
            lean("Everton", "Hull", backcast=True),  # backcast entry
            lean("Brighton", "Burnley", strategy="edge12", id="E0|2627|Brighton|Burnley"),
            {"type": "update", "id": "E0|2627|Old|Trade|lean", "status": "won"},  # settlement
        ],
    )
    write(
        log,
        "E0_lean_shadow_2627.jsonl",
        [lean("Bournemouth", "Leeds", shadow=True, id="E0|2627|Bournemouth|Leeds|lean|shadow")],
    )
    write(
        log,
        "E0_dnb_2627.jsonl",
        [lean("Villa", "Leeds", bet_type="dnb", id="E0|2627|Villa|Leeds|lean|dnb")],
    )
    # A new file HEAD has never seen (git diff alone would miss it).
    write(
        log,
        "E0_corners_2627.jsonl",
        [corner(), corner("Newcastle", p=0.65), corner("Brentford", dk=False)],
    )
    post = Poster()
    res = ba.run(log, env=ENV, post=post)
    sent = [c[2]["tag"] for c in post.calls]
    assert sent == ["E0|2627|Arsenal|Leeds|lean", "E0|2627|Arsenal|Leeds|corners|home|5.5|dk"]
    assert (res["sent"], res["lean"], res["corners"]) == (2, 1, 1)
    assert res["line"] == "Bet alerts: 2 sent (Lean 1, corners 1), 8 skipped"
    url, token, msg = post.calls[0]
    assert url == "https://alerts.example.workers.dev/alert" and token == "tok"
    assert msg["title"] == "Blend Lean: Arsenal v Leeds"
    assert msg["body"] == "Arsenal to win · +150 (2.50) · $10 stake"
    assert msg["kickoff"] == "2026-11-07T15:00:00+00:00"
    assert msg["url"] == "./?trade=E0%7C2627%7CArsenal%7CLeeds%7Clean"
    cmsg = post.calls[1][2]
    assert cmsg["title"] == "Corners (DraftKings): Arsenal over 5.5"
    assert cmsg["body"].startswith("Arsenal over 5.5 corners · −110 (1.91) · $10 stake")


def test_one_alert_per_trade_even_after_a_retried_push(log, tmp_path):
    write(log, "E0_2627.jsonl", [lean()])
    state = tmp_path / "state.jsonl"
    post = Poster()
    assert ba.run(log, state=state, env=ENV, post=post)["sent"] == 1
    # Run again on the same working tree: recorded in alerts/sent.jsonl, nothing re-sent.
    assert ba.run(log, state=state, env=ENV, post=post)["sent"] == 0
    rows = [json.loads(x) for x in (log / ba.SENT_FILE).read_text().splitlines()]
    assert [r["id"] for r in rows] == ["E0|2627|Arsenal|Leeds|lean"] and rows[0]["kind"] == "lean"
    # A push race: the step starts again from a fresh data-log checkout (no sent.jsonl), and
    # paper opens the same trade again. The state file outside the checkout still knows it.
    git(log, "checkout", "-q", "--", ".")
    git(log, "clean", "-qfd")
    write(log, "E0_2627.jsonl", [lean()])
    assert ba.run(log, state=state, env=ENV, post=post)["sent"] == 0
    assert len(post.calls) == 1


def test_off_without_the_secrets_or_off_the_default_branch(log):
    write(log, "E0_2627.jsonl", [lean()])
    post = Poster()
    res = ba.run(log, env={}, post=post)
    assert res["line"] == "Bet alerts: off (ALERT_URL or ALERT_TOKEN not set)"
    res = ba.run(
        log, env={**ENV, "GITHUB_REF_NAME": "team/ui", "DEFAULT_BRANCH": "main"}, post=post
    )
    assert res["line"] == "Bet alerts: off (not the default branch)"
    assert not post.calls and not (log / ba.SENT_FILE).exists()


def test_a_failing_endpoint_never_raises_and_isnt_recorded(log):
    write(log, "E0_2627.jsonl", [lean(), lean("Chelsea", "Fulham")])
    res = ba.run(log, env=ENV, post=Poster(status=500))
    assert res["sent"] == 0 and res["failed"] == 2
    assert res["line"] == "Bet alerts: 0 sent (Lean 0, corners 0), 0 skipped, 2 failed (HTTP 500)"
    res = ba.run(log, env=ENV, post=Poster(raise_exc=TimeoutError("slow https://secret")))
    assert res["failed"] == 2 and "TimeoutError" in res["line"] and "secret" not in res["line"]
    assert not (log / ba.SENT_FILE).exists()


def test_no_history_means_no_alerts(tmp_path):
    d = tmp_path / "fresh"
    d.mkdir()
    git(d, "init", "-q")
    write(d, "E0_2627.jsonl", [lean()])
    res = ba.run(d, env=ENV, post=Poster())
    assert res["line"] == "Bet alerts: off (data-log has no history to compare with)"


def test_american_odds_and_picks():
    assert ba.american(2.5) == "+150" and ba.american(1.91) == "−110"
    assert ba.american(2.0) == "+100"
    assert ba.pick_text(lean(market="draw")) == "Draw"
    assert ba.pick_text(lean(market="away")) == "Leeds to win"
    assert ba.pick_text(corner(line=4.0, side="under")) == "Arsenal under 4 corners"


def test_the_app_gets_only_the_workers_address():
    from soccer_stats import publish

    assert publish.bet_alerts_block({}) == {"worker": None}
    assert publish.bet_alerts_block({"ALERT_URL": "https://a.example.workers.dev/"}) == {
        "worker": "https://a.example.workers.dev"
    }
    assert publish.bet_alerts_block({"ALERT_URL": "http://insecure"}) == {"worker": None}
    assert "ALERT_TOKEN" not in str(publish.bet_alerts_block({"ALERT_TOKEN": "x"}))


def test_the_test_alert_says_how_many_phones_it_reached():
    calls = []

    def post(url, token, payload):
        calls.append((url, token, payload))
        return 200, {"ok": True, "sent": 1, "removed": 1, "failed": 0}

    res = ba.send_test(ENV, post=post)
    assert res["line"] == "Bet alerts: test sent to 1 phone (1 old subscription removed)"
    assert (
        calls[0][0] == "https://alerts.example.workers.dev/alert" and calls[0][2]["tag"] == "test"
    )
    none = ba.send_test(ENV, post=lambda *a: (200, {"sent": 0}))
    assert none["line"].endswith("no phone has turned alerts on yet") and not none["ok"]
    assert ba.send_test(ENV, post=lambda *a: (401, {}))["line"] == (
        "Bet alerts: test failed (wrong ALERT_TOKEN)"
    )
    assert "not set" in ba.send_test({}, post=post)["line"]

    def boom(*a):
        raise ConnectionError("https://secret")

    assert ba.send_test(ENV, post=boom)["line"] == "Bet alerts: test failed (ConnectionError)"
