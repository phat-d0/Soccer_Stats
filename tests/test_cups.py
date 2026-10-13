"""The UEFA cups' cost estimate (cups.py): ESPN calendar, estimates and team coverage, all
on mocked HTTP. Nothing here may reach the network or The Odds API."""

import argparse
import json

import pandas as pd
import pytest
import requests

from soccer_stats import cups
from soccer_stats import leagues as lgs
from soccer_stats import odds_feed as feed
from soccer_stats.publish import league_list

TODAY = pd.Timestamp("2026-10-12 12:00", tz="UTC")


class Resp:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status

    def json(self):
        return self.body


def event(eid, kickoff, home, away, completed=True, hid=None, aid=None):
    return {
        "id": eid,
        "date": kickoff,
        "season": {"slug": "league-phase"},
        "status": {"type": {"completed": completed}},
        "competitions": [
            {
                "competitors": [
                    {"homeAway": "home", "team": {"id": hid or home, "displayName": home}},
                    {"homeAway": "away", "team": {"id": aid or away, "displayName": away}},
                ]
            }
        ],
    }


# Two Champions League matchdays (Tue/Wed 16 and 17 Sep 2025, 16:45 and 19:00 UTC), and
# one Europa League Thursday; ESPN's calendar lists those days.
UCL_DAYS = {
    "20250916": [
        event("1", "2025-09-16T16:45Z", "Athletic Club", "Arsenal"),
        event("2", "2025-09-16T19:00Z", "PSV Eindhoven", "Union St.-Gilloise"),
        event("3", "2025-09-16T19:00Z", "Real Madrid", "Marseille"),
    ],
    "20250917": [
        event("4", "2025-09-17T19:00Z", "Slavia Prague", "Bodo/Glimt"),
        event("5", "2025-09-17T19:00Z", "Olympiacos", "Pafos"),
    ],
}
UEL_DAYS = {"20250925": [event("9", "2025-09-25T19:00Z", "Lincoln Red Imps", "Celtic")]}


def fake_espn(calendar=True, log=None):
    def get(url, params=None, timeout=None):
        assert url.startswith("https://site.api.espn.com/"), url  # ESPN only, never odds
        if log is not None:
            log.append((url, dict(params or {})))
        if url.endswith("/summary"):
            stats = [{"name": "wonCorners", "displayValue": "7"}, {"name": "foulsCommitted"}]
            return Resp(
                {"boxscore": {"teams": [{"team": {"displayName": "Arsenal"}, "statistics": stats}]}}
            )
        slug = url.split("/soccer/")[1].split("/")[0]
        days = {"uefa.champions": UCL_DAYS, "uefa.europa": UEL_DAYS}.get(slug, {})
        day = params["dates"]
        cal = [f"{d[:4]}-{d[4:6]}-{d[6:]}T07:00Z" for d in sorted(days)]
        body = {"events": days.get(day, [])}
        if calendar:
            body["leagues"] = [{"calendarType": "day", "calendar": cal}]
        return Resp(body)

    return get


# ---------- the registry: three cups, off ----------


def test_cups_are_registered_with_their_odds_api_keys_and_only_ucl_is_live():
    assert feed.SPORTS["UCL"] == "soccer_uefa_champs_league"
    assert feed.SPORTS["UEL"] == "soccer_uefa_europa_league"
    assert feed.SPORTS["UECL"] == "soccer_uefa_europa_conference_league"
    for code in ("UCL", "UEL", "UECL"):
        lg = lgs.get(code)
        assert lg.cup and lg.understat is None and lg.odds_policy == "matchday"
        assert lg.live == (code == "UCL")  # the Champions League live from 13 Oct (owner)
        assert (code in lgs.live_codes()) == (code == "UCL")
    assert [c for c in lgs.live_codes() if lgs.get(c).cup] == ["UCL"]
    assert set(cups.CUPS) == {c for c, lg in lgs.LEAGUES.items() if lg.cup}


def test_an_off_cup_is_never_fetched_or_listed(tmp_path, monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: pytest.fail("should not call"))
    near = [TODAY + pd.Timedelta(hours=2)]
    for code in ("UEL", "UECL"):  # the cups still off
        events, status = feed.fetch_odds(
            code, raw_dir=tmp_path, api_key="k", now=TODAY, kickoffs=near
        )
        assert events is None and "not live" in status.error
    assert list(tmp_path.iterdir()) == []
    rows = league_list([{"league": "E0"}], {})
    assert [r["code"] for r in rows] == lgs.live_codes()  # data.json and the app unchanged


