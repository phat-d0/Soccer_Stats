"""Odds-only in-play competitions (lead, 28 Oct): free events lists, in-play runs only, the
empty-pause rule and the cap."""

import json

import pandas as pd
from test_inplay_log import Resp, event, fake_get

from soccer_stats import close_runs as cr
from soccer_stats import inplay_events as ie
from soccer_stats import inplay_log as il


def T(s):
    return pd.Timestamp(s, tz="UTC")


NOW = T("2026-10-31 12:00")


def test_registry_matches_research_lists():
    from soccer_stats.lab import heavy_favourites as hf
    from soccer_stats.lab import heavy_more as hm

    assert len(ie.COMPETITIONS) == 6 + 2 + 10 + 16
    for code in hm.DOMESTIC + ("UEL", "UECL"):
        assert ie.sport_of(code) == hm.CUPS[code][2] and ie.slug_of(code) == hm.CUPS[code][1]
    for code, (_name, slug, sport) in hm.EXTRA.items():
        assert ie.sport_of(code) == sport and ie.slug_of(code) == slug
    for code, (_name, sport) in hf.OTHER.items():
        assert ie.sport_of(code) == sport
    assert ie.group_of("FAC") == "domestic_cups" and ie.group_of("ARG") == "more_extra"
    assert "UCL" not in ie.COMPETITIONS  # live already, with its model


def events_get(calls, cost="0", active=("soccer_fa_cup", "soccer_usa_mls")):
    evs = {
        "soccer_fa_cup": [
            {
                "id": "e1",
                "commence_time": "2026-11-01T15:00:00Z",
                "home_team": "A",
                "away_team": "B",
            },
            {
                "id": "e0",
                "commence_time": "2026-10-30T15:00:00Z",
                "home_team": "C",
                "away_team": "D",
            },
        ],
        "soccer_usa_mls": [
            {
                "id": "m1",
                "commence_time": "2026-10-31T23:30:00Z",
                "home_team": "X",
                "away_team": "Y",
            }
        ],
    }

    def get(url, params=None, timeout=None):
        calls.append(url)
        if url == ie.SPORTS_URL:
            return Resp(200, [{"key": k} for k in active], {"x-requests-last": "0"})
        sport = url.split("/sports/")[1].split("/")[0]
        return Resp(200, evs.get(sport, []), {"x-requests-last": cost})

    return get


def test_events_lists_are_free_throttled_and_deduplicated(tmp_path):
    calls = []
    res = ie.fetch(tmp_path, "key", now=NOW, get=events_get(calls))
    assert res["asked"] == 2 and res["inactive"] == len(ie.COMPETITIONS) - 2
    assert res["new"] == 2 and res["credits"] == 0  # the 30 Oct kickoff is too far back
    rows = [json.loads(x) for x in (tmp_path / "FAC_events_2026-11.jsonl").read_text().splitlines()]
    assert rows[0] == {
        "league": "FAC",
        "sport": "soccer_fa_cup",
        "event_id": "e1",
        "home": "A",
        "away": "B",
        "kickoff": "2026-11-01T15:00:00+00:00",
        "fetched_at": "2026-10-31T12:00:00+00:00",
    }
    assert ie.kickoffs(tmp_path) == {"FAC": [T("2026-11-01 15:00")], "USA": [T("2026-10-31 23:30")]}
    # Within EVENTS_EVERY nothing is asked again; after it, nothing is logged twice.
    calls.clear()
    assert (
        ie.fetch(tmp_path, "key", now=NOW + pd.Timedelta(hours=1), get=events_get(calls))["stop"]
        == "nothing due"
    )
    assert calls == []
    again = ie.fetch(tmp_path, "key", now=NOW + pd.Timedelta(hours=3), get=events_get(calls))
    assert again["asked"] == 2 and again["new"] == 0
    assert ie.fetch(tmp_path / "x", "", now=NOW)["stop"].startswith("no ODDS")


def test_an_events_list_that_bills_stops_the_step(tmp_path):
    res = ie.fetch(tmp_path, "key", now=NOW, get=events_get([], cost="1"))
    assert res["asked"] == 1 and "cost 1 credits" in res["stop"]


def test_events_kickoffs_get_in_play_runs_but_no_close_or_settle(tmp_path):
    (tmp_path / "E0_2026-10.jsonl").write_text(
        json.dumps({"league": "E0", "kickoff": "2026-10-31T15:00:00+00:00", "market": "h2h"}) + "\n"
    )
    ie.append(tmp_path, [{"league": "FAC", "kickoff": "2026-10-31T16:00:00+00:00"}], [], NOW)
    assert cr.kickoffs(tmp_path) == [T("2026-10-31 15:00")]
    assert cr.inplay_kickoffs(tmp_path) == [T("2026-10-31 16:00")]
    p = cr.plan(cr.kickoffs(tmp_path), NOW, inplay_ks=cr.inplay_kickoffs(tmp_path))
    kinds = {(d["kind"], d["kickoffs"][0]) for d in p["dispatch"]}
    assert ("inplay55", T("2026-10-31 16:00")) in kinds and (
        "inplay75",
        T("2026-10-31 15:00"),
    ) in kinds
    assert all(
        d["kickoffs"] == [T("2026-10-31 15:00")]
        for d in p["dispatch"]
        if d["kind"] in ("close", "settle")
    )
    # An events-only kickoff a day ahead keeps the planner going.
    later = cr.plan([], NOW, inplay_ks=[T("2026-11-01 12:00")])
    assert later["continue"] and later["next_kickoff"] == T("2026-11-01 12:00")


