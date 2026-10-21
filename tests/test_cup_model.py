"""The Champions League's cross-league model and live switch-on (cup_model.py; owner, 13 Oct
2026). Everything runs on synthetic matches and mocked ESPN / The Odds API: nothing here
may reach the network."""

import json

import numpy as np
import pandas as pd
import pytest
import requests

from soccer_stats import cup_model as cm
from soccer_stats import cups
from soccer_stats import leagues as lgs
from soccer_stats import match_calibration as mc
from soccer_stats.models import DixonColes

NOW = pd.Timestamp("2026-10-13 12:00", tz="UTC")

# Three football-data leagues and their clubs (football-data spellings), with strengths.
CLUBS = {
    "E0": {"Arsenal": 0.5, "Chelsea": 0.4, "Leeds": -0.1, "Burnley": -0.2},
    "P1": {"Benfica": 0.3, "Porto": 0.25, "Braga": -0.1, "Arouca": -0.4},
    "SC0": {"Celtic": 0.1, "Rangers": 0.05, "Hearts": -0.4, "Hibernian": -0.45},
}
LEVEL = {"E0": 0.4, "P1": 0.0, "SC0": -0.3}
# ESPN's names for the clubs that play in Europe (cups.match_team maps them back).
ESPN = {
    "Arsenal": ("E0", "Arsenal"),
    "Chelsea": ("E0", "Chelsea"),
    "Benfica": ("P1", "Benfica"),
    "FC Porto": ("P1", "Porto"),
    "Celtic": ("SC0", "Celtic"),
    "Rangers": ("SC0", "Rangers"),
}
UNCOVERED = "Qarabag"  # no football-data file has it


def strength(code, club):
    return LEVEL[code] + CLUBS[code][club]


def domestic_frame(seed=0, start="2024-08-03", end="2026-10-11"):
    rng = np.random.default_rng(seed)
    rows = []
    for d in pd.date_range(start, end, freq="7D"):
        for code, clubs in CLUBS.items():
            names = list(clubs)
            perm = rng.permutation(names)
            for h, a in ((perm[0], perm[1]), (perm[2], perm[3])):
                lam = np.exp(0.2 + clubs[h] - clubs[a])
                mu = np.exp(0.0 + clubs[a] - clubs[h])
                rows.append(
                    {
                        "league": code,
                        "date": d,
                        "home": h,
                        "away": a,
                        "home_goals": int(rng.poisson(lam)),
                        "away_goals": int(rng.poisson(mu)),
                    }
                )
    return pd.DataFrame(rows)


def espn_event(eid, kickoff, home, away, score=None, completed=True, **extra):
    comp = {
        "competitors": [
            {"homeAway": "home", "team": {"id": f"id-{home}", "displayName": home}},
            {"homeAway": "away", "team": {"id": f"id-{away}", "displayName": away}},
        ]
    }
    if score is not None:
        comp["competitors"][0]["score"] = str(score[0])
        comp["competitors"][1]["score"] = str(score[1])
    if extra.get("neutral"):
        comp["neutralSite"] = True
    if extra.get("linescores"):
        for c, ls in zip(comp["competitors"], extra["linescores"], strict=True):
            c["linescores"] = [{"value": v} for v in ls]
    status = {"type": {"completed": completed, "name": extra.get("status", "STATUS_FULL_TIME")}}
    if extra.get("period"):
        status["period"] = extra["period"]
    return {
        "id": str(eid),
        "date": pd.Timestamp(kickoff).isoformat(),
        "season": {"slug": extra.get("stage", "league-phase")},
        "status": status,
        "competitions": [comp],
    }


def european_schedule(seed=1, until=NOW, upcoming=()):
    """Champions League matches every second Tuesday from Sep 2024, cross-league pairs
    (one with the uncovered club), plus `upcoming` fixtures (not played)."""
    rng = np.random.default_rng(seed)
    names = list(ESPN)
    out = {}
    eid = 1000
    for d in pd.date_range(pd.Timestamp("2024-09-17 19:00", tz="UTC"), until, freq="14D"):
        perm = rng.permutation(names)
        games = [(perm[0], perm[1]), (perm[2], perm[3]), (perm[4], UNCOVERED)]
        for h, a in games:
            sh = strength(*ESPN[h]) if h in ESPN else -0.6
            sa = strength(*ESPN[a]) if a in ESPN else -0.6
            sc = (int(rng.poisson(np.exp(0.2 + sh - sa))), int(rng.poisson(np.exp(sa - sh))))
            out.setdefault(f"{d:%Y%m%d}", []).append(espn_event(eid, d, h, a, sc))
            eid += 1
    for e in upcoming:
        out.setdefault(pd.Timestamp(e["date"]).strftime("%Y%m%d"), []).append(e)
    return out


class Resp:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status

    def json(self):
        return self.body


def fake_espn(days_by_slug, log=None, fail=False):
    def get(url, params=None, timeout=None):
        assert url.startswith("https://site.api.espn.com/"), url  # ESPN only
        if log is not None:
            log.append((url, dict(params or {})))
        if fail:
            raise requests.ConnectionError("down")
        slug = url.split("/soccer/")[1].split("/")[0]
        days = days_by_slug.get(slug, {})
        cal = [f"{d[:4]}-{d[4:6]}-{d[6:]}T07:00Z" for d in sorted(days)]
        return Resp({"events": days.get(params["dates"], []), "leagues": [{"calendar": cal}]})

    return get


