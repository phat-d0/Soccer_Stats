"""Edge research helpers on synthetic data (no network)."""

import numpy as np
import pandas as pd
import pytest

from soccer_stats.edge import books, espn, fanduel, props
from soccer_stats.edge.stats import bonferroni_level, bootstrap_mean

# ---------- stats ----------


def test_bootstrap_range_covers_mean_and_clusters_widen():
    rng = np.random.default_rng(1)
    v = rng.normal(0.1, 1, 400)
    lo, hi = bootstrap_mean(v)
    assert lo < v.mean() < hi
    # 40 clusters of 10 identical values: far less information than 400 draws.
    cl = np.repeat(np.arange(40), 10)
    w = np.repeat(rng.normal(0, 1, 40), 10)
    clo, chi = bootstrap_mean(w, clusters=cl)
    ilo, ihi = bootstrap_mean(w)
    assert chi - clo > ihi - ilo
    assert bootstrap_mean([1.0]) is None
    assert bonferroni_level(10) == pytest.approx(0.995)


# ---------- football-data books ----------


def _raw(n=60, seed=0):
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n):
        hg, ag = rng.poisson(1.5), rng.poisson(1.1)
        r = {
            "Date": (pd.Timestamp("2024-08-10") + pd.Timedelta(days=i)).strftime("%d/%m/%Y"),
            "HomeTeam": f"H{i}",
            "AwayTeam": f"A{i}",
            "FTHG": hg,
            "FTAG": ag,
            "HS": 12,
            "AS": 10,
            "HST": 4,
            "AST": 3,
        }
        for pre, mult in (("PS", 1.0), ("B365", 0.97), ("Max", 1.03), ("Avg", 0.96)):
            for c in ("", "C"):
                r[f"{pre}{c}H"], r[f"{pre}{c}D"], r[f"{pre}{c}A"] = (
                    2.2 * mult,
                    3.4 * mult,
                    3.4 * mult,
                )
        for pre in ("P", "B365", "Max", "Avg"):
            for c in ("", "C"):
                r[f"{pre}{c}>2.5"], r[f"{pre}{c}<2.5"] = 1.9, 1.95
        rows.append(r)
    return pd.DataFrame(rows)


def test_book_prices_margins_and_best_named():
    p = books.book_prices(_raw(), season="2425")
    assert p["pinnacle_close_home"].iloc[0] == pytest.approx(2.2)
    assert p["bet365_early_draw"].iloc[0] == pytest.approx(3.4 * 0.97)
    assert p["williamhill_early_home"].isna().all()  # missing book -> NaN
    # best of the named books is Pinnacle here (Bet365 is shorter; Max isn't "named")
    assert p["best_named_early_home"].iloc[0] == pytest.approx(2.2)
    m = books.margins(p).set_index(["book", "when", "market"])
    pin = 1 / 2.2 + 2 / 3.4 - 1
    assert m.loc[("pinnacle", "close", "1x2"), "margin"] == pytest.approx(pin)
    assert m.loc[("max", "early", "1x2"), "margin"] < m.loc[("avg", "early", "1x2"), "margin"]


def test_exchange_prices_are_net_of_commission():
    raw = _raw(3)
    raw["BFEH"], raw["BFED"], raw["BFEA"] = 3.0, 3.0, 3.0
    p = books.book_prices(raw)
    assert p["betfair_ex_early_home"].iloc[0] == pytest.approx(1 + 2 * 0.95)


def test_replay_settles_and_prices_clv_against_fair_close():
    p = books.add_fair_close(books.book_prices(_raw(), season="2425"))
    preds = p[["date", "home", "away"]].copy()
    preds["p_home"], preds["p_draw"], preds["p_away"] = 0.6, 0.2, 0.2  # home edge 0.32 at 2.2
    preds["p_over25"], preds["p_under25"] = 0.5, 0.5
    j = books.join(preds, p)
    bets = books.replay(j, "pinnacle", "early", threshold=0.12)
    assert len(bets) == len(p) and (bets["market"] == "home").all()
    home_won = p["home_goals"] > p["away_goals"]
    assert bets["profit"].sum() == pytest.approx(home_won.sum() * 1.2 - (~home_won).sum())
    # Entering at Pinnacle's own close: CLV = price x fair - 1 < 0 (the margin).
    fair = p["fair_home"].iloc[0]
    assert bets["clv"].iloc[0] == pytest.approx(2.2 * fair - 1)
    assert bets["clv"].iloc[0] < 0
    s = books.summarize(bets)
    assert s["bets"] == len(p) and s["roi_ci95"][0] <= s["roi"] <= s["roi_ci95"][1]
    # A bigger price at Max means a better ROI on the same bets.
    assert books.summarize(books.replay(j, "max", "early"))["roi"] > s["roi"]


