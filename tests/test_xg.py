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
    with pytest.raises(ValueError, match="without a match list"):
        check_league_json(b'{"teams": {}}')
    check_league_json(b'{"dates": []}')  # a season before its schedule is out
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


# Every club in each xG league's 2026/27 Understat file (titles from the cached files,
# odds-check task=understat, 5 Nov 2026) and football-data's name for it (as on the cards).
CURRENT_CLUBS = {
    "E0": {
        "Arsenal": "Arsenal", "Aston Villa": "Aston Villa", "Bournemouth": "Bournemouth",
        "Brentford": "Brentford", "Brighton": "Brighton", "Chelsea": "Chelsea",
        "Coventry": "Coventry", "Crystal Palace": "Crystal Palace", "Everton": "Everton",
        "Fulham": "Fulham", "Hull": "Hull", "Ipswich": "Ipswich", "Leeds": "Leeds",
        "Liverpool": "Liverpool", "Manchester City": "Man City",
        "Manchester United": "Man United", "Newcastle United": "Newcastle",
        "Nottingham Forest": "Nott'm Forest", "Sunderland": "Sunderland",
        "Tottenham": "Tottenham",
    },
    "SP1": {
        "Alaves": "Alaves", "Athletic Club": "Ath Bilbao", "Atletico Madrid": "Ath Madrid",
        "Barcelona": "Barcelona", "Celta Vigo": "Celta", "Deportivo La Coruna": "La Coruna",
        "Elche": "Elche", "Espanyol": "Espanol", "Getafe": "Getafe", "Levante": "Levante",
        "Malaga": "Malaga", "Osasuna": "Osasuna", "Racing Santander": "Santander",
        "Rayo Vallecano": "Vallecano", "Real Betis": "Betis", "Real Madrid": "Real Madrid",
        "Real Sociedad": "Sociedad", "Sevilla": "Sevilla", "Valencia": "Valencia",
        "Villarreal": "Villarreal",
    },
    "D1": {
        "Augsburg": "Augsburg", "Bayer Leverkusen": "Leverkusen",
        "Bayern Munich": "Bayern Munich", "Borussia Dortmund": "Dortmund",
        "Borussia M.Gladbach": "M'gladbach", "Eintracht Frankfurt": "Ein Frankfurt",
        "Elversberg": "Elversberg", "FC Cologne": "FC Koln", "Freiburg": "Freiburg",
        "Hamburger SV": "Hamburg", "Hoffenheim": "Hoffenheim", "Mainz 05": "Mainz",
        "Paderborn": "Paderborn", "RasenBallsport Leipzig": "RB Leipzig",
        "Schalke 04": "Schalke 04", "Union Berlin": "Union Berlin",
        "VfB Stuttgart": "Stuttgart", "Werder Bremen": "Werder Bremen",
    },
    "I1": {
        "AC Milan": "Milan", "Atalanta": "Atalanta", "Bologna": "Bologna",
        "Cagliari": "Cagliari", "Como": "Como", "Fiorentina": "Fiorentina",
        "Frosinone": "Frosinone", "Genoa": "Genoa", "Inter": "Inter", "Juventus": "Juventus",
        "Lazio": "Lazio", "Lecce": "Lecce", "Monza": "Monza", "Napoli": "Napoli",
        "Parma Calcio 1913": "Parma", "Roma": "Roma", "Sassuolo": "Sassuolo",
        "Torino": "Torino", "Udinese": "Udinese", "Venezia": "Venezia",
    },
    "F1": {
        "Angers": "Angers", "Auxerre": "Auxerre", "Brest": "Brest", "Le Havre": "Le Havre",
        "Le Mans": "Le Mans", "Lens": "Lens", "Lille": "Lille", "Lorient": "Lorient",
        "Lyon": "Lyon", "Marseille": "Marseille", "Monaco": "Monaco", "Nice": "Nice",
        "Paris FC": "Paris FC", "Paris Saint Germain": "Paris SG", "Rennes": "Rennes",
        "Strasbourg": "Strasbourg", "Toulouse": "Toulouse", "Troyes": "Troyes",
    },
}  # fmt: skip


def test_every_current_club_in_each_xg_league_maps_to_football_data():
    from soccer_stats.xg import LEAGUES, TEAM_NAMES

    assert set(CURRENT_CLUBS) == set(LEAGUES)
    for league, clubs in CURRENT_CLUBS.items():
        assert len(clubs) == (18 if league in ("D1", "F1") else 20), league
        ours = [TEAM_NAMES.get(t, t) for t in clubs]
        assert ours == list(clubs.values()), league
        assert len(set(ours)) == len(ours), league  # no two clubs onto one name
    assert TEAM_NAMES["Real Oviedo"] == "Oviedo"  # La Liga 2025/26, still in the fit window


def test_the_odds_feed_sends_racing_santander_to_football_datas_name():
    from soccer_stats.odds_feed import _team

    assert _team("Real Racing Club de Santander") == "Santander"
    assert _team("Racing Santander") == "Santander"


def test_a_stale_raw_cache_with_the_maintenance_page_recovers_from_the_good_copy(
    tmp_path, monkeypatch, season_json
):
    """The 5 Nov race: a publish restored a raw-data cache saved before the repair (its
    2026/27 file is the maintenance page) while Understat is still down. The good-copy
    folder brings the season back."""
    from soccer_stats import data
    from soccer_stats.xg import fetch_season

    raw, good = tmp_path / "raw", tmp_path / "good"
    raw.mkdir()
    good.mkdir()
    name = "understat_EPL_2026.json"
    (raw / name).write_bytes(MAINTENANCE)
    (good / name).write_text(json.dumps(season_json))
    _serve(monkeypatch, MAINTENANCE)
    monkeypatch.setattr(data, "STALE", [])
    monkeypatch.setattr("soccer_stats.xg.STALE", data.STALE)
    monkeypatch.setattr("soccer_stats.xg.current_season", lambda: 2026)

    path = fetch_season("E0", 2026, raw, good_dir=good)
    assert json.loads(path.read_text()) == season_json
    assert any("restored from the last good copy" in s for s in data.STALE)


