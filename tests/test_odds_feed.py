import json

import numpy as np
import pandas as pd
import pytest
import requests

import soccer_stats.odds_feed as feed
from soccer_stats.odds_feed import apply_odds, fetch_odds, parse_odds


def event(home, away, when, h, d, a, over=None, under=None, book="draftkings"):
    markets = [
        {
            "key": "h2h",
            "outcomes": [
                {"name": home, "price": h},
                {"name": away, "price": a},
                {"name": "Draw", "price": d},
            ],
        }
    ]
    if over:
        markets.append(
            {
                "key": "totals",
                "outcomes": [
                    {"name": "Over", "price": over, "point": 2.5},
                    {"name": "Under", "price": under, "point": 2.5},
                    {"name": "Over", "price": 9.0, "point": 4.5},
                ],
            }
        )
    return {
        "id": "x",
        "sport_key": "soccer_epl",
        "commence_time": when,
        "home_team": home,
        "away_team": away,
        "bookmakers": [
            {"key": book, "title": "DraftKings", "last_update": when, "markets": markets}
        ],
    }


EVENTS = [
    event(
        "Manchester City",
        "Wolverhampton Wanderers",
        "2026-10-18T14:00:00Z",
        1.25,
        6.5,
        11.0,
        1.6,
        2.3,
    ),
    event("Hull City", "Arsenal", "2026-10-18T16:30:00Z", 7.0, 4.5, 1.45),
    event("Chelsea", "Everton", "2026-10-19T15:00:00Z", 1.5, 4.2, 6.0, book="fanduel"),
]


def test_parse_maps_names_and_markets():
    df = parse_odds(EVENTS, known_teams={"Man City", "Wolves", "Hull", "Arsenal"})
    assert list(df["home"]) == ["Man City", "Hull"]  # fanduel-only event skipped
    r = df.iloc[0]
    assert (r.away, r.odds_home, r.odds_draw, r.odds_away) == ("Wolves", 1.25, 6.5, 11.0)
    assert (r.odds_over25, r.odds_under25) == (1.6, 2.3)  # the 4.5 line is ignored
    assert np.isnan(df.iloc[1]["odds_over25"])
    assert df.iloc[0]["kickoff"] == pd.Timestamp("2026-10-18 14:00", tz="UTC")


def test_apply_replaces_odds_and_blanks_unpriced():
    fixtures = pd.DataFrame(
        {
            "kickoff": pd.to_datetime(["2026-10-18 14:00", "2026-10-19 15:00"], utc=True),
            "home": ["Man City", "Chelsea"],
            "away": ["Wolves", "Everton"],
            "odds_home": [1.3, 1.6],
            "odds_draw": [6.0, 4.0],
            "odds_away": [10.0, 5.5],
            "odds_over25": [1.5, 1.9],
            "odds_under25": [2.5, 1.9],
        }
    )
    out = apply_odds(fixtures, parse_odds(EVENTS, {"Man City", "Wolves"}))
    assert out.loc[0, "odds_home"] == 1.25 and out.loc[0, "odds_over25"] == 1.6
    assert out.loc[1, ["odds_home", "odds_over25"]].isna().all()  # DraftKings only


def test_no_key_means_no_request(tmp_path, monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: pytest.fail("should not call"))
    events, status = fetch_odds(raw_dir=tmp_path, api_key="")
    assert events is None and "ODDS_API_KEY" in status.error


class FakeResp:
    def __init__(self, ok=True, code=200, body="[]", left="480"):
        self.ok, self.status_code, self.text = ok, code, body
        self.headers = {"x-requests-remaining": left}


def test_fetch_caches_and_records_credits(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        requests, "get", lambda *a, **k: calls.append(k) or FakeResp(body=json.dumps(EVENTS))
    )
    events, status = fetch_odds(raw_dir=tmp_path, api_key="secret-key")
    assert len(events) == 3 and status.credits_left == 480 and status.error is None
    assert calls[0]["params"]["bookmakers"] == "draftkings"
    fetch_odds(raw_dir=tmp_path, api_key="secret-key")  # fresh cache -> no second call
    assert len(calls) == 1


def test_errors_never_leak_the_key(tmp_path, monkeypatch):
    def boom(url, params, timeout):
        raise requests.ConnectionError(f"failed {url}?apiKey={params['apiKey']}")

    monkeypatch.setattr(requests, "get", boom)
    _, status = fetch_odds(raw_dir=tmp_path, api_key="secret-key")
    assert "secret-key" not in status.error
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResp(ok=False, code=401))
    _, status = fetch_odds(raw_dir=tmp_path, api_key="secret-key")
    assert status.error == "The Odds API returned HTTP 401"