# ---------- ESPN parsing: scores, neutral venue, extra time ----------


def test_parse_events_reads_scores_neutral_and_the_90_minute_score():
    body = {
        "events": [
            espn_event(1, "2026-05-30T19:00Z", "PSG", "Arsenal", (2, 1), neutral=True),
            # 1-1 after 90, 2-1 after extra time: the first two linescores give the 90.
            espn_event(
                2,
                "2026-03-11T20:00Z",
                "Inter",
                "Barcelona",
                (2, 1),
                period=4,
                status="STATUS_FINAL_AET",
                linescores=[[1, 0, 1], [0, 1, 0]],
            ),
            # Extra time without linescores: the 90-minute score is unknown.
            espn_event(
                3,
                "2026-03-12T20:00Z",
                "Real Madrid",
                "Bayern",
                (3, 2),
                period=5,
                status="STATUS_FINAL_PEN",
            ),
            espn_event(4, "2026-10-21T19:00Z", "Celtic", "Benfica", completed=False),
        ]
    }
    ev = {e["id"]: e for e in cups.parse_events(body)}
    assert ev["1"]["home_score"] == 2 and ev["1"]["neutral"] and ev["1"]["score90"] == [2, 1]
    assert ev["2"]["extra_time"] and ev["2"]["home_score"] == 2 and ev["2"]["score90"] == [1, 1]
    assert ev["3"]["extra_time"] and ev["3"]["score90"] is None
    assert ev["4"]["home_score"] is None and ev["4"]["score90"] is None
    assert [e["id"] for e in cm.results(list(ev.values()))] == ["1", "2"]


def goal(team, clock, **kw):
    return {"scoringPlay": True, "team": {"id": f"id-{team}"}, "clock": {"displayValue": clock},
            **kw}  # fmt: skip


def test_extra_time_90_minute_score_from_goal_events_only_when_they_add_up():
    def aet(details, score=(2, 1)):
        e = espn_event(7, "2025-04-30T19:00Z", "Barcelona", "Internazionale", score, period=4,
                       status="STATUS_FINAL_AET")  # fmt: skip
        e["competitions"][0]["details"] = details
        return cups.parse_events({"events": [e]})[0]

    # 1-1 after 90 (stoppage time counts), Barcelona's 105th-minute goal is extra time;
    # shootout kicks never count.
    full = [goal("Barcelona", "12'"), goal("Internazionale", "90'+3'"),
            goal("Barcelona", "105'"), goal("Barcelona", "", shootout=True)]  # fmt: skip
    assert aet(full)["score90"] == [1, 1]
    assert aet(full[:2])["score90"] is None  # the events miss a goal: not trusted
    assert (
        aet([goal("Barcelona", "12'"), goal("Nobody", "50'"), goal("Barcelona", "99'")])["score90"]
        is None
    )  # a team it can't place
    assert aet(None)["score90"] is None


# ---------- the Dixon-Coles options: unchanged by default ----------


def test_dixon_coles_defaults_are_unchanged_and_neutral_drops_home_advantage():
    df = domestic_frame()[["date", "home", "away", "home_goals", "away_goals"]]
    df = df.assign(home="x" + df["home"], away="x" + df["away"])
    a = DixonColes().fit(df)
    b = DixonColes().fit(df.assign(neutral=0.0))  # an all-zero neutral column: identical
    for k in ("intercept", "home_adv", "rho"):
        assert a.params[k] == pytest.approx(b.params[k], abs=1e-9)
    lam, mu = a.expected_goals("xArsenal", "xLeeds")
    lam_n, mu_n = a.expected_goals("xArsenal", "xLeeds", neutral=True)
    assert lam_n < lam and mu_n == pytest.approx(mu)
    m = a.score_matrix("xArsenal", "xLeeds", neutral=True)
    assert m.sum() == pytest.approx(1.0)
    # A ridge pulls a club with almost no matches toward the average, and stays finite.
    tiny = pd.concat(
        [
            df,
            pd.DataFrame(
                [
                    {
                        "date": df["date"].max(),
                        "home": "new",
                        "away": "xArsenal",
                        "home_goals": 0,
                        "away_goals": 5,
                    }
                ]
            ),
        ],
        ignore_index=True,
    )
    r = DixonColes(ridge=0.1).fit(tiny)
    assert np.isfinite(r.attack["new"]) and r.attack["new"] > DixonColes().fit(tiny).attack["new"]


# ---------- domestic files ----------


def test_load_domestic_reads_main_and_extra_files_and_filters_dates(tmp_path):
    main = tmp_path / "P1_2526.csv"
    main.write_text(
        "Div,Date,HomeTeam,AwayTeam,FTHG,FTAG\n"
        "P1,10/08/2025,Benfica,Porto,2,1\nP1,17/08/2025,Porto,Braga,1,1\n"
        "P1,20/10/2026,Braga,Benfica,,\n"
    )
    extra = tmp_path / "NOR.csv"
    extra.write_text(
        "Country,League,Season,Date,Time,Home,Away,HG,AG\n"
        "Norway,Eliteserien,2026,01/06/2026,17:00,Bodo/Glimt,Molde,3,0\n"
        "Norway,Eliteserien,2021,01/06/2021,17:00,Bodo/Glimt,Molde,1,0\n"
    )
    df, errors = cm.load_domestic(
        "2025-08-01",
        "2026-10-13",
        leagues=("P1", "NOR"),
        main_path=lambda code, y: main if y == 2025 else (_ for _ in ()).throw(OSError("x")),
        extra_path=lambda code: extra,
    )
    assert list(df["league"]) == ["P1", "P1", "NOR"]
    assert df["home_goals"].tolist() == [2, 1, 3]
    assert errors == {"P1 2627": "OSError"}  # this season's file not posted: named, skipped


