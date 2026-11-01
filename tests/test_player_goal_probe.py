"""Priced goalscorer step 0: the probe's calls, cap, cache and name matching (no network)."""

import pandas as pd

from soccer_stats import player_goal_probe as gp

NOW = pd.Timestamp("2026-11-01T12:00:00Z")


class Resp:
    def __init__(self, body, cost, ok=True):
        self.body, self.ok, self.status_code = body, ok, 200 if ok else 404
        self.headers = {"x-requests-last": str(cost), "x-requests-remaining": "4000000"}

    def json(self):
        return self.body


def offer(book, players, no=False):
    outs = []
    for name in players:
        outs.append({"name": "Yes", "description": name, "price": 4.0})
        if no:
            outs.append({"name": "No", "description": name, "price": 1.2})
    return {"key": book, "markets": [{"key": gp.MARKET, "outcomes": outs}]}


def fake_api(calls, priced_live=True):
    """Every league: two upcoming matches (the first unpriced), a Saturday list each season."""

    def get(url, params=None, timeout=None):
        calls.append((url, dict(params or {})))
        assert "apiKey" in params
        if url.endswith("/events") and "/historical/" not in url:
            return Resp(
                [
                    {
                        "id": "u1",
                        "home_team": "Arsenal",
                        "away_team": "Chelsea",
                        "commence_time": "2026-11-02T14:00:00Z",
                    },
                    {
                        "id": "u2",
                        "home_team": "Arsenal",
                        "away_team": "Chelsea",
                        "commence_time": "2026-11-02T16:30:00Z",
                    },
                ],
                0,
            )
        if "/historical/" in url and url.endswith("/events"):
            day = params["date"][:10]
            return Resp(
                {
                    "data": [
                        {
                            "id": f"h{day}b",
                            "home_team": "Arsenal",
                            "away_team": "Chelsea",
                            "commence_time": f"{day}T17:30:00Z",
                        },
                        {
                            "id": f"h{day}a",
                            "home_team": "Arsenal",
                            "away_team": "Chelsea",
                            "commence_time": f"{day}T12:30:00Z",
                        },
                        {
                            "id": "early",
                            "home_team": "X",
                            "away_team": "Y",
                            "commence_time": f"{day}T10:30:00Z",
                        },
                    ]
                },
                1,
            )
        if "/historical/" in url:
            assert params["bookmakers"].split(",")[0] == "betrivers"
            assert len(params["bookmakers"].split(",")) <= gp.MAX_BOOKS
            body = {
                "id": url.split("/")[-2],
                "bookmakers": [offer("betrivers", ["Bukayo Saka", "Cole Palmer", "Nobody X"])],
            }
            return Resp({"data": body}, 10)
        # live event odds
        if url.split("/")[-2] == "u1" or not priced_live:
            return Resp({"id": "u1", "bookmakers": []}, 0)
        return Resp({"id": "u2", "bookmakers": [offer("betrivers", ["Bukayo Saka"])]}, 5)

    return get


def squads(league, season):
    return {"Arsenal": {"1": "Bukayo Saka"}, "Chelsea": {"2": "Cole Palmer"}}


def test_probe_spends_within_the_cap_and_caches_every_body(tmp_path):
    calls = []
    res = gp.run(
        250,
        leagues=("E0", "SP1"),
        api_key="k",
        now=NOW,
        get=fake_api(calls),
        raw_dir=tmp_path,
        rosters=squads,
    )
    # Per league: live 5 (the empty first answer costs 0), 2 event lists, 4 snapshots x 10.
    assert res["spent"] == 2 * (5 + 2 + 40) <= 250
    e0 = res["leagues"]["E0"]
    assert (
        e0["live"]["event"] == "Arsenal v Chelsea" and e0["live_tried"] == ["Arsenal v Chelsea"] * 2
    )
    assert e0["books_requested"][0] == "betrivers" and len(e0["books_requested"]) == gp.MAX_BOOKS
    h = e0["history"]["2025"]
    assert h["kickoff"] == "2025-11-01T12:30:00Z"  # the first kickoff from 11:00
    assert h["entry"]["at"] == "2025-11-01T11:40:00Z" and h["close"]["at"] == "2025-11-01T12:28:00Z"
    names = h["close"]["names"]
    assert (names["matched"], names["names"]) == (2, 3) and names["unmatched"] == ["Nobody X"]
    assert h["close"]["books"][0]["two_sided"] == 0
    # Nothing outside the market is asked for, and no result is read.
    assert all(p.get("markets", gp.MARKET) == gp.MARKET for _, p in calls)
    lines = gp.summary_lines(res)
    assert lines[0].startswith("Goalscorer probe: spent 94 of 250")
    assert any("E0 2026 close (Arsenal v Chelsea): betrivers 3" in x for x in lines)

    # A rerun buys no historical snapshot or list again (only the free list and live).
    again = []
    res2 = gp.run(
        250,
        leagues=("E0", "SP1"),
        api_key="k",
        now=NOW,
        get=fake_api(again),
        raw_dir=tmp_path,
        rosters=squads,
    )
    assert not any("/historical/" in u for u, _ in again)
    assert res2["spent"] == 2 * 5
    assert res2["leagues"]["E0"]["history"]["2026"]["close"]["names"]["matched"] == 2


def test_probe_never_passes_its_cap(tmp_path):
    calls = []
    res = gp.run(
        30,
        leagues=("E0", "SP1"),
        api_key="k",
        now=NOW,
        get=fake_api(calls),
        raw_dir=tmp_path,
        rosters=squads,
    )
    assert res["spent"] <= 30
    assert any("skipped, would pass the 30-credit cap" in x for x in res["log"])


def test_no_key_fetches_nothing(tmp_path):
    calls = []
    res = gp.run(250, api_key="", get=fake_api(calls), raw_dir=tmp_path, rosters=squads)
    assert calls == [] and res["spent"] == 0


def test_books_for_and_first_match():
    assert gp.books_for(["onexbet", "betrivers"])[:3] == ["onexbet", "betrivers", "bovada"]
    assert len(gp.books_for(list("abcdefghijkl"))) == gp.MAX_BOOKS
    evs = [
        {"id": "x", "commence_time": "2025-11-01T10:59:00Z"},
        {"id": "y", "commence_time": "2025-11-02T00:00:00Z"},
    ]
    assert gp.first_match(evs, "2025-11-01") is None


def test_rosters_from_understat_lists_a_mover_at_both_clubs():
    data = {"players": [{"id": 7, "player_name": "A Player", "team_title": "Arsenal,Chelsea"}]}
    r = gp.rosters_from([data])
    assert r["Arsenal"] == {"7": "A Player"} and r["Chelsea"] == {"7": "A Player"}
