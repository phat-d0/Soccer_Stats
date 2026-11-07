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
    assert json.loads(saved.read_bytes())["dates"] == DATES
    assert saved.exists() and len(row["sha256"]) == 64
    none = wb.probe_one("E0", 2019, None, s)
    assert none["status"] == "no capture" and none["captures"] == 0
    assert "E0" in wb.table([row, none]) and "no capture" in wb.table([row, none])


def _m(i, xg_h="1.2", goals_h="2", result=True, home="Arsenal"):
    return {
        "id": i,
        "isResult": result,
        "h": {"title": home},
        "a": {"title": "Leicester"},
        "goals": {"h": goals_h, "a": "0"},
        "xG": {"h": xg_h, "a": "0.4"},
        "datetime": "2022-08-05 19:00:00",
    }


def test_compare_counts_matches_that_differ_and_the_largest_xg_gap():
    held = {"dates": [_m("1"), _m("2"), _m("3")]}
    same = wb.compare(held, {"dates": [_m("1", xg_h=1.2), _m("2", goals_h=2), _m("3")]})
    assert same["differ"] == 0 and same["max_xg_diff"] == 0 and same["both"] == 3
    moved = wb.compare(held, {"dates": [_m("1", xg_h="1.25"), _m("2", home="Chelsea"), _m("4")]})
    assert moved["differ"] == 2 and moved["only_held"] == 1 and moved["only_copy"] == 1
    assert moved["fields"] == {"xg_h": 1, "h": 1}
    assert moved["max_xg_diff"] == pytest.approx(0.05)


class _Copies:
    """A fake Wayback with several page copies of one season, each with its own played count."""

    def __init__(self, copies):  # {timestamp: played}
        self.copies = copies

    def get(self, url, params=None, headers=None, timeout=None):
        if url == wb.CDX:
            if "/league/" not in params["url"]:
                return _Resp(200, "[]")
            rows = [["timestamp", "original", "mimetype", "length"]]
            rows += [
                [t, "https://understat.com/league/EPL/2018", "text/html", "9"] for t in self.copies
            ]
            return _Resp(200, json.dumps(rows))
        ts = url.split("/web/")[1].split("id_")[0]
        n = self.copies[ts]
        dates = [_m(str(i), result=i < n) for i in range(4)]
        return _Resp(
            200,
            content=("<script>var datesData = JSON.parse('" + _js(dates) + "');</script>").encode(),
        )


def test_the_most_complete_copy_wins_and_the_search_stops_at_a_complete_one(monkeypatch):
    monkeypatch.setattr(wb, "PAUSE", 0)
    s = _Copies({"20190801000000": 3, "20190901000000": 2, "20190101000000": 4})
    row, data = wb.best_capture("E0", 2018, 4, s)
    assert row["timestamp"] == "20190101000000" and row["played"] == 4 and row["complete"]
    s = _Copies({"20190801000000": 4, "20190901000000": 2})
    row, _ = wb.best_capture("E0", 2018, 4, s)
    assert row["timestamp"] == "20190801000000" and len(row["tried"]) == 2  # stopped there


def test_load_writes_only_complete_copies_through_keep_good_and_logs_the_source(tmp_path):
    good = tmp_path / "good"
    data = {"dates": [_m("1"), _m("2")]}
    ok = {
        "league": "E0",
        "season": 2018,
        "status": "ok",
        "complete": True,
        "played": 2,
        "fd_matches": 2,
        "sha256": "ab" * 32,
        "source": "wayback",
        "url": "u",
        "captured": "2019-08-01",
    }
    partial = {**ok, "season": 2019, "complete": False, "played": 1}
    res = wb.load(
        [
            (ok, data),
            (partial, data),
            ({"league": "E0", "season": 2020, "status": "no capture"}, None),
        ],
        good,
        "now",
    )
    assert [r["loaded"] for r in res] == [True, False, False]
    assert res[1]["reason"].startswith("partial") and res[2]["reason"] == "no capture"
    assert (good / "understat_EPL_2018.json").exists()
    assert not (good / "understat_EPL_2019.json").exists()
    src = [json.loads(line) for line in (good / wb.SOURCES).read_text().splitlines()]
    assert src == [
        {
            **{k: ok[k] for k in ("league", "season", "source", "url", "captured", "played")},
            "file": "understat_EPL_2018.json",
            "sha256": ok["sha256"],
            "fd_matches": 2,
            "loaded_at": "now",
        }
    ]
    # A better file held (more played results) is never replaced.
    better = {"dates": [_m("1"), _m("2"), _m("3")]}
    (good / "understat_EPL_2021.json").write_text(json.dumps(better))
    again = wb.load([({**ok, "season": 2021}, data)], good, "now")
    assert (
        again[0]["loaded"] is False
        and json.loads((good / "understat_EPL_2021.json").read_text()) == better
    )