def test_estimate_credits_cli_leaves_the_cups_to_estimate_cups(monkeypatch, capsys):
    from soccer_stats import cli, xg
    from soccer_stats import data as fd

    asked = []

    def kickoffs(code, year, raw_dir=None):
        asked.append(code)
        return pd.Series(pd.date_range("2025-10-04 14:00", periods=4, freq="7D", tz="UTC"))

    def schedule(code, year, raw_dir=None, include_played=False):
        asked.append(code)
        return pd.DataFrame(
            {"kickoff": pd.date_range("2026-10-03 14:00", periods=4, freq="7D", tz="UTC")}
        )

    monkeypatch.setattr(fd, "season_kickoffs", kickoffs)
    monkeypatch.setattr(xg, "load_schedule", schedule)
    cli.cmd_estimate_credits(argparse.Namespace(month="2026-10"))
    out = capsys.readouterr().out
    assert not set(asked) & set(cups.CUPS)  # never looked for a football-data cup file
    assert "UEFA cups (Champions League, Europa League, Conference League): off" in out
    assert "Champions League " not in out.split("UEFA cups")[0]


# ---------- the ESPN calendar ----------


def test_season_calendar_follows_espns_calendar_and_caches_final_days(tmp_path):
    log = []
    start, end = cups.season_window(2025)
    cal = cups.season_calendar(
        "uefa.champions", start, end, tmp_path, fake_espn(log=log), today=TODAY
    )
    assert cal["mode"] == "calendar"
    assert [e["id"] for e in cal["events"]] == ["1", "2", "3", "4", "5"]
    assert cal["events"][0]["home"] == "Athletic Club" and cal["events"][0]["completed"]
    # 11 monthly seeds (Aug-Jun) plus the two listed days, one date per call, never a range.
    days = [p["dates"] for _, p in log]
    assert len(days) == len(set(days)) == 11 + 2
    assert all(len(d) == 8 and d.isdigit() for d in days)
    # Past days are cached: a second pass asks nothing.
    again = []
    cups.season_calendar("uefa.champions", start, end, tmp_path, fake_espn(log=again), today=TODAY)
    assert again == []


def test_season_calendar_asks_every_day_without_a_calendar(tmp_path):
    log = []
    start = pd.Timestamp("2025-09-01", tz="UTC")
    end = pd.Timestamp("2025-10-01", tz="UTC")
    cal = cups.season_calendar(
        "uefa.champions", start, end, tmp_path, fake_espn(calendar=False, log=log), today=TODAY
    )
    assert cal["mode"].startswith("every day")
    assert len(log) == 30 and len(cal["events"]) == 5


def test_an_unknown_slug_stops_after_its_seeds(tmp_path):
    log = []

    def missing(url, params=None, timeout=None):
        log.append(params["dates"])
        return Resp({}, status=404)

    start, end = cups.season_window(2025)
    cal = cups.season_calendar("uefa.nothing", start, end, tmp_path, missing, today=TODAY)
    assert cal["mode"] == "no response" and cal["events"] == [] and len(log) == 11


def test_another_seasons_calendar_falls_back_to_every_day(tmp_path):
    # ESPN may answer with the current season's calendar for a past date: no day in the
    # window, so every day is asked rather than trusting it.
    log = []

    def other(url, params=None, timeout=None):
        log.append(params["dates"])
        return Resp({"events": [], "leagues": [{"calendar": ["2026-09-16T07:00Z"]}]})

    start = pd.Timestamp("2025-09-01", tz="UTC")
    end = pd.Timestamp("2025-11-01", tz="UTC")
    cal = cups.season_calendar("uefa.champions", start, end, tmp_path, other, today=TODAY)
    assert cal["mode"].startswith("every day") and len(log) == 61
    assert cal["calendar_sample"][0] == 1


def test_recent_days_are_never_cached(tmp_path):
    day = f"{TODAY:%Y%m%d}"
    cups.scoreboard_day("uefa.champions", day, tmp_path, fake_espn(), {}, TODAY)
    assert not cups._cache_path(tmp_path, "uefa.champions", day).exists()


def test_failures_stop_the_calendar(tmp_path, monkeypatch):
    monkeypatch.setattr(cups, "PAUSE", 0)
    calls = []

    def down(url, params=None, timeout=None):
        calls.append(1)
        raise requests.ConnectionError("down")

    start = pd.Timestamp("2025-09-01", tz="UTC")
    cal = cups.season_calendar(
        "uefa.champions",
        start,
        start + pd.Timedelta(days=60),
        tmp_path,
        down,
        every_day=True,
        today=TODAY,
    )
    assert cal["events"] == [] and cal["failures"] == cups.MAX_FAILURES
    assert len(calls) == cups.MAX_FAILURES * (cups.RETRIES + 1)