def test_low_credits_pause_within_the_month(tmp_path, monkeypatch):
    path = tmp_path / "odds_api_E0_draftkings.json"
    path.write_text("[]")
    path.with_suffix(".meta.json").write_text(
        json.dumps({"fetched_at": pd.Timestamp.now(tz="UTC").isoformat(), "credits_left": 5})
    )
    import os

    os.utime(path, (0, 0))  # stale cache
    monkeypatch.setattr(requests, "get", lambda *a, **k: pytest.fail("should pause"))
    events, status = fetch_odds(raw_dir=tmp_path, api_key="k")
    assert events == [] and "paused" in status.error


def test_with_draftkings_adds_unscheduled_matches(tmp_path, monkeypatch):
    from soccer_stats.publish import with_draftkings

    monkeypatch.setattr(feed, "RAW_DIR", tmp_path)
    monkeypatch.setattr(
        "soccer_stats.publish.fetch_odds",
        lambda league, **kw: (EVENTS, feed.OddsStatus(credits_left=400)),
    )
    fixtures = pd.DataFrame(
        {
            "kickoff": pd.to_datetime(["2026-10-18 14:00"], utc=True),
            "home": ["Man City"],
            "away": ["Wolves"],
        }
    )
    for c in ["odds_home", "odds_draw", "odds_away", "odds_over25", "odds_under25"]:
        fixtures[c] = np.nan
    out, source = with_draftkings(
        fixtures,
        "E0",
        {"Man City", "Wolves", "Hull", "Arsenal"},
        now=pd.Timestamp("2026-10-10", tz="UTC"),
    )
    assert source["name"] == "DraftKings" and source["format"] == "american"
    assert list(out["home"]) == ["Man City", "Hull"]
    assert out.loc[1, "odds_away"] == 1.45


def test_budget_interval():
    from soccer_stats.odds_feed import next_reset, refresh_interval_hours

    now = pd.Timestamp("2026-10-05 09:00", tz="UTC")
    assert next_reset(now) == pd.Timestamp("2026-11-01", tz="UTC")
    assert next_reset(now, reset_day=10) == pd.Timestamp("2026-10-10", tz="UTC")
    # 497 credits, 2 per call, 638.9 hours to go -> 238 calls -> ~2.7h apart
    assert refresh_interval_hours(497, 2, now) == pytest.approx(638.95 / 238, rel=1e-3)
    assert refresh_interval_hours(497, 3, now) > refresh_interval_hours(497, 2, now)
    assert refresh_interval_hours(21, 2, now) == float("inf")  # reserve protected
    assert refresh_interval_hours(None, None, now) == 1.0  # unknown -> find out
    late = pd.Timestamp("2026-10-31 22:00", tz="UTC")
    assert refresh_interval_hours(400, 2, late) == 1.0  # spare credits -> hourly cap


@pytest.mark.parametrize("cost", [2, 3])
def test_simulated_month_never_overspends(tmp_path, monkeypatch, cost):
    """Run the publish job every 15 minutes for a month against a fake API that charges."""
    credits = {"left": 500}

    def fake_get(url, params, timeout):
        if credits["left"] < cost:
            return FakeResp(ok=False, code=429, left="0")
        credits["left"] -= cost
        r = FakeResp(body="[]", left=str(credits["left"]))
        r.headers["x-requests-last"] = str(cost)
        return r

    monkeypatch.setattr(requests, "get", fake_get)
    t, end = pd.Timestamp("2026-10-01 00:05", tz="UTC"), pd.Timestamp("2026-10-31 23:50", tz="UTC")
    fetch_times = []
    while t <= end:
        before = credits["left"]
        _, status = fetch_odds(raw_dir=tmp_path, api_key="k", now=t)
        if credits["left"] != before:
            fetch_times.append(t)
        assert status.error is None or "paused" in status.error
        t += pd.Timedelta(minutes=15)

    spent = 500 - credits["left"]
    assert credits["left"] >= feed.RESERVE_CREDITS  # never dipped into the reserve
    assert spent >= 500 - feed.RESERVE_CREDITS - 2 * cost  # ...but used nearly all of it
    gaps = pd.Series(fetch_times).diff().dropna() / pd.Timedelta(hours=1)
    assert gaps.min() >= 1.0  # never more than hourly
    assert fetch_times[-1] > pd.Timestamp("2026-10-29", tz="UTC")  # still refreshing at month end