def test_shots_check_lines_up_understat_with_football_data():
    p = books.book_prices(_raw(2))
    apps = []
    for r in p.itertuples():
        k = pd.Timestamp(r.date).tz_localize("UTC") + pd.Timedelta(hours=15)
        apps += [
            {"kickoff": k, "team": r.home, "home": True, "shots": 6, "sot": 2},
            {"kickoff": k, "team": r.home, "home": True, "shots": 5, "sot": 2},
            {"kickoff": k, "team": r.away, "home": False, "shots": 9, "sot": 3},
        ]
    out = books.shots_check(p, pd.DataFrame(apps))
    assert out["team_matches"] == 4
    assert out["shots_ratio"] == pytest.approx((11 + 9) / (12 + 10))
    assert out["sot_exact"] == 1.0 and out["shots_exact"] == 0.0
    assert books.shots_check(p, pd.DataFrame()) == {}


# ---------- two-sided props ----------


def _event():
    def o(name, player, point, price):
        return {"name": name, "description": player, "point": point, "price": price}

    return {
        "id": "e1",
        "home_team": "Arsenal",
        "away_team": "Chelsea",
        "bookmakers": [
            {
                "key": "fanduel",
                "markets": [
                    {
                        "key": "player_shots",
                        "outcomes": [o("Over", "Bukayo Saka", 2.0, 1.6)],
                    }
                ],
            },
            {
                "key": "betsharp",
                "markets": [
                    {
                        "key": "player_shots",
                        "outcomes": [
                            o("Over", "Bukayo Saka", 1.5, 1.95),
                            o("Under", "Bukayo Saka", 1.5, 1.95),
                            o("Over", "Cole Palmer", 2.0, 1.8),  # whole line, two-sided: push
                            o("Under", "Cole Palmer", 2.0, 2.0),
                        ],
                    },
                    {"key": "h2h", "outcomes": [o("Arsenal", None, None, 2.0)]},
                ],
            },
        ],
    }


def test_threshold_rules():
    assert props.threshold(1.5, True) == 2
    assert props.threshold(1.0, False) == 1  # FanDuel's "at least 1"
    assert props.threshold(2.0, True) is None  # would push
    assert props.threshold(float("nan"), True) is None


def test_pairs_summary_and_fanduel_against_fair():
    p = props.pairs(_event())
    sharp = p[(p["book"] == "betsharp") & (p["player"] == "Bukayo Saka")].iloc[0]
    assert sharp["k"] == 2
    assert sharp["margin"] == pytest.approx(2 / 1.95 - 1)
    assert sharp["fair_over"] == pytest.approx(0.5)
    s = props.book_summary(p).set_index("book")
    assert s.loc["betsharp", "two_sided"] == 2 and s.loc["fanduel", "two_sided"] == 0
    fv = props.fanduel_vs_fair(p)
    assert len(fv) == 1  # FanDuel's 2.0 (2+) meets the sharp book's over 1.5
    assert fv["ev"].iloc[0] == pytest.approx(1.6 * 0.5 - 1)
    assert props.pairs({}).empty and props.book_summary(props.pairs({})).empty


class _Resp:
    def __init__(self, body, cost, left=10000, status=200):
        self.body, self.status_code, self.ok = body, status, status == 200
        self.headers = {"x-requests-last": str(cost), "x-requests-remaining": str(left)}

    def json(self):
        return self.body


