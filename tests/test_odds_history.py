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


def snap_body(books=("draftkings", "pinnacle", "fanduel")):
    ev = FakeResp(0).json()["data"][0]
    market = ev["bookmakers"][0]["markets"]
    return {
        "timestamp": "2025-09-13T10:55:00Z",
        "data": [{**ev, "bookmakers": [{"key": b, "markets": market} for b in books]}],
    }


class Resp:
    def __init__(self, body, cost=10, remaining=9000):
        self.ok, self.status_code, self._body = True, 200, body
        self.headers = {"x-requests-remaining": str(remaining), "x-requests-last": str(cost)}

    def json(self):
        return self._body


def test_ten_book_backfill_keeps_every_asked_book_and_counts_coverage(tmp_path):
    asked = []

    def get(url, params, timeout):
        asked.append(params)
        return Resp(snap_body(("draftkings", "pinnacle", "fanduel", "unasked_book")))

    times = [ts("2025-09-13 11:00"), ts("2025-09-13 13:55")]
    rep = oh.backfill(
        times,
        max_credits=100,
        api_key="k",
        raw_dir=tmp_path,
        get=get,
        books=oh.TEN_BOOKS,
        markets="h2h",
        cost_per_snapshot=oh.TEN_BOOKS_COST,
    )
    assert rep.fetched == 2 and rep.credits_used == 20
    assert asked[0]["bookmakers"] == ",".join(oh.TEN_BOOKS) and asked[0]["markets"] == "h2h"
    saved = (tmp_path / "odds_history" / "E0").glob("*.json")
    text = next(saved).read_text()
    assert "pinnacle" in text and "fanduel" in text and "unasked_book" not in text
    assert rep.by_season == {"2526": {"snapshots": 2, "events": 2, "draftkings": 2, "pinnacle": 2}}
    assert any("DraftKings 2 (100%), Pinnacle 2 (100%)" in line for line in rep.lines())
    # The backtest still reads DraftKings' prices from the ten-book bodies.
    hist = oh.load_history("E0", {"Arsenal", "Leeds"}, raw_dir=tmp_path)
    assert hist["odds_home"].iloc[0] == 1.4


def test_guard_stops_a_season_without_draftkings_and_moves_on(tmp_path):
    """Design C's guard: 2019/20's first three snapshots hold no DraftKings price, so
    the rest of 2019/20 is skipped; 2020/21 is bought as usual."""
    asked = []

    def get(url, params, timeout):
        asked.append(params["date"])
        dk = params["date"].startswith("2020-09")
        return Resp(snap_body(("draftkings", "pinnacle") if dk else ("pinnacle",)))

    old = [ts("2019-09-14 11:00") + pd.Timedelta(days=7 * i) for i in range(6)]
    new = [ts("2020-09-12 11:00") + pd.Timedelta(days=i) for i in range(2)]
    rep = oh.backfill(
        old + new,
        max_credits=1000,
        api_key="k",
        raw_dir=tmp_path,
        get=get,
        books=oh.TEN_BOOKS,
        markets="h2h",
        cost_per_snapshot=10,
        guard_first=oh.GUARD_FIRST,
    )
    assert len(asked) == 5 and rep.fetched == 5 and rep.credits_used == 50
    assert set(rep.guarded) == {"1920"} and rep.skipped == 3
    assert rep.by_season["1920"]["draftkings"] == 0 and rep.by_season["2021"]["draftkings"] == 2
    assert "guarded" in "\n".join(rep.lines())
    # A rerun counts the cached snapshots toward the guard and buys nothing for 2019/20.
    asked.clear()
    again = oh.backfill(
        old + new, max_credits=1000, api_key="k", raw_dir=tmp_path, get=get, guard_first=3
    )
    assert asked == [] and again.skipped == 3


def test_a_snapshot_billed_less_never_loosens_the_cap(tmp_path):
    """A cheap answer (an emptier snapshot) mustn't make the next call look cheap: each
    call is budgeted at the planned cost at least."""
    costs = iter([5, 10, 10])
    times = [ts("2025-09-13 11:00") + pd.Timedelta(hours=i) for i in range(3)]
    rep = oh.backfill(
        times,
        max_credits=12,
        api_key="k",
        raw_dir=tmp_path,
        get=lambda *a, **k: Resp({"timestamp": None, "data": []}, cost=next(costs)),
        cost_per_snapshot=10,
    )
    assert rep.fetched == 1 and rep.credits_used == 5 and "max-credits" in rep.stopped


def test_a_call_billed_over_the_limit_stops_the_run(tmp_path):
    times = [ts("2025-09-13 11:00") + pd.Timedelta(hours=i) for i in range(3)]
    rep = oh.backfill(
        times,
        max_credits=1000,
        api_key="k",
        raw_dir=tmp_path,
        get=lambda *a, **k: Resp(snap_body(), cost=20),
        cost_per_snapshot=10,
        max_call_cost=10,
    )
    assert rep.fetched == 1 and rep.credits_used == 20 and "over 10" in rep.stopped