def test_only_good_files_reach_the_good_copy_and_newer_ones_replace_it(
    tmp_path, monkeypatch, season_json
):
    import os

    from soccer_stats.data import BadResponse
    from soccer_stats.xg import fetch_season, keep_good

    raw, good = tmp_path / "raw", tmp_path / "good"
    raw.mkdir()
    name = "understat_EPL_2024.json"
    # A good download is mirrored.
    _serve(monkeypatch, json.dumps(season_json).encode())
    fetch_season("E0", 2024, raw, good_dir=good)
    assert json.loads((good / name).read_text()) == season_json
    # A bad file never is: nothing good anywhere, so the season fails and good stays empty.
    _serve(monkeypatch, MAINTENANCE)
    with pytest.raises(BadResponse):
        fetch_season("E0", 2025, raw, good_dir=good)
    assert not (good / "understat_EPL_2025.json").exists()
    assert not keep_good(raw / "understat_EPL_2025.json", good)
    # A raw file with as many results replaces the copy only when newer.
    same = {**season_json, "teams": {}}
    (raw / name).write_text(json.dumps(same))
    later = (good / name).stat().st_mtime + 60
    os.utime(raw / name, (later, later))
    assert keep_good(raw / name, good)
    assert json.loads((good / name).read_text()) == same
    os.utime(raw / name, (later - 3600, later - 3600))
    assert not keep_good(raw / name, good)
    # Fewer played results never replace the copy, however new the file.
    fewer = {"dates": season_json["dates"][:1]}
    (raw / name).write_text(json.dumps(fewer))
    os.utime(raw / name, (later + 3600, later + 3600))
    assert not keep_good(raw / name, good)
    assert json.loads((good / name).read_text()) == same


def test_the_better_file_wins_between_the_raw_cache_and_the_copy(tmp_path, season_json):
    """The review's point 2: an older good raw-data file must not beat a newer good copy;
    more played results win, then the newer file. A bad copy is never used."""
    import os

    from soccer_stats.xg import restore_good

    raw, good = tmp_path / "raw", tmp_path / "good"
    raw.mkdir()
    good.mkdir()
    name = "understat_EPL_2026.json"
    older = {"dates": season_json["dates"][:1]}  # one result
    (raw / name).write_text(json.dumps(older))
    (good / name).write_text(json.dumps(season_json))  # two results
    assert restore_good(raw / name, good)
    assert json.loads((raw / name).read_text()) == season_json
    # As many results: the newer file stays.
    mine = {**season_json, "teams": {}}
    (raw / name).write_text(json.dumps(mine))
    t = (good / name).stat().st_mtime + 60
    os.utime(raw / name, (t, t))
    assert not restore_good(raw / name, good)
    assert json.loads((raw / name).read_text()) == mine
    os.utime(raw / name, (t - 3600, t - 3600))  # now the copy is newer
    assert restore_good(raw / name, good)
    (good / name).write_bytes(MAINTENANCE)  # a bad copy is never used
    (raw / name).unlink()
    assert not restore_good(raw / name, good)


def test_an_empty_answer_never_replaces_a_file_with_results(tmp_path, monkeypatch, season_json):
    """The review's point 3: {"dates": []} is valid JSON, but with results held it is
    refused, and the kept file is used."""
    import os

    from soccer_stats import data
    from soccer_stats.xg import fetch_season

    raw, good = tmp_path / "raw", tmp_path / "good"
    raw.mkdir()
    name = "understat_EPL_2026.json"
    (raw / name).write_text(json.dumps(season_json))
    old = (raw / name).stat().st_mtime - 13 * 3600
    os.utime(raw / name, (old, old))
    _serve(monkeypatch, b'{"dates": []}')
    monkeypatch.setattr(data, "STALE", [])
    monkeypatch.setattr("soccer_stats.xg.STALE", data.STALE)
    monkeypatch.setattr("soccer_stats.xg.current_season", lambda: 2026)
    path = fetch_season("E0", 2026, raw, good_dir=good)
    assert json.loads(path.read_text()) == season_json
    assert "empty match list" in data.STALE[0]
    # With nothing held, an empty season (before its schedule is out) is accepted.
    path = fetch_season("E0", 2026, tmp_path / "fresh", good_dir=tmp_path / "good2")
    assert json.loads(path.read_text()) == {"dates": []}


def test_backfill_keeps_every_good_season_and_reports_the_rest(tmp_path, monkeypatch, season_json):
    from soccer_stats.xg import backfill

    raw, good = tmp_path / "raw", tmp_path / "good"
    raw.mkdir()
    (good).mkdir()
    (good / "understat_EPL_2017.json").write_text(json.dumps(season_json))  # kept from before

    def get(url, headers=None, timeout=None):
        return _Resp(MAINTENANCE if url.endswith("2019") else json.dumps(season_json).encode())

    from soccer_stats import data

    monkeypatch.setattr(data.requests, "get", get)
    rows = {r["season"]: r for r in backfill(["E0"], range(2017, 2020), raw, good)}
    assert rows[2017]["status"] == "kept" and rows[2018]["status"] == "ok"
    assert rows[2018]["matches"] == len(season_json["dates"])
    assert rows[2019]["status"] == "failed" and rows[2019]["error"] == "BadResponse"
    assert sorted(p.name for p in good.iterdir()) == [
        "understat_EPL_2017.json",
        "understat_EPL_2018.json",
    ]
