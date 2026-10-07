"""Anytime-goalscorer probe and pilot: parsing, the hard credit cap, matching, verdict."""

import json

import numpy as np
import pandas as pd
import pytest

from soccer_stats import player_goal_odds as go
from soccer_stats.player_odds import hist_path

NOW = pd.Timestamp("2026-10-07 06:00", tz="UTC")


def body(eid, books):
    """books: {book: [(player, yes, no or None)]}; Yes/No with the player in description."""
    bms = []
    for book, rows in books.items():
        outs = []
        for player, yes, no in rows:
            outs.append({"name": "Yes", "description": player, "price": yes})
            if no is not None:
                outs.append({"name": "No", "description": player, "price": no})
        bms.append({"key": book, "markets": [{"key": go.MARKET, "outcomes": outs}]})
    return {"id": eid, "bookmakers": bms}


def test_prices_both_formats_and_summary():
    b = body(
        "e1", {"fanduel": [("Bukayo Saka", 3.0, None)], "pinnacle": [("Bukayo Saka", 3.2, 1.38)]}
    )
    b["bookmakers"].append(
        {
            "key": "onexbet",
            "markets": [{"key": go.MARKET, "outcomes": [{"name": "Kai Havertz", "price": 2.9}]}],
        }
    )
    p = go.prices(b)
    assert len(p) == 3 and set(p["book"]) == {"fanduel", "pinnacle", "onexbet"}
    assert p.loc[p["book"] == "onexbet", "player"].item() == "Kai Havertz"  # player as name
    s = {r["book"]: r for r in go.book_summary(p)}
    assert s["pinnacle"]["two_sided"] == 1
    assert s["pinnacle"]["margin_two_sided"] == pytest.approx(1 / 3.2 + 1 / 1.38 - 1, abs=1e-4)
    assert s["fanduel"]["two_sided"] == 0 and s["fanduel"]["margin_two_sided"] is None