# ---------- names: football-data's club or the club's own UEFA name ----------


def test_names_are_prefixed_by_league_so_namesakes_never_merge():
    dom = pd.DataFrame(
        [
            {
                "league": "SC0",
                "date": pd.Timestamp("2026-08-01"),
                "home": "Rangers",
                "away": "Celtic",
                "home_goals": 1,
                "away_goals": 0,
            },
            {
                "league": "IRL",
                "date": pd.Timestamp("2026-08-01"),
                "home": "Rangers",
                "away": "Derry",
                "home_goals": 0,
                "away_goals": 3,
            },
        ]
    )
    teams = cm.teams_by_league(dom)
    mapping = cm.team_map(["Celtic", UNCOVERED], teams)
    assert mapping == {"Celtic": "SC0:Celtic", UNCOVERED: None}
    euro = [
        {
            **cups.parse_events(
                {"events": [espn_event(9, "2026-08-20T19:00Z", UNCOVERED, "Celtic", (1, 1))]}
            )[0],
            "cup": "UCL",
        }
    ]
    m = cm.fit_matches(dom, euro, mapping)
    assert {"SC0:Rangers", "IRL:Rangers", "SC0:Celtic", f"UEFA:id-{UNCOVERED}"} <= set(
        m["home"]
    ) | set(m["away"])


def test_the_fit_rates_leagues_against_each_other_through_european_matches():
    dom = domestic_frame()
    sched = european_schedule()
    euro = cups.parse_events({"events": [e for d in sched.values() for e in d]})
    mapping = cm.team_map([e[s] for e in euro for s in ("home", "away")], cm.teams_by_league(dom))
    assert mapping["FC Porto"] == "P1:Porto" and mapping[UNCOVERED] is None
    model = cm.fit(cm.fit_matches(dom, euro, mapping), NOW)
    # Each league's middle club is ordered by its league's level (E0 > P1 > SC0).
    rating = {k: model.attack[k] - model.defence[k] for k in model.attack}
    assert rating["E0:Leeds"] > rating["P1:Braga"] > rating["SC0:Hearts"]


# ---------- no look-ahead ----------


def test_fit_uses_only_matches_before_its_time():
    dom = domestic_frame()
    m = cm.fit_matches(dom, [], {})
    cut = pd.Timestamp("2026-03-02")
    w = cm.window(m, cut)
    assert w["date"].max() < cut and w["date"].min() >= cut - pd.Timedelta(days=cm.WINDOW_DAYS)
    later = m["date"] >= cut
    changed = m.copy()
    changed.loc[later, "home_goals"] = 9  # rewrite every result from the cut on
    a, b = cm.fit(m, cut), cm.fit(changed, cut)
    assert a.attack == pytest.approx(b.attack) and a.params == pytest.approx(b.params)


def test_walk_forward_predicts_each_week_from_matches_before_its_monday():
    dom = domestic_frame()
    sched = european_schedule()
    euro = cups.parse_events({"events": [e for d in sched.values() for e in d]})
    mapping = cm.team_map([e[s] for e in euro for s in ("home", "away")], cm.teams_by_league(dom))
    matches = cm.fit_matches(dom, euro, mapping)
    targets = [e for e in cm.results(euro) if pd.Timestamp("2025-09-01", tz="UTC") <= e["kickoff"]]
    targets = targets[:12]
    seen = []

    def spy(ms, as_of):
        seen.append((as_of, cm.window(ms, as_of)["date"].max()))
        return cm.fit(ms, as_of)

    preds = cm.walk_forward(targets, matches, mapping, fitter=spy)
    assert len(preds) == len(targets)
    for at, last in seen:
        assert at.weekday() == 0 and last < at  # Monday 00:00, nothing on or after it
    assert (preds["fit_to"] < preds["fit_at"]).all() and (preds["fit_at"] <= preds["date"]).all()
    covered = preds[preds["covered"]]
    assert np.allclose(covered[["p_home", "p_draw", "p_away"]].sum(axis=1), 1.0)
    assert preds.loc[~preds["covered"], "p_home"].isna().all()  # the uncovered club's games
    assert not preds["covered"].all()
    # Rewriting the targets' own results (and everything after) changes no prediction.
    cut = min(e["kickoff"] for e in targets)
    changed = matches.copy()
    changed.loc[changed["date"] >= cut.tz_localize(None), "away_goals"] = 7
    again = cm.walk_forward(targets, changed, mapping)
    first_week = preds["fit_at"] == preds["fit_at"].min()
    pd.testing.assert_frame_equal(
        preds.loc[first_week, ["p_home", "p_draw", "p_away"]],
        again.loc[first_week, ["p_home", "p_draw", "p_away"]],
    )


# ---------- evaluation and the blend's rows ----------