def test_allowance_reset_resumes_fetching(tmp_path, monkeypatch):
    path = tmp_path / "odds_api_E0_draftkings.json"
    path.write_text("[]")
    path.with_suffix(".meta.json").write_text(
        json.dumps({"fetched_at": "2026-10-30T12:00:00+00:00", "credits_left": 5, "last_cost": 2})
    )
    calls = []
    monkeypatch.setattr(requests, "get", lambda *a, **k: calls.append(1) or FakeResp(left="498"))
    _, status = fetch_odds(
        raw_dir=tmp_path, api_key="k", now=pd.Timestamp("2026-10-31 08:00", tz="UTC")
    )
    assert not calls and "paused" in status.error  # same period, <24h: stay paused
    _, status = fetch_odds(
        raw_dir=tmp_path, api_key="k", now=pd.Timestamp("2026-11-01 00:15", tz="UTC")
    )
    assert calls and status.credits_left == 498  # new month: fetch and learn the new budget


def test_paused_checks_daily_and_learns_reset_day(tmp_path, monkeypatch):
    path = tmp_path / "odds_api_E0_draftkings.json"
    path.write_text("[]")
    meta = path.with_suffix(".meta.json")
    meta.write_text(
        json.dumps({"fetched_at": "2026-11-01T06:00:00+00:00", "credits_left": 19, "last_cost": 2})
    )
    left = {"n": 17}

    def get(*a, **k):
        r = FakeResp(left=str(left["n"]))
        r.headers["x-requests-last"] = "2"
        return r

    calls = []
    monkeypatch.setattr(requests, "get", lambda *a, **k: calls.append(1) or get())
    monkeypatch.setenv("ODDS_API_RESET_DAY", "1")
    # Paused (below reserve, reset assumed on the 1st already passed): only a daily check.
    fetch_odds(raw_dir=tmp_path, api_key="k", now=pd.Timestamp("2026-11-01 18:00", tz="UTC"))
    assert not calls
    fetch_odds(raw_dir=tmp_path, api_key="k", now=pd.Timestamp("2026-11-02 06:30", tz="UTC"))
    assert len(calls) == 1 and json.loads(meta.read_text())["reset_day"] is None  # 17 < 19
    # The real reset happens on the 5th: the daily check sees credits jump and learns it.
    left["n"] = 498
    fetch_odds(raw_dir=tmp_path, api_key="k", now=pd.Timestamp("2026-11-05 07:00", tz="UTC"))
    saved = json.loads(meta.read_text())
    assert saved["credits_left"] == 498 and saved["reset_day"] == 5
    from soccer_stats.odds_feed import next_reset

    assert next_reset(
        pd.Timestamp("2026-11-05 07:00", tz="UTC"), saved["reset_day"]
    ) == pd.Timestamp("2026-12-05", tz="UTC")


def test_with_draftkings_never_duplicates_a_scheduled_match(tmp_path, monkeypatch):
    """A priced match under another spelling joins its scheduled card; a match with
    unknown names is listed as unmatched instead of becoming a second card (8 Oct bug:
    36 duplicate cards across La Liga, Bundesliga, Serie A and Ligue 1)."""
    from soccer_stats.publish import duplicate_fixtures, with_draftkings

    events = [
        event("Borussia Dortmund", "Werder Bremen", "2026-10-09T18:30:00Z", 1.6, 4.2, 5.0),
        event("Atlético Madrid", "Alavés", "2026-10-10T14:15:00Z", 1.5, 4.0, 7.0),
        event("Union Berlin", "Eintracht Frankfurt am Main", "2026-10-10T13:30:00Z", 3, 3.4, 2.3),
        event("Nowhere United", "Elsewhere Town", "2026-10-10T16:30:00Z", 2, 3, 4),
    ]
    monkeypatch.setattr(feed, "RAW_DIR", tmp_path)
    monkeypatch.setattr(
        "soccer_stats.publish.fetch_odds",
        lambda league, **kw: (events, feed.OddsStatus(credits_left=4000)),
    )
    fixtures = pd.DataFrame(
        {
            "kickoff": pd.to_datetime(
                ["2026-10-09 18:30", "2026-10-10 14:15", "2026-10-10 13:30"], utc=True
            ),
            "home": ["Dortmund", "Ath Madrid", "Union Berlin"],
            "away": ["Werder Bremen", "Alaves", "Ein Frankfurt"],
        }
    )
    for c in ["odds_home", "odds_draw", "odds_away", "odds_over25", "odds_under25"]:
        fixtures[c] = np.nan
    known = {"Dortmund", "Werder Bremen", "Ath Madrid", "Alaves", "Union Berlin", "Ein Frankfurt"}
    out, source = with_draftkings(
        fixtures, "D1", known, now=pd.Timestamp("2026-10-08 22:00", tz="UTC")
    )
    assert len(out) == 3 and out["odds_home"].notna().all()  # every scheduled card priced
    cards = [
        {"league": "D1", "home": h, "away": a, "kickoff": str(k)}
        for h, a, k in zip(out["home"], out["away"], out["kickoff"], strict=True)
    ]
    assert duplicate_fixtures(cards) == []
    assert source["unmatched"] == ["Nowhere United v Elsewhere Town (2026-10-10 16:30)"]