def test_probe_stops_at_the_cap_and_never_logs_the_key():
    calls = []
    now = pd.Timestamp("2026-10-06T12:00Z")

    def get(url, params=None, timeout=None):
        calls.append(url)
        if url.endswith("/sports/soccer_epl/events") and "/historical/" not in url:
            return _Resp(
                [
                    {
                        "id": "u1",
                        "home_team": "A",
                        "away_team": "B",
                        "commence_time": "2026-10-17T14:00:00Z",
                    }
                ],
                0,
            )
        if "/historical/" in url and url.endswith("/events"):
            return _Resp(
                {
                    "data": [
                        {
                            "id": "h1",
                            "home_team": "C",
                            "away_team": "D",
                            "commence_time": "2026-10-04T14:00:00Z",
                        }
                    ]
                },
                1,
            )
        if "/historical/" in url:
            return _Resp({"data": _event()}, 100)
        return _Resp(_event(), 10)

    res = props.probe(60, ["2026-10-03T08:00:00Z"], now=now, api_key="SECRETKEY", get=get)
    # live (10) + historical index (1) fit; the 100-credit historical call doesn't.
    assert res["spent"] == 11
    assert any("skipped" in line for line in res["log"])
    assert all("SECRETKEY" not in line for line in res["log"])
    assert [label for label, _ in res["bodies"]] == ["live"]
    res = props.probe(500, ["2026-10-03T08:00:00Z"], now=now, api_key="SECRETKEY", get=get)
    assert res["spent"] == 111 and len(res["bodies"]) == 2
    assert props.probe(100, [], api_key="")["spent"] == 0


# ---------- FanDuel slices ----------


def _lines(seed=0):
    rng = np.random.default_rng(seed)
    rows = []
    for m in range(80):
        k = pd.Timestamp("2024-09-01", tz="UTC") + pd.Timedelta(days=5 * m)
        for pl in range(6):
            for line, odds in ((1.0, 1.25), (2.0, 2.2), (3.0, 4.5)):
                p_true = {1.0: 0.75, 2.0: 0.40, 3.0: 0.18}[line]
                for kind in ("look", "close"):
                    won = int(rng.random() < p_true)
                    rows.append(
                        {
                            "kind": kind,
                            "kickoff": str(k),
                            "home": f"H{m}",
                            "away": f"A{m}",
                            "player": f"P{pl}",
                            "position": "FWD" if pl < 2 else "MID",
                            "started": True,
                            "market": "player_shots",
                            "line": line,
                            "odds": odds if kind == "look" else odds * 0.98,
                            "implied": 1 / odds,
                            "won": won,
                        }
                    )
    return pd.DataFrame(rows)


def test_fanduel_prepare_scan_and_holdout():
    d = fanduel.prepare(_lines())
    assert set(d["move"]) == {"look", "shortened"}
    assert set(d["season"]) == {"2425", "2526"}
    t, tests = fanduel.scan(d, min_lines=50)
    assert tests == len(t) and tests > 5
    assert t["roi"].is_monotonic_decreasing
    one = fanduel.slice_table(d, "line").set_index("slice")
    assert one.loc["line=1.0", "won"] == pytest.approx(d.loc[d["line"] == 1.0, "won"].mean())
    ho = fanduel.holdout(d, "2526", top=2, min_lines=50)
    assert len(ho) == 2 and all(h["holdout_lines"] > 0 for h in ho)


# ---------- ESPN shots ----------


def _summary():
    def pl(name, starter, sub, shots, sot):
        return {
            "athlete": {"displayName": name},
            "starter": starter,
            "subbedIn": sub,
            "stats": [
                {"name": "totalShots", "value": shots},
                {"name": "shotsOnTarget", "value": sot},
            ],
        }

    return {
        "rosters": [
            {
                "team": {"displayName": "Arsenal"},
                "roster": [
                    pl("Bukayo Saka", True, False, 4, 2),
                    pl("Martin Ødegaard", True, False, 2, 1),
                    pl("Unused Sub", False, False, 0, 0),
                ],
            }
        ]
    }


def test_espn_parse_join_and_compare():
    e = espn.parse_summary(_summary())
    assert list(e["player"]) == ["Bukayo Saka", "Martin Ødegaard"]  # unused sub dropped
    assert "totalShots" in espn.stat_names(_summary())
    apps = pd.DataFrame(
        {
            "match_id": ["m1", "m1"],
            "player_id": [1, 2],
            "player": ["Bukayo Saka", "Martin Odegaard"],
            "shots": [3, 2],
            "sot": [2, 1],
        }
    )
    j, unmatched = espn.join_day(apps, e)
    assert len(j) == 2 and unmatched == 0
    c = espn.compare(j)
    assert c["shots_diff"] == pytest.approx(-0.5)
    assert c["shots_exact"] == pytest.approx(0.5)
    assert c["sot_exact"] == 1.0
    assert espn.compare(pd.DataFrame())["player_matches"] == 0