def closes_file(tmp_path, preds, rng):
    rows = []
    for r in preds.itertuples():
        fair = np.array([0.45, 0.27, 0.28]) + rng.normal(0, 0.03, 3)
        fair = np.clip(fair, 0.05, None)
        fair = fair / fair.sum()
        for book, margin in (("pinnacle", 1.03), ("draftkings", 1.06)):
            price = 1 / (fair * margin)
            rows.append(
                {
                    "espn_id": r.espn_id,
                    "book": book,
                    "price_home": round(price[0], 2),
                    "price_draw": round(price[1], 2),
                    "price_away": round(price[2], 2),
                }
            )
    path = tmp_path / "UCL_closes.jsonl"
    path.write_text("".join(json.dumps(x) + "\n" for x in rows))
    return path


def synthetic_preds(n=400, seed=3):
    rng = np.random.default_rng(seed)
    p = rng.dirichlet([4, 2.5, 3], n)
    y = np.array([rng.choice(3, p=pi) for pi in p])
    goals = {0: (2, 0), 1: (1, 1), 2: (0, 1)}
    dates = pd.date_range("2024-09-17", periods=n, freq="1D")
    return pd.DataFrame(
        {
            "espn_id": [str(i) for i in range(n)],
            "date": dates,
            "season": ["2425" if d < pd.Timestamp("2025-07-01") else "2526" for d in dates],
            "home": "a",
            "away": "b",
            "p_home": p[:, 0],
            "p_draw": p[:, 1],
            "p_away": p[:, 2],
            "covered": True,
            "low_data": False,
            "home_goals": [goals[k][0] for k in y],
            "away_goals": [goals[k][1] for k in y],
        }
    )


def test_evaluate_reports_model_pinnacle_draftkings_and_a_labelled_season_split(tmp_path):
    preds = synthetic_preds()
    path = closes_file(tmp_path, preds, np.random.default_rng(0))
    pin, dk = cm.load_closes(path, "pinnacle"), cm.load_closes(path, "draftkings")
    assert len(pin) == len(dk) == len(preds)
    ev = cm.evaluate(preds, pin, dk)
    mp = ev["model_vs_pinnacle"]
    assert ev["scored"] == mp["matches"] == len(preds)
    assert set(mp["by_season"]) == {"2425", "2526"}
    assert mp["draftkings"]["matches"] == len(preds)
    ss = ev["season_split"]
    assert ss["fit_season"] == "2425" and ss["score_season"] == "2526"
    assert "evaluation only" in ss["note"]
    assert cm.load_closes(tmp_path / "missing.jsonl").empty


def test_blend_rows_and_the_min_rows_guard_are_unchanged(tmp_path):
    preds = synthetic_preds(n=250)
    path = closes_file(tmp_path, preds, np.random.default_rng(1))
    rows = mc.training_rows(cm.blend_preds(preds, cm.load_closes(path)), "h2h")
    assert len(rows) == 250
    fit = mc.league_fit(rows, "2026-10-13")
    assert fit["coef"] is None and fit["matches"] == 250  # under MIN_ROWS: no blend
    fit = mc.league_fit(
        mc.training_rows(
            cm.blend_preds(
                synthetic_preds(400),
                cm.load_closes(
                    closes_file(tmp_path, synthetic_preds(400), np.random.default_rng(2))
                ),
            ),
            "h2h",
        ),
        "2026-10-13",
    )
    assert fit["coef"] is not None and fit["sigma"] > 0


# ---------- live: DraftKings pairing, cards, the run budget ----------


def odds_event(home, away, kickoff, ph, pd_, pa):
    return {
        "id": f"{home}-{away}",
        "commence_time": pd.Timestamp(kickoff).isoformat().replace("+00:00", "Z"),
        "home_team": home,
        "away_team": away,
        "bookmakers": [
            {
                "key": "draftkings",
                "last_update": "2026-10-20T15:00:00Z",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": home, "price": ph},
                            {"name": away, "price": pa},
                            {"name": "Draw", "price": pd_},
                        ],
                    }
                ],
            }
        ],
    }


def test_pair_odds_maps_the_odds_api_names_onto_espns():
    k = pd.Timestamp("2026-10-21 19:00", tz="UTC")
    fixtures = cups.parse_events(
        {
            "events": [
                espn_event(1, k, "Internazionale", "Bayern Munich", completed=False),
                espn_event(2, k, "FK Qarabag", "Celtic", completed=False),
            ]
        }
    )
    events = [
        odds_event("Inter Milan", "Bayern München", k, 2.9, 3.5, 2.4),
        odds_event("Qarabağ FK", "Celtic", k, 3.1, 3.4, 2.3),
        odds_event("Somebody", "Else", k, 2.0, 3.0, 4.0),
    ]
    odds, unmatched = cm.pair_odds(fixtures, events)
    assert odds["1"]["odds_home"] == 2.9 and odds["1"]["odds_away"] == 2.4
    assert odds["2"]["odds_draw"] == 3.4
    assert unmatched == ["Somebody v Else (2026-10-21 19:00)"]
    assert cm.pair_odds(fixtures, None) == ({}, [])