def test_duplicate_fixtures_flags_a_repeated_card():
    from soccer_stats.publish import duplicate_fixtures

    card = {"league": "SP1", "home": "Betis", "away": "Osasuna", "kickoff": "k"}
    assert duplicate_fixtures([card, {**card, "league": "E0"}]) == []
    assert duplicate_fixtures([card, dict(card)]) == [("SP1", "Betis", "Osasuna", "k")]


def test_web_fixture_has_no_duplicate_cards():
    from pathlib import Path

    from soccer_stats.publish import duplicate_fixtures

    path = Path(__file__).parent / "fixtures" / "web" / "data.json"
    assert duplicate_fixtures(json.loads(path.read_text())["fixtures"]) == []


def _simulate_month(tmp_path, monkeypatch, cost, params_seen=None):
    """Publish every 15 minutes for October against a fake API charging `cost` a call."""
    credits = {"left": 500}

    def fake_get(url, params, timeout):
        if params_seen is not None:
            params_seen.append(params)
        credits["left"] -= cost
        r = FakeResp(body="[]", left=str(credits["left"]))
        r.headers["x-requests-last"] = str(cost)
        return r

    monkeypatch.setattr(requests, "get", fake_get)
    t, end = pd.Timestamp("2026-10-01 00:05", tz="UTC"), pd.Timestamp("2026-10-31 23:50", tz="UTC")
    times = []
    while t <= end:
        before = credits["left"]
        fetch_odds(raw_dir=tmp_path, api_key="k", now=t)
        if credits["left"] != before:
            times.append(t)
        t += pd.Timedelta(minutes=15)
    return times, 500 - credits["left"]


def test_request_asks_only_for_h2h(tmp_path, monkeypatch):
    seen = []
    _simulate_month(tmp_path, monkeypatch, 1, seen)
    assert seen and all(p["markets"] == "h2h" for p in seen)
    assert all(p["bookmakers"] == "draftkings" for p in seen)


def test_cheaper_call_does_not_refresh_more_often(tmp_path, monkeypatch):
    """Dropping totals halves the price, but at any balance the budget still counts each
    call at BUDGET_COST, so the interval is what the two-credit call gave."""
    now = pd.Timestamp("2026-10-05 09:00", tz="UTC")
    for credits in (60, 497, 3_000, 4_000, 22_000):
        for share, reserve in ((1, feed.RESERVE_CREDITS), (6, feed.MATCHDAY_RESERVE_CREDITS)):
            kw = {"share": share, "reserve": reserve}
            assert feed.refresh_interval_hours(credits, 1, now, **kw) == (
                feed.refresh_interval_hours(credits, 2, now, **kw)
            )
    # A whole month at 1 credit a call never goes past the hourly floor or the reserve.
    times, spent = _simulate_month(tmp_path, monkeypatch, 1)
    gaps = pd.Series(times).diff().dropna() / pd.Timedelta(hours=1)
    assert gaps.min() >= 1.0 and spent <= 500 - feed.RESERVE_CREDITS


def test_parse_still_reads_a_totals_market():
    ev = {
        "home_team": "Arsenal",
        "away_team": "Chelsea",
        "commence_time": "2026-10-10T14:00:00Z",
        "bookmakers": [
            {
                "key": "draftkings",
                "markets": [
                    {"key": "h2h", "outcomes": [{"name": "Arsenal", "price": 2.0}]},
                    {
                        "key": "totals",
                        "outcomes": [
                            {"name": "Over", "point": 2.5, "price": 1.9},
                            {"name": "Under", "point": 2.5, "price": 1.95},
                        ],
                    },
                ],
            }
        ],
    }
    row = feed.parse_odds([ev]).iloc[0]
    assert row["odds_home"] == 2.0 and row["odds_over25"] == 1.9 and row["odds_under25"] == 1.95
    ev["bookmakers"][0]["markets"].pop()  # h2h only, as requested now
    row = feed.parse_odds([ev]).iloc[0]
    assert row["odds_home"] == 2.0 and pd.isna(row["odds_over25"])
