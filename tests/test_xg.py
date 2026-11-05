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


MAINTENANCE = b"<!DOCTYPE html>\n<html><head><title>Understat.com</title></head>" + (
    b"<body><p>We will come back soon</p></body></html>"
)


class _Resp:
    def __init__(self, body, status=200):
        self.content = body
        self.status_code = status

    def raise_for_status(self):
        pass


def _serve(monkeypatch, body):
    import soccer_stats.data as data

    calls = []

    def get(url, headers=None, timeout=None):
        calls.append(url)
        return _Resp(body)

    monkeypatch.setattr(data.requests, "get", get)
    return calls


def test_check_league_json_rejects_html_and_empty():
    from soccer_stats.xg import check_league_json

    with pytest.raises(ValueError, match="HTML"):
        check_league_json(MAINTENANCE)
    with pytest.raises(ValueError):
        check_league_json(b"")
    with pytest.raises(ValueError, match="without any matches"):
        check_league_json(b'{"dates": []}')
    check_league_json(json.dumps({"dates": [{"id": "1"}]}).encode())


def test_maintenance_page_never_replaces_a_good_cached_file(tmp_path, monkeypatch, season_json):
    import os

    from soccer_stats import data
    from soccer_stats.xg import fetch_season

    path = tmp_path / "understat_EPL_2026.json"
    good = json.dumps(season_json)
    path.write_text(good)
    old = path.stat().st_mtime - 13 * 3600  # older than the live season's 12 h
    os.utime(path, (old, old))
    calls = _serve(monkeypatch, MAINTENANCE)
    monkeypatch.setattr(data, "STALE", [])
    monkeypatch.setattr("soccer_stats.xg.STALE", data.STALE)
    monkeypatch.setattr("soccer_stats.xg.current_season", lambda: 2026)

    assert fetch_season("E0", 2026, tmp_path) == path
    assert calls  # it did try to refresh
    assert path.read_text() == good  # and kept the good file
    assert "understat_EPL_2026.json" in data.STALE[0]


def test_bad_cached_file_is_refetched_and_never_rewritten(tmp_path, monkeypatch, season_json):
    from soccer_stats.data import BadResponse
    from soccer_stats.xg import fetch_season

    path = tmp_path / "understat_EPL_2024.json"  # a finished season: normally cached for good
    path.write_bytes(MAINTENANCE)
    calls = _serve(monkeypatch, MAINTENANCE)
    with pytest.raises(BadResponse, match="HTML page"):
        fetch_season("E0", 2024, tmp_path)
    assert calls  # the bad copy didn't count as cached
    assert path.read_bytes() == MAINTENANCE

    calls = _serve(monkeypatch, json.dumps(season_json).encode())
    fetch_season("E0", 2024, tmp_path)
    assert json.loads(path.read_text()) == season_json  # healed once Understat is back


@pytest.mark.filterwarnings("ignore:Understat teams")
def test_one_bad_season_keeps_the_others_xg(tmp_path, monkeypatch, season_json):
    (tmp_path / "understat_EPL_2024.json").write_text(json.dumps(season_json))
    (tmp_path / "understat_EPL_2025.json").write_bytes(MAINTENANCE)
    _serve(monkeypatch, MAINTENANCE)
    matches = pd.DataFrame(
        {
            "league": ["E0", "E0"],
            "season": ["2425", "2526"],
            "date": [pd.Timestamp("2024-08-17"), pd.Timestamp("2025-08-17")],
            "home": ["Man City", "Man City"],
            "away": ["Wolves", "Wolves"],
        }
    )
    out, err = with_xg(matches, raw_dir=tmp_path)
    assert out.loc[0, "home_xg"] == pytest.approx(1.5)  # 2024/25 kept
    assert np.isnan(out.loc[1, "home_xg"])
    assert "E0 2025/26" in err and "HTML page" in err


def test_xg_line_says_plainly_when_xg_is_missing():
    from soccer_stats.publish import xg_line

    none = xg_line("E0", {"coverage": 0.0, "season_coverage": 0.0, "error": "down"})
    assert none.startswith("WARNING: no xG, goals-only model.") and none.endswith("down")
    part = xg_line("SP1", {"coverage": 0.8, "season_coverage": 0.0, "error": None})
    assert part.startswith("WARNING: xG missing for this season")
    ok = xg_line("D1", {"coverage": 0.99, "season_coverage": 0.97, "error": None})
    assert not ok.startswith("WARNING")
    assert xg_line("E1", None) is None


def test_repair_copies_only_good_old_files_over_bad_current_ones(tmp_path, season_json):
    from soccer_stats.xg import repair_from

    old, cur = tmp_path / "old", tmp_path / "cur"
    old.mkdir()
    cur.mkdir()
    good = json.dumps(season_json)
    (old / "understat_EPL_2026.json").write_text(good)  # current is the maintenance page
    (cur / "understat_EPL_2026.json").write_bytes(MAINTENANCE)
    (old / "understat_La_Liga_2026.json").write_text(good)  # current is good: kept
    (cur / "understat_La_Liga_2026.json").write_text(json.dumps({"dates": [{"id": "9"}]}))
    (old / "understat_Serie_A_2026.json").write_bytes(MAINTENANCE)  # old is bad too
    (cur / "understat_Serie_A_2026.json").write_bytes(MAINTENANCE)
    (old / "understat_Ligue_1_2026.json").write_text(good)  # current missing

    rows = {r["file"]: r for r in repair_from(old, cur)}
    assert rows["understat_EPL_2026.json"]["action"] == "copied"
    assert (cur / "understat_EPL_2026.json").read_text() == good
    assert rows["understat_La_Liga_2026.json"] == {
        "file": "understat_La_Liga_2026.json",
        "action": "kept_current",
        "matches": 1,
    }
    assert rows["understat_Serie_A_2026.json"]["action"] == "old_bad"
    assert rows["understat_Ligue_1_2026.json"]["action"] == "copied"
