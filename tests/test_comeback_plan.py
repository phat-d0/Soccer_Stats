import pandas as pd

from soccer_stats.lab import comeback_plan as cp


def frame(rows):
    """Minimal football-data rows: home favourite at 60% by Pinnacle's close."""
    base = {
        "FTHG": 1,
        "FTAG": 1,
        "PSCH": 1.6,
        "PSCD": 4.0,
        "PSCA": 6.0,
        "PC>2.5": 1.9,
        "PC<2.5": 1.9,
    }
    return pd.DataFrame([{**base, **r} for r in rows])


def test_slots_group_trailing_favourites_by_kickoff(tmp_path, monkeypatch):
    raw = frame(
        [
            {
                "Date": "25/05/2025",
                "Time": "16:00",
                "HomeTeam": "A",
                "AwayTeam": "B",
                "HTHG": 0,
                "HTAG": 1,
            },
            {
                "Date": "25/05/2025",
                "Time": "16:00",
                "HomeTeam": "C",
                "AwayTeam": "D",
                "HTHG": 0,
                "HTAG": 2,
            },
            {
                "Date": "25/05/2025",
                "Time": "16:00",
                "HomeTeam": "E",
                "AwayTeam": "F",
                "HTHG": 1,
                "HTAG": 0,
            },
            {
                "Date": "26/05/2025",
                "Time": "20:00",
                "HomeTeam": "G",
                "AwayTeam": "H",
                "HTHG": 0,
                "HTAG": 0,
            },
        ]
    )
    path = tmp_path / "E0.csv"
    raw.to_csv(path, index=False)
    monkeypatch.setattr(cp, "download", lambda *a, **k: path)
    r = cp.season_plan("E0", 2024, tmp_path)
    assert r["trailing_favourites"] == 2 and r["one_goal_down"] == 1
    assert r["slots_to_buy"] == 1 and r["credits"] == cp.HIST_COST
    # 16:00 UK summer time = 15:00 UTC; the snapshot is requested 55 minutes later.
    assert r["snapshot_times"] == ["2025-05-25T15:55Z"]


def test_power_matches_needed():
    assert 860 <= cp.matches_needed() <= 880


def test_designs_sum_their_league_seasons():
    rows = [
        {
            "league": "E0",
            "season": "2024/25",
            "slots_to_buy": 3,
            "credits": 30,
            "trailing_favourites": 4,
            "one_goal_down": 3,
        },
        {
            "league": "SP1",
            "season": "2024/25",
            "slots_to_buy": 2,
            "credits": 20,
            "trailing_favourites": 2,
            "one_goal_down": 2,
        },
        {"league": "E0", "season": "2025/26", "error": "missing"},
    ]
    ds = {d["name"][:1]: d for d in cp.designs(rows)}
    assert ds["A"]["credits"] == 30 and ds["B"]["credits"] == 50
    assert ds["C"]["trailing_favourites"] == 6 and ds["C"]["reaches_need"] is False
