"""The Wayback Machine probe for the missing Understat seasons (lead, 7 Nov): parsing only,
no network."""

import json

import pytest

from soccer_stats import xg_wayback as wb
from soccer_stats.xg import check_league_json

DATES = [
    {"id": "1", "isResult": True, "h": {"title": "Arsenal"}, "a": {"title": "Leicester"}},
    {"id": "2", "isResult": False, "h": {"title": "Chelsea"}, "a": {"title": "Burnley"}},
]


def _js(obj) -> str:
    """Understat's page encoding: the JSON's UTF-8 bytes as \\xHH escapes."""
    return "".join(f"\\x{b:02X}" for b in json.dumps(obj).encode())


def test_the_json_endpoint_is_read_as_is():
    body = json.dumps({"dates": DATES, "teams": {}, "players": []}).encode()
    data = wb.parse(body)
    assert data["dates"] == DATES and wb.played(data) == 1


def test_the_old_league_page_embedded_data_is_decoded_into_the_fetchers_shape():
    teams = {"83": {"id": "83", "title": "Atlético Madrid"}}  # non-ASCII survives
    html = (
        "<html><script>var datesData\t= JSON.parse('" + _js(DATES) + "');\n"
        "var teamsData = JSON.parse('" + _js(teams) + "');\n"
        "var playersData = JSON.parse('" + _js([{"id": "1"}]) + "');</script></html>"
    ).encode()
    data = wb.parse(html)
    assert data["dates"] == DATES and data["teams"] == teams and data["players"] == [{"id": "1"}]
    check_league_json(json.dumps(data).encode())  # the same validation as a fresh download


def test_a_page_without_data_and_the_maintenance_page_fail():
    with pytest.raises(ValueError):
        wb.parse(b"<html><body>We will come back soon</body></html>")


def test_only_the_missing_seasons_are_wanted():
    assert list(wb.WANTED["E0"]) == [2017, 2018, 2019, 2020, 2021]
    assert all(list(wb.WANTED[c]) == list(range(2017, 2024)) for c in ("SP1", "D1", "I1", "F1"))
    assert sum(len(v) for v in wb.WANTED.values()) == 33


class _Resp:
    def __init__(self, status, text="", content=b""):
        self.status_code, self.text, self.content = status, text, content

    def json(self):
        return json.loads(self.text)


class _Session:
    """A fake Wayback: CDX lists one page capture after the season, the raw fetch serves it."""

    def __init__(self, page):
        self.page, self.calls = page, []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params))
        if url == wb.CDX:
            if "league/EPL/2018" in params["url"]:
                rows = [["timestamp", "original", "mimetype", "length"]]
                rows.append(
                    ["20190801120000", "https://understat.com/league/EPL/2018", "text/html", "9"]
                )
                return _Resp(200, json.dumps(rows))
            return _Resp(200, "[]")
        return _Resp(200, content=self.page)


def test_probe_one_reports_the_capture_and_writes_only_to_out(tmp_path, monkeypatch):
    monkeypatch.setattr(wb, "PAUSE", 0)
    monkeypatch.setattr(wb, "fd_matches", lambda league, year: 1)
    page = ("<script>var datesData = JSON.parse('" + _js(DATES) + "');</script>").encode()
    s = _Session(page)
    row = wb.probe_one("E0", 2018, tmp_path / "out", s)
    assert row["status"] == "ok" and row["kind"] == "page" and row["source"] == "wayback"
    assert row["captured"] == "2019-08-01" and row["after_season"] is True
    assert row["matches"] == 2 and row["played"] == 1 and row["complete"] is True
    assert row["json_captures"] == 0 and row["page_captures"] == 1
    saved = tmp_path / "out" / "understat_EPL_2018.json"
    assert saved.exists() and len(row["sha256"]) == 64
    none = wb.probe_one("E0", 2019, None, s)
    assert none["status"] == "no capture" and none["captures"] == 0
    assert "E0" in wb.table([row, none]) and "no capture" in wb.table([row, none])