def test_calendar_days_and_parse_events_tolerate_odd_shapes():
    assert cups.calendar_days(None) == []
    assert cups.calendar_days({"leagues": [{"calendar": [{"label": "week 1"}]}]}) == []
    assert cups.calendar_days({"leagues": [{"calendar": ["2025-09-16T07:00Z"]}]}) == ["20250916"]
    assert cups.parse_events({"events": [{"id": "1"}, "x"]}) == []


# ---------- the estimates ----------


def ucl_kickoffs():
    return [pd.Timestamp(e["date"]) for evs in UCL_DAYS.values() for e in evs]


def test_live_estimate_replays_the_matchday_rule_and_counts_midweek():
    start, end = cups.season_window(2025)
    ks = ucl_kickoffs()
    est = cups.live_estimate("UCL", ks, start, end)
    sep = next(r for r in est["months"] if r["month"] == "2025-09")
    direct = feed.estimate_credits(
        "UCL", ks, pd.Timestamp("2025-09-01", tz="UTC"), pd.Timestamp("2025-10-01", tz="UTC")
    )
    assert sep["calls"] == direct["calls"] > 0 and sep["credits"] == sep["calls"]  # 1 a refresh
    assert sep["matches"] == sep["matches_midweek"] == 5
    # The 48-hour window opens on Sunday 14 Sep: some refreshes fall before Tuesday.
    assert 0 < sep["calls_midweek"] < sep["calls"]
    assert set(sep["call_days"]) == {"Sun", "Mon", "Tue", "Wed"}
    assert est["season"]["credits"] == sep["credits"] and est["months_with_matches"] == 1
    assert est["typical"] == sep["credits"]


def test_estimate_credits_records_call_times_only_when_asked():
    ks = ucl_kickoffs()
    s, e = pd.Timestamp("2025-09-01", tz="UTC"), pd.Timestamp("2025-10-01", tz="UTC")
    times = []
    got = feed.estimate_credits("UCL", ks, s, e, call_times=times)
    assert len(times) == got["calls"] and times == sorted(times)
    assert feed.estimate_credits("UCL", ks, s, e) == got


def test_snapshot_estimate_is_look_plus_close_at_one_credit_a_market():
    start, end = cups.season_window(2025)
    est = cups.snapshot_estimate("UCL", ucl_kickoffs(), start, end)
    sep = next(r for r in est["months"] if r["month"] == "2025-09")
    # Five matches, a look and a close each, at three distinct times per day: the 19:00
    # matches share their runs, but each match is its own event call.
    assert sep["corners"] == sep["team_totals"] == 10
    assert sep["team_totals_with_match_total"] == 20
    assert est["season"]["corners"] == 10


def test_historical_options_price_one_snapshot_per_kickoff_slot():
    ks = ucl_kickoffs()  # 5 matches at 3 distinct times
    h = cups.historical_cost(ks)
    assert h["slots"] == 3 and h["matches"] == 5
    assert h["a_pinnacle_close"] == h["b_draftkings_close"] == 3 * cups.HIST_COST
    assert h["c_both_close_and_look"] == (3 + 3) * cups.HIST_COST * 2
    opts = cups.historical_options({2024: ks[:2], 2025: ks})
    assert opts["one_season"]["slots"] == 3  # the latest season alone
    assert opts["all_seasons"]["slots"] == 2 + 3
    assert opts["all_seasons"]["a_pinnacle_close"] == 5 * cups.HIST_COST


# ---------- team coverage on football-data ----------

FD = {
    "E0": {"Arsenal", "Nott'm Forest"},
    "SP1": {"Ath Bilbao", "Real Madrid"},
    "F1": {"Marseille"},
    "F2": {"Red Star"},
    "E2": {"Lincoln"},
    "N1": {"PSV Eindhoven"},
    "B1": {"St. Gilloise"},
    "SC0": {"Celtic"},
    "G1": {"Olympiakos"},
    "NOR": {"Bodo/Glimt"},
}


def test_match_team_uses_aliases_words_and_spelling_but_not_lookalikes():
    m = lambda n: (cups.match_team(n, FD) or {}).get("league")  # noqa: E731
    assert m("Arsenal") == "E0"
    assert m("Athletic Club") == "SP1"  # ESPN_NAMES / ALIASES
    assert m("Union St.-Gilloise") == "B1"
    assert m("Olympiacos") == "G1"
    assert m("Bodo/Glimt") == "NOR"
    assert m("Olympique de Marseille") == "F1"
    assert m("FC Celtic") == "SC0"  # filler words dropped
    assert m("Lincoln Red Imps") is None  # Gibraltar, not League One's Lincoln
    assert m("Red Star Belgrade") is None  # DENY: not F2's Red Star
    assert m("Slavia Prague") is None and m("Pafos") is None  # no football-data file


