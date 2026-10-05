import json

import numpy as np
import pandas as pd
import pytest

from soccer_stats.xg import attach_xg, parse_season, with_xg


def _us_match(home, away, dt, goals=(1, 0), xg=(1.5, 0.4), played=True):
    return {
        "id": "1",
        "isResult": played,
        "h": {"id": "1", "title": home, "short_title": home[:3].upper()},
        "a": {"id": "2", "title": away, "short_title": away[:3].upper()},
        "goals": {"h": str(goals[0]), "a": str(goals[1])} if played else {"h": None, "a": None},
        "xG": {"h": str(xg[0]), "a": str(xg[1])} if played else {"h": None, "a": None},
        "datetime": dt,
    }


@pytest.fixture
def season_json():
    return {
        "dates": [
            _us_match("Manchester City", "Wolverhampton Wanderers", "2024-08-17 19:30:00"),
            _us_match("Arsenal", "Nottingham Forest", "2024-08-17 23:30:00", (2, 2), (2.9, 0.6)),
            _us_match("Chelsea", "Newcastle United", "2025-05-30 15:00:00", played=False),
        ]
    }


def test_parse_season_maps_names_and_skips_unplayed(season_json):
    df = parse_season(season_json)
    assert list(df["home"]) == ["Man City", "Arsenal"]
    assert list(df["away"]) == ["Wolves", "Nott'm Forest"]
    assert df.loc[1, "home_xg"] == pytest.approx(2.9)


def test_attach_xg_tolerates_date_shift(season_json):
    matches = pd.DataFrame(
        {
            "date": pd.to_datetime(["2024-08-17", "2024-08-18", "2024-08-25"]),
            "home": ["Man City", "Arsenal", "Man City"],
            "away": ["Wolves", "Nott'm Forest", "Wolves"],  # 3rd: same pairing, no xG row
            "home_goals": [1, 2, 0],
            "away_goals": [0, 2, 0],
        }
    )
    # Pretend the 3rd fixture is a rematch far from the xG date.
    matches.loc[2, "date"] = pd.Timestamp("2025-01-20")
    out = attach_xg(matches, parse_season(season_json))
    assert out.loc[0, "home_xg"] == pytest.approx(1.5)
    assert out.loc[1, "away_xg"] == pytest.approx(0.6)  # 1 day later in football-data
    assert np.isnan(out.loc[2, "home_xg"])


def test_with_xg_falls_back_when_download_fails(tmp_path, monkeypatch):
    import soccer_stats.xg as xg

    def boom(*a, **k):
        raise ConnectionError("blocked")

    monkeypatch.setattr(xg, "load_xg", boom)
    matches = pd.DataFrame(
        {
            "league": ["E0"],
            "season": ["2425"],
            "date": [pd.Timestamp("2024-08-17")],
            "home": ["Man City"],
            "away": ["Wolves"],
        }
    )
    out, err = with_xg(matches, raw_dir=tmp_path)
    assert "blocked" in err
    assert out["home_xg"].isna().all()


@pytest.mark.filterwarnings("ignore:Understat teams")
def test_with_xg_reads_cached_files(tmp_path, season_json):
    (tmp_path / "understat_EPL_2024.json").write_text(json.dumps(season_json))
    matches = pd.DataFrame(
        {
            "league": ["E0"],
            "season": ["2425"],
            "date": [pd.Timestamp("2024-08-17")],
            "home": ["Man City"],
            "away": ["Wolves"],
        }
    )
    out, err = with_xg(matches, raw_dir=tmp_path)
    assert err is None
    assert out.loc[0, "home_xg"] == pytest.approx(1.5)


def test_parse_schedule_keeps_only_unplayed_in_utc(season_json):
    from soccer_stats.xg import parse_schedule

    sched = parse_schedule(season_json)
    assert list(sched["home"]) == ["Chelsea"]
    assert list(sched["away"]) == ["Newcastle"]
    assert str(sched.loc[0, "kickoff"].tz) == "UTC"