def test_budget_stops_requests_after_failures_or_time():
    calls = []

    def bad(url, params=None, timeout=None):
        calls.append(1)
        raise requests.ConnectionError("x")

    b = cm.Budget(bad, seconds=60, failures=2)
    for _ in range(5):
        cups._get_json("https://site.api.espn.com/x", {}, b, b.stats)
    assert len(calls) == 2 and b.stats["stopped"]  # nothing asked after the second failure
    t = iter([0.0, 100.0, 100.0])
    b = cm.Budget(lambda *a, **k: Resp({}), seconds=60, clock=lambda: next(t))
    assert cups._get_json("https://site.api.espn.com/x", {}, b, b.stats) is None
    assert "time budget" in b.stats["why"]


def fixtures_world(tmp_path, monkeypatch, fail_espn=False, odds_events=None):
    """ESPN (mocked) with the synthetic Champions League history and two fixtures on 21
    Oct (one with the uncovered club), football-data (mocked) with the domestic frame."""
    from soccer_stats import publish

    k = pd.Timestamp("2026-10-21 19:00", tz="UTC")
    up = [
        espn_event(1, k, "Arsenal", "FC Porto", completed=False),
        espn_event(2, k, UNCOVERED, "Celtic", completed=False),
    ]
    sched = european_schedule(until=NOW - pd.Timedelta(days=1), upcoming=up)
    get = fake_espn({"uefa.champions": sched}, fail=fail_espn)
    dom = domestic_frame()
    monkeypatch.setattr(cm, "load_domestic", lambda *a, **kw: (dom, {}))
    events = (
        odds_events
        if odds_events is not None
        else [
            odds_event("Arsenal", "Porto", k, 1.5, 4.4, 6.5),
            odds_event("Qarabağ FK", "Celtic", k, 3.1, 3.4, 2.3),
        ]
    )

    class Status:
        fetched_at, credits_left, last_cost, refresh_hours, error = (
            "2026-10-13T11:00:00+00:00",
            20000,
            1,
            3.0,
            None,
        )

    asked = {}

    def fake_fetch(league, raw_dir=None, kickoffs=None, share=1, now=None):
        asked.update(league=league, kickoffs=list(kickoffs or []), share=share)
        return events, Status()

    monkeypatch.setattr(publish, "fetch_odds", fake_fetch)
    return publish.cup_league_fixtures("UCL", 9, NOW, raw_dir=tmp_path, get=get), asked


def test_cup_cards_have_model_chances_and_odds_only_for_an_uncovered_club(
    tmp_path, monkeypatch, capsys
):
    (cards, source, block), asked = fixtures_world(tmp_path, monkeypatch)
    assert block is None and source["name"] == "DraftKings"
    assert asked["league"] == "UCL" and len(asked["kickoffs"]) == 2  # the matchday budget
    by = {c["home"]: c for c in cards}
    a = by["Arsenal"]
    assert a["league"] == "UCL" and not a.get("no_model") and not a["low_data"]
    assert sum(a["p"][m] for m in ("home", "draw", "away")) == pytest.approx(1.0)
    assert a["p"]["home"] > a["p"]["away"] and a["odds"]["home"] == 1.5
    assert a["implied"]["home"] is not None and len(a["total_goals_cdf"]) == 7
    q = by[UNCOVERED]
    assert q["no_model"] and q["low_data"] and q["p"]["home"] is None
    assert q["odds"]["away"] == 2.3 and UNCOVERED in q["no_model_reason"]
    out = capsys.readouterr().out
    assert "1 of 2 fixtures with a model chance" in out and f"{UNCOVERED} (not covered)" in out


def test_cup_cards_show_odds_only_while_european_results_are_incomplete(
    tmp_path, monkeypatch, capsys
):
    (cards, source, _), _ = fixtures_world(tmp_path, monkeypatch, fail_espn=True)
    # ESPN down: no fixtures known, nothing built, the site still publishes.
    assert cards == [] and "incomplete" in capsys.readouterr().out


def test_lean_and_paper_trades_skip_a_no_model_card(tmp_path, monkeypatch):
    from soccer_stats import paper, publish
    from soccer_stats import trades as tr

    (cards, source, _), _ = fixtures_world(tmp_path, monkeypatch)
    cards = publish._clean(cards)
    data = {"fixtures": cards, "league_code": "E0"}
    blends = {"leagues": {"UCL": {"coef": [0.0, 0.0, 1.0, 0.0], "sigma": 0.0001, "matches": 300}}}
    publish.add_lean(data, blends)
    q = next(c for c in data["fixtures"] if c.get("no_model"))
    assert q["lean_pick"] is None and q["z"] is None
    events, _ = paper.update_ledger(
        {},
        data["fixtures"],
        {**source, "fetched_at": NOW.isoformat()},
        pd.DataFrame(),
        NOW,
        "UCL",
        threshold=0.0001,
        rule=tr.paper_threshold(None, "UCL"),
        lean=tr.lean_rule({"leagues": blends["leagues"]}, "UCL"),
    )
    opened = [e for e in events if e["type"] == "open"]
    assert opened and all(e["home"] != UNCOVERED for e in opened)
    assert {e["id"].split("|")[0] for e in opened} == {"UCL"}


def test_no_model_card_has_the_card_shape():
    from soccer_stats.publish import implied_probs

    e = cups.parse_events({"events": [espn_event(5, NOW, UNCOVERED, "Celtic", completed=False)]})
    c = cm.no_model_card(
        e[0], {"odds_home": 3.0, "odds_draw": 3.3, "odds_away": 2.4}, "why", implied_probs
    )
    assert c["league"] == "UCL" and c["implied"]["margin"] > 0 and c["xg"] == [None, None]
    assert c["no_model_reason"] == "why" and c["matrix"] is None
    assert cm.no_model_card(e[0], None, "w", implied_probs)["implied"]["home"] is None