def test_denied_lookalikes_and_near_names_for_the_log():
    fd = {"G1": {"AEK", "Aris"}, "B1": {"Genk"}, "DNK": {"FC Copenhagen"}}
    assert cups.match_team("AEK Athens", fd)["name"] == "AEK"  # by words
    assert cups.match_team("AEK Larnaca", fd) is None  # Cyprus: denied
    assert cups.match_team("Racing Genk", fd)["league"] == "B1"
    assert cups.match_team("F.C. København", fd)["league"] == "DNK"
    assert cups.near_names("Aris Limassol", fd) == []  # "aris" is under five letters
    assert cups.near_names("Copenhagen Select Allstars", fd) == ["DNK FC Copenhagen"]
    cov = cups.team_coverage({"UECL": ["AEK Larnaca", "Copenhagen Select Allstars"]}, fd)
    assert cov["all"]["near"] == {"Copenhagen Select Allstars": ["DNK FC Copenhagen"]}


def test_team_coverage_splits_fitted_main_and_extra():
    teams = {"UCL": ["Arsenal", "PSV Eindhoven", "Bodo/Glimt", "Slavia Prague"]}
    cov = cups.team_coverage(teams, FD)
    u = cov["cups"]["UCL"]
    assert (u["teams"], u["fitted"], u["main"], u["extra"], u["rated"]) == (4, 1, 1, 1, 3)
    assert u["share"] == 0.75 and u["share_main"] == 0.5
    assert u["unmatched"] == ["Slavia Prague"]
    assert cov["all"]["rated"] == 3


def test_fd_teams_reads_main_and_extra_files(tmp_path):
    def fetch(url, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        if "/new/" in url:
            code = url.rsplit("/", 1)[1][:-4]
            text = "Country,League,Season,Date,Home,Away\nX,Y,2025/2026,01/08/2025,A,B\n"
            text += "X,Y,2019/2020,01/08/2019,Old,Gone\n"
            path.write_text(text if code == "AUT" else "Country,Season,Home,Away\n")
        elif "P1.csv" in url:
            path.write_text("Div,Date,HomeTeam,AwayTeam\nP1,01/08/2025,Benfica,Porto\n")
        else:
            raise requests.HTTPError("404")
        return path

    got = cups.fd_teams(tmp_path, fetch=fetch)
    assert got["teams"]["P1"] == {"Benfica", "Porto"}
    assert got["teams"]["AUT"] == {"A", "B"}  # 2019/20 rows left out
    assert "E0 2526" in got["errors"]


# ---------- the whole run, and the CLI ----------


def test_run_and_cli_print_every_figure_without_odds_calls(tmp_path, monkeypatch, capsys):
    from soccer_stats import cli

    log = []
    monkeypatch.setattr(requests, "get", fake_espn(log=log))
    monkeypatch.setattr(cups, "RAW_DIR", tmp_path)

    def fd_fetch(url, path):
        raise requests.HTTPError("offline")

    real_run = cups.run
    monkeypatch.setattr(
        cups,
        "run",
        lambda seasons, every_day=False, teams=True: real_run(
            seasons,
            raw_dir=tmp_path,
            get=fake_espn(log=log),
            every_day=every_day,
            teams=teams,
            fd_fetch=fd_fetch,
            today=TODAY,
        ),
    )
    out_json = tmp_path / "out" / "cups.json"
    cli.main(["estimate-cups", "--seasons", "2024,2025", "--json", str(out_json)])
    out = capsys.readouterr().out
    assert all(u.startswith("https://site.api.espn.com/") for u, _ in log)
    assert "UCL (uefa.champions):" in out and "historical 2 seasons" in out
    assert "ESPN summary 5: corners {'Arsenal': '7'}" in out
    line = next(ln for ln in out.splitlines() if ln.startswith("CUPS_JSON "))
    res = json.loads(line[len("CUPS_JSON ") :])
    ucl = res["cups"]["UCL"]["seasons"]["2025"]
    assert ucl["matches"] == 5 and ucl["midweek_matches"] == 5 and ucl["slots"] == 3
    assert res["cups"]["UEL"]["seasons"]["2025"]["matches"] == 1
    assert res["cups"]["UCL"]["seasons"]["2024"]["matches"] == 0
    assert res["total"]["historical_one_season"]["slots"] == 3 + 1
    assert "events_latest" not in res["cups"]["UCL"]  # the full list goes to --json only
    full = json.loads(out_json.read_text())
    assert len(full["cups"]["UCL"]["events_latest"]) == 5
    assert full["coverage"]["all"]["teams"] == 12 and full["coverage"]["all"]["rated"] == 0