def test_an_odds_only_competition_is_snapshotted_with_its_sport_and_slug(tmp_path):
    ko = "2026-10-31T15:00:00+00:00"
    ie.append(tmp_path, [{"league": "FAC", "kickoff": ko}], [], NOW)
    urls = []
    get = fake_get(urls, events=[event(kickoff=ko, home="Leeds United", away="Hull City")])
    seen = []

    def espn(lg):
        seen.append(lg)
        return []

    res = il.run(tmp_path, "key", now=T("2026-10-31 15:58"), get=get, espn=espn)
    assert res["calls"] == 1 and res["rows"] == 2 and seen == ["FAC"]
    assert all("soccer_fa_cup" in u for u in urls)
    row = json.loads((tmp_path / "FAC_inplay_2026-10.jsonl").read_text().splitlines()[0])
    assert (row["league"], row["home"], row["away"]) == ("FAC", "Leeds United", "Hull City")


def test_six_empty_calls_over_two_days_pause_a_kind_for_a_week():
    def rec(day, rows=0, kinds=("inplay55",), lg="NOR"):
        return {
            "league": lg,
            "kinds": list(kinds),
            "kickoffs": [f"2026-10-{day:02d}T15:00:00+00:00"],
            "fetched_at": f"2026-10-{day:02d}T15:58:00+00:00",
            "status": 200,
            "rows": rows,
        }

    empty = [rec(d) for d in (20, 20, 21, 21, 22, 22)]
    assert il.paused(empty, "NOR", "inplay55", T("2026-10-23 12:00")) == T("2026-10-29 15:58")
    assert il.paused(empty, "NOR", "inplay55", T("2026-10-30 12:00")) is None  # a week on
    assert il.paused(empty, "NOR", "inplay75", T("2026-10-23 12:00")) is None  # per kind
    assert il.paused(empty[:-1] + [rec(22, rows=4)], "NOR", "inplay55", T("2026-10-23")) is None
    one_day = [rec(20) for _ in range(6)]
    assert il.paused(one_day, "NOR", "inplay55", T("2026-10-21")) is None  # one match day
    live = [rec(d, lg="E0") for d in (20, 20, 21, 21, 22, 22)]
    assert il.paused(live, "E0", "inplay55", T("2026-10-23")) is None  # live leagues never pause


def test_a_paused_competition_makes_no_call(tmp_path):
    ko = "2026-10-31T15:00:00+00:00"
    ie.append(tmp_path, [{"league": "NOR", "kickoff": ko}], [], NOW)
    rec = []
    for d in (24, 24, 25, 25, 26, 26):
        rec.append(
            {
                "league": "NOR",
                "kinds": ["inplay55"],
                "kickoffs": [f"2026-10-{d}T15:00:00+00:00"],
                "fetched_at": f"2026-10-{d}T15:58:00+00:00",
                "status": 200,
                "cost": 1,
                "rows": 0,
            }
        )
    (tmp_path / "inplay_calls_2026-10.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rec) + "\n"
    )
    urls = []
    res = il.run(tmp_path, "key", now=T("2026-10-31 15:58"), get=fake_get(urls), espn=lambda _: [])
    assert res["calls"] == 0 and res["paused"] == ["NOR"] and urls == []
    assert "paused NOR" in il.summary_line(res)


def test_the_monthly_cap_is_3000():
    assert il.MONTHLY_CAP == 3000


def test_a_sports_list_that_bills_stops_before_any_events_list(tmp_path):
    calls = []

    def get(url, params=None, timeout=None):
        calls.append(url)
        return Resp(200, [{"key": "soccer_fa_cup"}], {"x-requests-last": "1"})

    res = ie.fetch(tmp_path, "key", now=NOW, get=get)
    assert calls == [ie.SPORTS_URL] and res["asked"] == 0 and "cost 1" in res["stop"]


def test_only_the_events_command_gets_the_key_in_publish_data_log_step():
    from pathlib import Path

    import yaml

    wf = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "publish.yml"
    steps = yaml.safe_load(wf.read_text())["jobs"]["build"]["steps"]
    step = next(s for s in steps if s.get("name", "").startswith("Paper trades"))
    assert "ODDS_API_KEY" not in step["env"] and "EVENTS_KEY" in step["env"]
    lines = [
        x.strip() for x in step["run"].splitlines() if "EVENTS_KEY" in x or "ODDS_API_KEY" in x
    ]
    assert lines == [
        'ODDS_API_KEY="$EVENTS_KEY" uv run soccer-stats log-inplay-events '
        "--log-dir ../log/odds_log ||"
    ]
    # Off the default branch the step exits before the data-log loop.
    run = step["run"]
    assert run.index('"$GITHUB_REF_NAME" != "$DEFAULT_BRANCH"') < run.index("log-inplay-events")