def test_days_not_yet_final_are_reused_for_a_short_while_on_live_runs(tmp_path):
    log = []
    k = NOW + pd.Timedelta(days=2)
    get = fake_espn(
        {"uefa.champions": {f"{k:%Y%m%d}": [espn_event(1, k, "A", "B", completed=False)]}}, log
    )
    day = f"{k:%Y%m%d}"
    for _ in range(3):
        cups.scoreboard_day("uefa.champions", day, tmp_path, get, {}, NOW, ttl_minutes=30)
    assert len(log) == 1  # reused from the short-lived copy
    cups.scoreboard_day("uefa.champions", day, tmp_path, get, {}, NOW)
    assert len(log) == 2  # without a TTL a day not yet final is always asked again
    assert not cups._cache_path(tmp_path, "uefa.champions", day).exists()  # never "final"


def test_upcoming_skips_placeholder_knockout_ties(tmp_path):
    k = NOW + pd.Timedelta(days=2)
    sched = {
        f"{k:%Y%m%d}": [
            espn_event(1, k, "TBD", "Arsenal", completed=False),
            espn_event(2, k, "Winner QF1", "Winner QF2", completed=False),
            espn_event(3, k, "Bodo/Glimt", "Arsenal", completed=False),
        ]
    }
    got = cm.upcoming(NOW, tmp_path, fake_espn({"uefa.champions": sched}))
    assert [e["id"] for e in got] == ["3"]


# ---------- settlement ----------


def test_cup_results_settle_on_90_minutes_with_espns_names(tmp_path):
    sched = {
        "20260916": [espn_event(1, "2026-09-16T19:00Z", "Arsenal", "FC Porto", (2, 0))],
        "20261001": [
            espn_event(
                2,
                "2026-10-01T19:00Z",
                "Celtic",
                "Benfica",
                (2, 2),
                period=4,
                status="STATUS_FINAL_AET",
            )
        ],
        "20261021": [espn_event(3, "2026-10-21T19:00Z", "Celtic", "Arsenal", completed=False)],
    }
    res = cm.cup_results(2026, tmp_path, fake_espn({"uefa.champions": sched}), now=NOW)
    assert res[["home", "away", "home_goals", "away_goals", "season"]].values.tolist() == [
        ["Arsenal", "FC Porto", 2, 0, "2627"]
    ]  # the extra-time match without a 90-minute score waits (voids after 14 days)


def test_paper_results_take_the_cup_from_espn_and_never_from_football_data(monkeypatch):
    from soccer_stats import cli

    asked = []

    def fake_load(codes, years):
        asked.append(list(codes))
        raise OSError("football-data down")

    def fake_cup(season, get=None, stats=None):
        return pd.DataFrame(
            [
                {
                    "league": "UCL",
                    "season": cm.season_code(season),
                    "home": "A",
                    "away": "B",
                    "home_goals": 1,
                    "away_goals": 0,
                }
            ]
        )

    monkeypatch.setattr(cli, "load_matches", fake_load)
    res = cli.cup_and_league_results(["E0", "UCL", "SP1"], 2026, cup_results=fake_cup)
    assert asked == [["E0", "SP1"]]  # the cup is never asked of football-data
    assert set(res["league"]) == {"UCL"} and set(res["season"]) == {"2526", "2627"}


def test_a_ucl_trade_settles_from_espn_results(tmp_path):
    from soccer_stats import paper
    from soccer_stats import trades as tr

    k = pd.Timestamp("2026-10-21 19:00", tz="UTC")
    card = {
        "league": "UCL",
        "kickoff": k.isoformat(),
        "home": "Arsenal",
        "away": "FC Porto",
        "p": {"home": 0.75, "draw": 0.15, "away": 0.10, "over25": None, "under25": None},
        "odds": {"home": 1.6, "draw": 4.2, "away": 6.0},
        "low_data": False,
    }
    src = {"name": "DraftKings", "fetched_at": (k - pd.Timedelta(hours=5)).isoformat()}
    rule = tr.paper_threshold(None, "UCL")
    ledger = {}
    ev, _ = paper.update_ledger(
        ledger,
        [card],
        src,
        pd.DataFrame(),
        k - pd.Timedelta(hours=4),
        "UCL",
        threshold=rule["threshold"],
        rule=rule,
    )
    t = ledger["UCL|2627|Arsenal|FC Porto"]
    assert t["market"] == "home" and t["strategy"] == "edge12"
    paper.append_events(tmp_path, ev, "UCL")
    assert (tmp_path / "paper_trades" / "UCL_2627.jsonl").exists()
    res = pd.DataFrame(
        [
            {
                "league": "UCL",
                "season": "2627",
                "date": pd.Timestamp("2026-10-21"),
                "home": "Arsenal",
                "away": "FC Porto",
                "home_goals": 2,
                "away_goals": 1,
                "close_home": np.nan,
                "close_draw": np.nan,
                "close_away": np.nan,
            }
        ]
    )
    ev, _ = paper.update_ledger(
        ledger,
        [],
        src,
        res,
        k + pd.Timedelta(hours=3),
        "UCL",
        threshold=rule["threshold"],
        rule=rule,
    )
    assert t["status"] == "won" and t["profit"] == pytest.approx(6.0)