def _cache(tmp_path, n_months=(8, 10, 12, 2, 4)):
    for m in n_months:
        y = 2025 if m >= 7 else 2026
        for d in (5, 20):  # two per month: the first is picked
            k = pd.Timestamp(f"{y}-{m:02d}-{d:02d} 15:00", tz="UTC")
            eid = f"ev{m}_{d}"
            path = hist_path("E0", eid, "close", tmp_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            ev = {
                "id": eid,
                "commence_time": k.isoformat(),
                "home_team": "Arsenal",
                "away_team": "Leeds United",
                "bookmakers": [{"key": "fanduel", "markets": []}],
            }
            path.write_text(
                json.dumps({"requested": (k - pd.Timedelta(minutes=1)).isoformat(), "event": ev})
            )


def test_pilot_events_first_cached_close_per_month(tmp_path):
    _cache(tmp_path)
    evs = go.pilot_events("E0", tmp_path)
    assert [e["kickoff"].month for e in evs] == [8, 10, 12, 2, 4]
    assert all(e["kickoff"].day == 5 for e in evs)


class Resp:
    def __init__(self, body, cost, left=20000, ok=True):
        self.body, self.ok, self.status_code = body, ok, 200 if ok else 500
        self.headers = {"x-requests-last": str(cost), "x-requests-remaining": str(left)}

    def json(self):
        return self.body


def _fake_api(calls, live_cost=5, hist_cost=10, left=20000):
    upcoming = [
        {
            "id": f"u{i}",
            "home_team": "Arsenal",
            "away_team": "Leeds United",
            "commence_time": (NOW + pd.Timedelta(days=3, hours=i)).isoformat(),
        }
        for i in range(10)
    ]

    def get(url, params, timeout):
        calls.append((url, dict(params)))
        if url.endswith("/events"):
            return Resp(upcoming, 0, left)
        if "/historical/" in url:
            return Resp(
                {"data": body("h", {"fanduel": [("Bukayo Saka", 2.8, None)]})}, hist_cost, left
            )
        eid = url.split("/events/")[1].split("/")[0]
        if eid == "u0":  # first match: nothing posted yet, costs nothing
            return Resp(body(eid, {}), 0, left)
        return Resp(
            body(
                eid,
                {
                    "fanduel": [("Bukayo Saka", 2.8, None)],
                    "betrivers": [("Bukayo Saka", 2.9, None)],
                },
            ),
            live_cost,
            left,
        )

    return get


def test_run_calls_never_passes_the_cap(tmp_path):
    _cache(tmp_path)
    evs = go.pilot_events("E0", tmp_path)
    calls = []
    res = go.run_calls(60, evs, api_key="SECRET", now=NOW, get=_fake_api(calls), raw_dir=tmp_path)
    assert res["spent"] <= 60
    assert res["spent"] == 2 * 5 + 5 * 10  # two priced live calls + five pilot calls
    live = [c for c in calls if "/odds" in c[0] and "/historical/" not in c[0]]
    assert len(live) == 3  # the empty first match, then two with prices
    assert res["books_found_live"] == ["betrivers", "fanduel"] or set(res["books_found_live"]) == {
        "betrivers",
        "fanduel",
    }
    hist = [c for c in calls if "/historical/" in c[0]]
    assert len(hist) == 5 and all(c[1]["markets"] == go.MARKET for c in hist)
    assert all(len(c[1]["bookmakers"].split(",")) <= go.MAX_BOOKS for c in hist)
    assert all("regions" not in c[1] for c in hist)  # one bookmakers= list = one region
    # A tighter cap stops early, before passing it.
    calls2 = []
    for p in tmp_path.rglob("*_goalscorer.json"):
        p.unlink()
    res2 = go.run_calls(25, evs, api_key="k", now=NOW, get=_fake_api(calls2), raw_dir=tmp_path)
    assert res2["spent"] <= 25 and any("would pass" in line for line in res2["log"])
    # Cached bodies cost nothing on a rerun; the key is never written.
    calls3 = []
    res3 = go.run_calls(
        60, evs, api_key="k", now=NOW, get=_fake_api(calls3, live_cost=0), raw_dir=tmp_path
    )
    assert not [c for c in calls3 if "/historical/" in c[0] and c[1]["date"].startswith("2025-08")]
    for p in tmp_path.rglob("*.json"):
        assert "SECRET" not in p.read_text()
    assert res3["spent"] <= 60


def test_reserve_is_kept(tmp_path):
    _cache(tmp_path)
    evs = go.pilot_events("E0", tmp_path)
    calls = []
    res = go.run_calls(
        60, evs, api_key="k", now=NOW, get=_fake_api(calls, left=3008), raw_dir=tmp_path
    )
    # 3,008 left on the key: a 5-credit live call keeps 3,000 spare, a 10-credit pilot
    # call would not, so every pilot call is skipped.
    assert res["spent"] == 10 and not [c for c in calls if "/historical/" in c[0]]


def test_no_key_spends_nothing(tmp_path):
    res = go.run_calls(60, [], api_key="", now=NOW, get=lambda *a, **k: 1 / 0)
    assert res["spent"] == 0


def _apps():
    k = pd.Timestamp("2025-08-16 14:00", tz="UTC")
    rows = []
    for team, opp, names in (
        ("Arsenal", "Leeds", ["Bukayo Saka", "Kai Havertz", "Ben White"]),
        ("Leeds", "Arsenal", ["Joel Piroe", "Ao Tanaka"]),
    ):
        for i, n in enumerate(names):
            rows.append(
                {
                    "match_id": "1",
                    "kickoff": k,
                    "team": team,
                    "opponent": opp,
                    "player_id": f"{team}{i}",
                    "player": n,
                    "started": i < 2,
                    "goals": 1 if n in ("Bukayo Saka", "Joel Piroe") else 0,
                }
            )
    return pd.DataFrame(rows), k


def test_match_players_evaluate_and_verdict():
    apps, k = _apps()
    preds = apps.assign(p_model=[0.35, 0.3, 0.05, 0.3, 0.1])
    ev = {
        "kickoff": k,
        "home": "Arsenal",
        "away": "Leeds United",
        "prices": go.prices(
            body(
                "e",
                {
                    "fanduel": [
                        ("Bukayo Saka", 2.6, None),
                        ("Kai Havertz", 3.0, None),
                        ("Joel Piroe", 3.4, None),
                        ("Nobody Here", 9.0, None),
                    ],
                    "betrivers": [
                        ("Bukayo Saka", 2.8, None),
                        ("Joel Piroe", 3.2, None),
                        ("Ao Tanaka", 7.0, None),
                        ("Kai Havertz", 3.1, None),
                    ],
                },
            )
        ),
    }
    m = go.match_players([ev], apps, preds, {"Arsenal", "Leeds"})
    assert m["matched"].sum() == 7 and not m.loc[m["player"] == "Nobody Here", "matched"].any()
    r = go.evaluate(m)
    assert r["names"] == 5 and r["names_matched"] == 4
    assert r["starters_priced"] == 4  # Saka, Havertz, Piroe, Tanaka started
    best = np.array([2.8, 3.1, 3.4, 7.0])
    assert r["implied"] == pytest.approx((1 / best).mean(), abs=1e-4)
    assert r["scored_rate"] == 0.5
    assert r["gap"] == pytest.approx((1 / best).mean() - 0.5, abs=1e-4)
    assert r["starter_coverage"]["betrivers"] == 1.0 and r["starter_coverage"]["fanduel"] == 0.75
    assert r["model"]["starters"] == 4
    v = go.verdict(go.book_summary(ev["prices"]), r)
    assert v["decision"] == "go" and v["ask_for_sample"]  # gap below 0 here, BetRivers covers all
    # Only FanDuel: kill (no second book covering half the starters).
    fd = {**r, "starter_coverage": {"fanduel": 1.0}, "gap": 0.08, "gap_range95": [0.05, 0.11]}
    v2 = go.verdict([{"book": "fanduel", "two_sided": 0, "margin_two_sided": None}], fd)
    assert v2["decision"] == "kill" and len(v2["kill_reasons"]) == 2 and not v2["ask_for_sample"]
    # A two-sided book under 6% margin is a go even with a middling gap.
    mid = {
        **r,
        "starter_coverage": {"fanduel": 1.0, "pinnacle": 0.8},
        "gap": 0.04,
        "gap_range95": [0.0, 0.08],
    }
    v3 = go.verdict([{"book": "pinnacle", "two_sided": 20, "margin_two_sided": 0.05}], mid)
    assert v3["decision"] == "go"
