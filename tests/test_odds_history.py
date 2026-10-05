import pandas as pd

from soccer_stats import odds_history as oh


def ts(s):
    return pd.Timestamp(s, tz="UTC")


def test_plan_shares_snapshots_between_close_kickoffs():
    plan = oh.plan_snapshots(
        [ts("2025-09-13 14:00"), ts("2025-09-13 14:10"), ts("2025-09-13 16:30")]
    )
    assert len(plan) == 9  # three kickoffs x (two looks + close)
    assert plan["at"].nunique() == 6  # the 14:00 and 14:10 kickoffs share theirs
    assert (plan["at"] < plan["kickoff"]).all()
    first = plan[plan["kickoff"] == ts("2025-09-13 14:10")].set_index("kind")["at"]
    assert first["look3"] == ts("2025-09-13 11:00")
    assert first["close"] == ts("2025-09-13 13:55")


class FakeResp:
    def __init__(self, remaining, cost=20):
        self.ok, self.status_code = True, 200
        self.headers = {"x-requests-remaining": str(remaining), "x-requests-last": str(cost)}

    def json(self):
        return {
            "timestamp": "2025-09-13T10:55:00Z",
            "data": [
                {
                    "commence_time": "2025-09-13T14:00:00Z",
                    "home_team": "Arsenal",
                    "away_team": "Leeds United",
                    "bookmakers": [
                        {
                            "key": "draftkings",
                            "last_update": "2025-09-13T10:50:00Z",
                            "markets": [
                                {
                                    "key": "h2h",
                                    "outcomes": [
                                        {"name": "Arsenal", "price": 1.4},
                                        {"name": "Leeds United", "price": 7.5},
                                        {"name": "Draw", "price": 4.8},
                                    ],
                                }
                            ],
                        },
                        {"key": "fanduel", "markets": []},
                    ],
                }
            ],
        }


def test_dry_run_makes_no_call(tmp_path):
    def boom(*a, **k):
        raise AssertionError("no API call in a dry run")

    times = [ts("2025-09-13 11:00"), ts("2025-09-13 13:55")]
    rep = oh.backfill(times, max_credits=1000, dry_run=True, raw_dir=tmp_path, get=boom)
    assert rep.planned == 2 and rep.fetched == 0 and rep.estimated_credits == 40


def test_backfill_respects_credit_cap_and_cache(tmp_path):
    calls = []

    def get(url, params, timeout):
        calls.append(params["date"])
        return FakeResp(remaining=10_000 - 20 * len(calls))

    times = [ts("2025-09-13 11:00") + pd.Timedelta(hours=i) for i in range(5)]
    rep = oh.backfill(times, max_credits=50, api_key="SECRET", raw_dir=tmp_path, get=get)
    assert rep.fetched == 2 and rep.credits_used == 40 <= 50
    assert "max-credits" in rep.stopped

    calls.clear()
    rep = oh.backfill(times[:2], max_credits=50, api_key="SECRET", raw_dir=tmp_path, get=get)
    assert calls == [] and rep.cached == 2  # never fetched twice

    for path in (tmp_path / "odds_history" / "E0").glob("*.json"):
        text = path.read_text()
        assert "SECRET" not in text and "fanduel" not in text
    assert "SECRET" not in "\n".join(rep.lines())


def test_backfill_keeps_credits_for_live(tmp_path):
    rep = oh.backfill(
        [ts("2025-09-13 11:00") + pd.Timedelta(hours=i) for i in range(5)],
        max_credits=10_000,
        keep_credits=1500,
        api_key="k",
        raw_dir=tmp_path,
        get=lambda *a, **k: FakeResp(remaining=1515),
    )
    assert rep.fetched == 1 and "live" in rep.stopped


def test_api_errors_never_show_the_key(tmp_path):
    class Bad:
        ok, status_code, headers = False, 401, {}

    rep = oh.backfill(
        [ts("2025-09-13 11:00")],
        max_credits=100,
        api_key="SECRET",
        raw_dir=tmp_path,
        get=lambda *a, **k: Bad(),
    )
    assert rep.fetched == 0 and "401" in rep.errors[0]
    assert "SECRET" not in " ".join(rep.lines())


def test_history_and_price_at(tmp_path):
    oh.backfill(
        [ts("2025-09-13 11:00")],
        max_credits=100,
        api_key="k",
        raw_dir=tmp_path,
        get=lambda *a, **k: FakeResp(remaining=9000),
    )
    hist = oh.load_history("E0", {"Arsenal", "Leeds"}, raw_dir=tmp_path)
    assert list(hist[["home", "away"]].iloc[0]) == ["Arsenal", "Leeds"]
    k = ts("2025-09-13 14:00")
    row = oh.price_at(hist, "Arsenal", "Leeds", k, ts("2025-09-13 11:00"))
    assert row["odds_home"] == 1.4
    assert oh.price_at(hist, "Arsenal", "Leeds", k, ts("2025-09-13 10:00")) is None  # before
    assert oh.price_at(hist, "Arsenal", "Leeds", k, ts("2025-09-13 18:00")) is None  # stale
    played = pd.DataFrame(
        {"season": ["2526", "2526"], "home": ["Arsenal", "Leeds"], "away": ["Leeds", "Arsenal"]}
    )
    assert oh.coverage(played, hist) == [
        {"season": "2526", "matches": 2, "priced": 1, "share": 0.5}
    ]