def test_blend_entry_fits_on_walk_forward_rows_and_reports_the_guard(tmp_path, monkeypatch):
    monkeypatch.setattr(cups, "PAUSE", 0.0)
    sched = european_schedule(until=NOW - pd.Timedelta(days=2))
    get = fake_espn({"uefa.champions": sched})
    dom = domestic_frame(start="2022-08-06")
    events = cups.parse_events({"events": [e for d in sched.values() for e in d]})
    rows = [
        {"espn_id": e["id"], "book": book, "price_home": 2.2, "price_draw": 3.4, "price_away": 3.3}
        for e in events
        for book in ("pinnacle", "draftkings")
    ]
    path = tmp_path / "UCL_closes.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    entry = cm.blend_entry(
        path,
        "2026-10-13",
        raw_dir=tmp_path,
        get=get,
        today=NOW,
        domestic_loader=lambda a, b: (dom, {}),
    )
    assert entry["european"]["complete"]
    cov = {c["season"]: c for c in entry["coverage"]}
    assert set(cov) == {"2425", "2526"}
    assert entry["fit_rows"] == sum(c["rows"] for c in cov.values()) < mc.MIN_ROWS
    assert entry["coef"] is None and "minimum" in entry["note"]  # the guard is unchanged
    assert entry["no_model"] == [UNCOVERED]
    assert entry["evaluation"]["model_vs_pinnacle"]["matches"] == entry["fit_rows"]
    assert entry["current_clubs"]["season"] == "2627"
    lines = "\n".join(cm.blend_report(entry))
    assert "Walk-forward log loss" in lines and "fit rows" in lines
    # An incomplete European history gives no fit at all.
    bad = cm.blend_entry(
        path,
        "2026-10-13",
        raw_dir=tmp_path / "cold",
        get=fake_espn({}, fail=True),
        today=NOW,
        domestic_loader=lambda a, b: (dom, {}),
    )
    assert bad["coef"] is None and "incomplete" in bad["error"]


# ---------- registry and the rest of the pipeline ----------


def test_ucl_is_live_and_the_other_cups_stay_off():
    from soccer_stats import corners_live, odds_feed, team_totals
    from soccer_stats.publish import league_list

    ucl = lgs.get("UCL")
    assert ucl.live and ucl.cup and ucl.odds_policy == "matchday" and ucl.understat is None
    assert ucl.odds_sport == "soccer_uefa_champs_league"
    assert "UCL" in lgs.live_codes() and lgs.live_codes()[0] == "E0"
    for code in ("UEL", "UECL"):
        assert not lgs.get(code).live and code not in lgs.live_codes()
    # No team corners or team totals for the Champions League in this step.
    assert "UCL" not in corners_live.LEAGUES
    assert "UCL" not in odds_feed.TEAM_TOTAL_LEAGUES and "UCL" not in team_totals.TEAM_TOTAL_LEAGUES
    rows = league_list([{"league": "UCL"}], {"UCL": {"name": "DraftKings"}})
    assert {r["code"]: r for r in rows}["UCL"] == {
        "code": "UCL",
        "name": "Champions League",
        "live": True,
        "fixtures": 1,
        "odds": "DraftKings",
    }
    assert "UEL" not in {r["code"] for r in rows}


def test_espn_team_news_reads_the_champions_league_by_espns_names():
    from soccer_stats import espn_news

    assert espn_news.SLUGS["UCL"] == "uefa.champions"
    known = {"Internazionale", "Paris Saint-Germain", "Arsenal"}
    assert espn_news.team_name("Internazionale", known) == "Internazionale"
    assert espn_news.team_name("Paris Saint-Germain", known) == "Paris Saint-Germain"
    assert espn_news.team_name("Internazionale", {"Inter", "Milan"}) == "Inter"  # Serie A


def test_odds_log_and_close_planner_see_ucl_kickoffs(tmp_path):
    from soccer_stats import close_runs
    from soccer_stats import odds_log as ol

    k = pd.Timestamp("2026-10-21 19:00", tz="UTC")
    data = {
        "fixtures": [
            {
                "league": "UCL",
                "home": "Arsenal",
                "away": "FC Porto",
                "kickoff": k.isoformat(),
                "odds": {"home": 1.6, "draw": 4.2, "away": 6.0},
                "p": {"home": 0.6, "draw": 0.2, "away": 0.2},
            }
        ],
        "odds_sources": {"UCL": {"name": "DraftKings", "fetched_at": "2026-10-20T10:00:00Z"}},
    }
    rows = ol.rows_from_data(data, NOW)
    assert len(rows) == 1 and rows[0]["league"] == "UCL"
    ol.append(tmp_path, rows)
    assert (tmp_path / "odds_log" / "UCL_2026-10.jsonl").exists()
    assert close_runs.kickoffs(tmp_path / "odds_log") == [k]


def test_backtest_lean_never_loads_the_cup_from_football_data(monkeypatch):
    import argparse

    from soccer_stats import cli

    seen = []

    def fake_load(codes, years):
        seen.extend(codes)
        raise OSError("stop")

    monkeypatch.setattr(cli, "load_matches", fake_load)
    args = argparse.Namespace(
        leagues=None, from_season=2025, burn_in=0, dk_file=None, json=None, log_dir=None
    )
    cli.cmd_backtest_lean(args)
    assert "UCL" not in seen and "E0" in seen


