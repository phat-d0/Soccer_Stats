"""Wayback Machine coverage of Understat match files: counts only (no network)."""

import json

from soccer_stats import player_wayback as pw
from soccer_stats.xg import LEAGUES


class Resp:
    def __init__(self, text, status=200):
        self.text, self.status_code = text, status


def test_counts_played_matches_with_an_archived_api_answer_or_page(tmp_path, monkeypatch):
    dates = [
        {"id": "1", "isResult": True},
        {"id": "2", "isResult": True},
        {"id": "3", "isResult": True},
        {"id": "4", "isResult": False},  # not played: never counted
    ]
    (tmp_path / f"understat_{LEAGUES['SP1']}_2024.json").write_text(json.dumps({"dates": dates}))
    seen = []

    def fake_get(url, params=None, session=None):
        seen.append(params)
        if params.get("showNumPages"):
            return Resp("1\n")
        if "getMatchData" in params["url"]:
            return Resp(
                "https://understat.com/getMatchData/1\nhttps://understat.com/getMatchData/4\n"
            )
        return Resp("http://understat.com:80/match/2\nhttps://understat.com/match/99\n")

    monkeypatch.setattr(pw, "_get", fake_get)
    rep = pw.report(tmp_path, leagues=("SP1",), years=(2024, 2025))
    assert rep["rows"] == [
        {"league": "SP1", "season": "2024/25", "played": 3, "api": 1, "page": 1, "either": 2}
    ]
    assert rep["league_files_missing"] == ["SP1 2025"]
    assert rep["share"] == round(2 / 3, 4) and rep["captured"] == {"api": 2, "page": 2}
    assert all(p["matchType"] == "prefix" and p["filter"] == "statuscode:200" for p in seen)