# ---------- names: the real 2026/27 league phase and the bought closes ----------

# ESPN's 2026/27 Champions League league phase and football-data's clubs, as backfill.yml
# run 38457777910 (fit-match-blends, no key) mapped them; four clubs no football-data
# league covers get no model chance.
UCL_2627 = {
    "AEK Athens": "G1:AEK", "AS Roma": "I1:Roma", "Arsenal": "E0:Arsenal",
    "Aston Villa": "E0:Aston Villa", "Atlético Madrid": "SP1:Ath Madrid",
    "Barcelona": "SP1:Barcelona", "Bayern Munich": "D1:Bayern Munich",
    "Bodo/Glimt": "NOR:Bodo/Glimt", "Borussia Dortmund": "D1:Dortmund",
    "Club Brugge": "B1:Club Brugge", "Como": "I1:Como", "FC Porto": "P1:Porto",
    "Fenerbahce": "T1:Fenerbahce", "Feyenoord Rotterdam": "N1:Feyenoord",
    "Galatasaray": "T1:Galatasaray", "Internazionale": "I1:Inter", "LASK Linz": "AUT:LASK",
    "Lens": "F1:Lens", "Lille": "F1:Lille", "Liverpool": "E0:Liverpool",
    "Manchester City": "E0:Man City", "Manchester United": "E0:Man United",
    "Napoli": "I1:Napoli", "PSV Eindhoven": "N1:PSV Eindhoven",
    "Paris Saint-Germain": "F1:Paris SG", "RB Leipzig": "D1:RB Leipzig",
    "Real Betis": "SP1:Betis", "Real Madrid": "SP1:Real Madrid", "Sabah FK": None,
    "Shakhtar Donetsk": None, "Slavia Prague": None, "Slovan Bratislava": None,
    "Sporting CP": "P1:Sp Lisbon", "VfB Stuttgart": "D1:Stuttgart", "Viking FK": "NOR:Viking",
    "Villarreal": "SP1:Villarreal",
}  # fmt: skip
# Their leagues' other clubs that could be confused with them.
DECOYS = {
    "E0": ["Man City", "Man United", "Liverpool", "Arsenal", "Aston Villa", "Chelsea"],
    "SP1": ["Real Madrid", "Barcelona", "Ath Madrid", "Ath Bilbao", "Betis", "Sevilla",
            "Villarreal", "Sociedad"],
    "D1": ["Bayern Munich", "Dortmund", "RB Leipzig", "Stuttgart", "Leverkusen",
           "M'gladbach"],
    "I1": ["Inter", "Milan", "Roma", "Lazio", "Napoli", "Como"],
    "F1": ["Paris SG", "Lens", "Lille", "Lyon", "Marseille"],
    "P1": ["Porto", "Benfica", "Sp Lisbon", "Sp Braga"],
    "N1": ["PSV Eindhoven", "Feyenoord", "Ajax"],
    "T1": ["Galatasaray", "Fenerbahce", "Besiktas"],
    "G1": ["AEK", "Olympiakos", "PAOK"],
    "B1": ["Club Brugge", "Anderlecht", "Genk"],
    "NOR": ["Bodo/Glimt", "Viking", "Molde", "Rosenborg"],
    "AUT": ["LASK", "Salzburg", "Sturm Graz"],
}  # fmt: skip


def test_every_2026_27_league_phase_club_maps_to_its_football_data_club():
    teams = {code: set(names) for code, names in DECOYS.items()}
    assert cm.team_map(list(UCL_2627), teams) == UCL_2627


# Every Odds API spelling that differed from ESPN's in the bought closes
# (odds_history/UCL_closes.jsonl, run 38452265319): the live cards pair on them.
ODDS_PAIRS = {
    "Sporting Lisbon": "Sporting CP", "LOSC Lille": "Lille", "Bayern München": "Bayern Munich",
    "Salzburg": "RB Salzburg", "Paris Saint Germain": "Paris Saint-Germain",
    "Internazionale Milano": "Internazionale", "ŠK Slovan Bratislava": "Slovan Bratislava",
    "Feyenoord": "Feyenoord Rotterdam", "Monaco": "AS Monaco",
    "Athletic Bilbao": "Athletic Club", "Union Saint-Gilloise": "Union St.-Gilloise",
    "Qarabağ FK": "FK Qarabag", "Olympiakos Piraeus": "Olympiacos", "Pafos FC": "Pafos",
    "Slavia Praha": "Slavia Prague", "Bodø/Glimt": "Bodo/Glimt", "Atalanta BC": "Atalanta",
    "Ajax": "Ajax Amsterdam", "Inter Milan": "Internazionale",
    "FC Copenhagen": "F.C. København", "FC Kairat": "Kairat Almaty",
}  # fmt: skip


def test_the_odds_api_names_from_the_bought_closes_pair_strongly_with_espns():
    from soccer_stats import cup_odds

    for odds_name, espn_name in ODDS_PAIRS.items():
        assert cup_odds.same_team(odds_name, espn_name) in cup_odds.STRONG, odds_name
    assert cup_odds.same_team("Inter Milan", "AC Milan") is None
