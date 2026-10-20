"""The cup corners model (cup_corners.py; owner, 20 Oct 2026; docs/totals.md "Cup corners
model"). Synthetic matches and a mocked ESPN only: nothing here may reach the network."""

import json

import numpy as np
import pandas as pd
import requests

from soccer_stats import cup_corners as cc
from soccer_stats.corners_live import CDF_MAX

# Two football-data leagues with corner counts; each club's true corner for/against
# ratio, and how much stronger each league is in Europe than its domestic ratios say.
CLUBS = {
    "E0": [f"E{i}" for i in range(12)],
    "P1": [f"P{i}" for i in range(12)],
}
COUNTRY = {"E0": "England", "P1": "Portugal"}
CUP_LEAGUE = {"E0": 0.25, "P1": -0.25}  # log offset for corners taken in a cup match
CUP_ONLY = "Sturm"  # an Austrian club: football-data's AUT file has no HC/AC
OTHER = "Rapid"  # a second one (eb_shrink needs two clubs)


def ratios(seed=0):
    rng = np.random.default_rng(seed)
    out = {}
    for names in CLUBS.values():
        for n in names:
            out[n] = (float(np.exp(rng.normal(0, 0.25))), float(np.exp(rng.normal(0, 0.2))))
    out[CUP_ONLY] = (1.3, 0.8)
    out[OTHER] = (0.8, 1.1)
    return out


def domestic(seed=0, start="2023-08-05", end="2026-06-01"):
    rng = np.random.default_rng(seed)
    r = ratios()
    rows = []
    for d in pd.date_range(start, end, freq="7D"):
        for lg, names in CLUBS.items():
            perm = rng.permutation(names)
            for h, a in zip(perm[::2], perm[1::2], strict=True):
                rows.append(
                    {
                        "league": lg,
                        "country": COUNTRY[lg],
                        "date": d,
                        "home": h,
                        "away": a,
                        "home_corners": float(rng.poisson(5.5 * r[h][0] * r[a][1])),
                        "away_corners": float(rng.poisson(4.5 * r[a][0] * r[h][1])),
                    }
                )
    return pd.DataFrame(rows)


def key(name):
    if name == CUP_ONLY:
        return "Austria:Sturm Graz"
    if name == OTHER:
        return "Austria:Rapid Vienna"
    return f"{COUNTRY['E0' if name.startswith('E') else 'P1']}:{name}"


def league_of(name):
    return "E0" if name.startswith("E") else "P1" if name.startswith("P") else None


def cup(seed=1, start="2023-09-05 19:00", end="2026-05-20"):
    """Cup matches every Tuesday: English v Portuguese clubs, plus one Sturm match a week."""
    rng = np.random.default_rng(seed)
    r = ratios()
    rows = []
    eid = 1
    for d in pd.date_range(start, end, freq="7D"):
        es, ps = rng.permutation(CLUBS["E0"]), rng.permutation(CLUBS["P1"])
        games = [(es[i], ps[i]) if i % 2 else (ps[i], es[i]) for i in range(4)]
        games.append((CUP_ONLY, es[5]) if rng.random() < 0.5 else (ps[5], CUP_ONLY))
        games.append((OTHER, ps[6]) if rng.random() < 0.5 else (es[6], OTHER))
        for j, (h, a) in enumerate(games):
            off_h = CUP_LEAGUE.get(league_of(h), 0.0)
            off_a = CUP_LEAGUE.get(league_of(a), 0.0)
            cupc = ["UCL", "UEL", "UECL"][j % 3]
            rows.append(
                {
                    "espn_id": str(eid),
                    "cup": cupc,
                    "slug": cc.CUPS[cupc],
                    "kickoff": d,
                    "season": cc.season_of(d),
                    "home": h,
                    "away": a,
                    "home_key": key(h),
                    "away_key": key(a),
                    "neutral": False,
                    "home_corners": float(
                        rng.poisson(5.0 * r[h][0] * r[a][1] * np.exp(off_h - off_a / 2))
                    ),
                    "away_corners": float(
                        rng.poisson(4.2 * r[a][0] * r[h][1] * np.exp(off_a - off_h / 2))
                    ),
                }
            )
            eid += 1
    return pd.DataFrame(rows)


DOM = domestic()
CUP = cup()


# ---------- the fit ----------


def test_fit_uses_only_matches_before_its_time():
    t = pd.Timestamp("2025-10-06")
    a = cc.CupCorners().fit(DOM, CUP, t)
    dom2, cup2 = DOM.copy(), CUP.copy()
    dom2.loc[dom2["date"] >= t, ["home_corners", "away_corners"]] = 30.0
    cup2.loc[cup2["kickoff"] >= t, ["home_corners", "away_corners"]] = 30.0
    b = cc.CupCorners().fit(dom2, cup2, t)
    target = CUP[CUP["kickoff"] >= t].head(20)
    for cand in cc.CANDS:
        for x, y in zip(a.means(target)[cand], b.means(target)[cand], strict=True):
            np.testing.assert_allclose(x, y)
    # and the future does matter once it is in the past
    c = cc.CupCorners().fit(dom2, cup2, t + pd.Timedelta(days=60))
    assert not np.allclose(a.means(target)["j"][0], c.means(target)["j"][0], equal_nan=True)


def test_league_offsets_recover_which_league_takes_more_corners_in_europe():
    m = cc.CupCorners().fit(DOM, CUP, pd.Timestamp("2026-06-01"))
    assert m.ok_j
    assert m.o["E0"] > m.o["P1"]  # English clubs take more cup corners than their ratios say
    assert all(abs(v) < 1 for v in [*m.o.values(), *m.dd.values()])


def test_club_ratings_domestic_cup_only_and_unrated():
    m = cc.CupCorners().fit(DOM, CUP, pd.Timestamp("2026-06-01"))
    assert m.rated("England:E1") == "domestic"
    assert m.rated("Austria:Sturm Graz") == "cup_only"  # many cup matches, no domestic data
    assert m.rated("UEFA:999") is None
    # (i) and (j) give no chance for the cup-only club; (k) does
    row = pd.DataFrame(
        [
            {
                "cup": "UEL",
                "neutral": False,
                "home_key": "Austria:Sturm Graz",
                "away_key": "England:E1",
            }
        ]
    )
    means = m.means(row)
    assert np.isnan(means["i"][0][0]) and np.isnan(means["j"][0][0])
    assert np.isfinite(means["k"][0][0]) and np.isfinite(means["k"][1][0])


def test_a_cup_only_club_needs_enough_cup_matches():
    few = CUP[(CUP["home"] != cc_only()) & (CUP["away"] != cc_only())]
    sturm = CUP[(CUP["home"] == cc_only()) | (CUP["away"] == cc_only())].tail(cc.MIN_CUP_ONLY - 1)
    m = cc.CupCorners().fit(DOM, pd.concat([few, sturm]), pd.Timestamp("2026-06-01"))
    assert m.rated("Austria:Sturm Graz") is None


def cc_only():
    return CUP_ONLY


def test_neutral_venue_gives_both_sides_the_average_scale():
    m = cc.CupCorners().fit(DOM, CUP, pd.Timestamp("2026-06-01"))
    base = {"cup": "UCL", "home_key": "England:E1", "away_key": "England:E1"}
    rows = pd.DataFrame([{**base, "neutral": False}, {**base, "neutral": True}])
    a = m.means(rows)["a"]
    assert a[0][1] == a[1][1]  # same club both sides: equal means at a neutral venue
    assert a[0][0] > a[1][0] > a[1][0] * 0  # home's mean above the neutral one
    j = m.means(rows)["j"]
    np.testing.assert_allclose(j[0][1], j[1][1])


# ---------- the card shape and the no-rating fallback ----------


def test_card_corners_has_add_model_shape_and_none_for_an_unrated_club():
    m = cc.CupCorners().fit(DOM, CUP, pd.Timestamp("2026-06-01"))
    got = cc.card_corners(m, {"cup": "UCL", "home_key": "England:E1", "away_key": "Portugal:P2"})
    assert got["model"] == "cup_j"
    for s in ("home", "away"):
        cdf = got[s]["cdf"]
        assert len(cdf) == CDF_MAX + 1 and all(np.diff(cdf) >= 0) and cdf[-1] <= 1
        assert got[s]["mean"] > 0
    row = {"cup": "UCL", "home_key": "UEFA:123", "away_key": "Portugal:P2"}
    assert cc.card_corners(m, row) is None
    sturm = {"cup": "UEL", "home_key": "Austria:Sturm Graz", "away_key": "Portugal:P2"}
    assert cc.card_corners(m, sturm) is None
    assert cc.card_corners(m, sturm, "k")["model"] == "cup_k"


def test_add_model_sets_corners_only_where_both_clubs_are_rated():
    m = cc.CupCorners().fit(DOM, CUP, pd.Timestamp("2026-06-01"))
    mapping = {
        "Arsenal E1": {"key": "England:E1"},
        "Benfica P2": {"key": "Portugal:P2"},
        "Sturm Graz": {"key": "Austria:Sturm Graz"},
    }
    cards = [
        {"league": "UCL", "home": "Arsenal E1", "away": "Benfica P2"},
        {"league": "UEL", "home": "Sturm Graz", "away": "Benfica P2"},
        {"league": "UECL", "home": "Qarabag", "away": "Arsenal E1", "home_id": "9"},
        {"league": "E0", "home": "Arsenal E1", "away": "Benfica P2"},
    ]
    out = cc.add_model(cards, m, mapping)
    assert out == {"cards": 3, "with_model": 1, "unrated": ["Qarabag", "Sturm Graz"]}
    assert set(cards[0]["corners"]) == {"model", "home", "away"}
    assert "corners" not in cards[1] and "corners" not in cards[2] and "corners" not in cards[3]


# ---------- walk-forward and scoring ----------


def test_walk_forward_fits_weekly_on_earlier_matches_and_scores_by_set():
    start, end = pd.Timestamp("2025-09-01"), pd.Timestamp("2025-12-01")
    pred = cc.walk_forward(DOM, CUP, start, end)
    assert len(pred) and (pred["kickoff"] >= start).all() and (pred["kickoff"] < end).all()
    assert (pred["fit_at"] <= pred["kickoff"]).all()
    assert (pred["fit_at"].dt.weekday == 0).all()
    assert set(pred["set"].dropna()) == {"D", "X"}
    x = pred[pred["set"] == "X"]
    assert x[["home", "away"]].isin([CUP_ONLY, OTHER]).any(axis=1).all()
    d = pred[pred["set"] == "D"]
    np.testing.assert_allclose(d["p_k_home_4.5"], d["p_j_home_4.5"])  # (k) = (j) on set D
    rows = cc.long_rows(pred, "j")
    assert len(rows) == 6 * len(d) and set(rows["cup"]) <= {"UCL", "UEL", "UECL"}
    assert rows["p_raw"].between(0, 1).all() and rows["p_base"].between(0, 1).all()


def test_score_and_verdict_follow_the_pre_registered_rule(monkeypatch):
    from soccer_stats.edge import corner_recal, totals

    monkeypatch.setattr(totals, "N_BOOT", 500)
    monkeypatch.setattr(corner_recal, "N_BOOT", 500)
    monkeypatch.setattr(totals, "_boot_slope", lambda p, y, g: (0.0, 2.0))  # speed only
    pred = cc.walk_forward(DOM, CUP, pd.Timestamp("2025-08-01"), pd.Timestamp("2026-03-01"))
    res = cc.score(pred, 0.99583)
    assert set(res) == set(cc.FAMILY) and set(res["j"]) == set(cc.GROUPS)
    for cand in cc.FAMILY:
        for r in res[cand].values():
            if "gain" in r:
                assert r["judged"] == (r["matches"] >= cc.MIN_JUDGED)
                if r["pass"]:
                    assert r["gain_range"][0] > 0 and r["slopes_in_band"] and r["judged"]
    v = cc.verdict(res)
    assert set(v) == {"UCL", "UEL", "UECL"}
    for cup_, x in v.items():
        assert x["from"] in (cup_, "all") and x["finalist"] in (None, "i", "j")
    assert cc.TESTS == 12


# ---------- data: football-data, names, ESPN corner counts ----------


def test_load_domestic_counts_corner_coverage_and_reads_extra_names(tmp_path):
    main = tmp_path / "E0_2526.csv"
    main.write_text(
        "Date,HomeTeam,AwayTeam,FTHG,FTAG,HC,AC\n"
        "16/08/2025,Arsenal,Leeds,2,0,7,3\n"
        "17/08/2025,Chelsea,Burnley,1,1,,\n"
        "23/08/2025,Leeds,Chelsea,,,,\n"
    )
    nocorners = tmp_path / "G1_2526.csv"
    nocorners.write_text("Date,HomeTeam,AwayTeam,FTHG,FTAG\n16/08/2025,Olympiakos,PAOK,1,0\n")
    extra = tmp_path / "AUT.csv"
    extra.write_text(
        "Country,League,Season,Date,Home,Away,HG,AG\n"
        "Austria,Bundesliga,2025/2026,02/08/2025,Sturm Graz,Rapid Vienna,2,1\n"
        "Austria,Bundesliga,2012/2013,02/08/2012,Old Club,Rapid Vienna,2,1\n"
    )
    paths = {("E0", 2025): main, ("G1", 2025): nocorners}

    def mp(code, y):
        if (code, y) not in paths:
            raise FileNotFoundError(code)
        return paths[(code, y)]

    got = cc.load_domestic(
        seasons=(2025,),
        main_path=mp,
        extra_path=lambda c: extra,
        leagues=("E0", "G1"),
        extras=("AUT",),
    )
    m = got["matches"]
    assert list(m["home"]) == ["Arsenal"] and m.iloc[0]["country"] == "England"
    cov = {(r["league"], r["season"]): r for r in got["coverage"]}
    assert cov[("E0", "2526")]["played"] == 2 and cov[("E0", "2526")]["with_corners"] == 1
    assert cov[("G1", "2526")]["columns"] is False and cov[("G1", "2526")]["with_corners"] == 0
    assert cov[("AUT", "all")]["columns"] is False
    assert got["teams"]["AUT"] == {"Sturm Graz", "Rapid Vienna"}
    assert "Olympiakos" in got["teams"]["G1"]


def test_club_map_keys_by_country_and_keeps_namesakes_apart():
    teams = {"E0": {"Arsenal", "Sporting"}, "P1": {"Sp Lisbon", "Benfica"}, "AUT": {"Sturm Graz"}}
    m = cc.club_map(["Arsenal", "SL Benfica", "Sturm Graz", "Qarabag FK"], teams)
    assert m["Arsenal"]["key"] == "England:Arsenal"
    assert m["SL Benfica"]["key"] == "Portugal:Benfica"
    assert m["Sturm Graz"]["key"] == "Austria:Sturm Graz"
    assert m["Qarabag FK"] is None
    e = {"home": "Qarabag FK", "home_id": "777", "away": "Arsenal"}
    assert cc.side_key(e, "home", m) == "UEFA:777"
    assert cc.side_key(e, "away", m) == "England:Arsenal"


class Resp:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status

    def json(self):
        return self.body


def summary_body(h, a):
    return {
        "boxscore": {
            "teams": [
                {
                    "team": {"id": "1", "displayName": "Home"},
                    "statistics": [{"name": "wonCorners", "displayValue": str(h)}],
                },
                {
                    "team": {"id": "2", "displayName": "Away"},
                    "statistics": [{"name": "wonCorners", "displayValue": str(a)}],
                },
            ]
        }
    }


def test_fetch_corners_caches_final_matches_for_good(tmp_path):
    today = pd.Timestamp("2026-10-20 12:00", tz="UTC")
    events = [
        {
            "id": "1",
            "slug": "uefa.europa",
            "kickoff": today - pd.Timedelta(days=30),
            "home": "Home",
            "away": "Away",
            "home_id": "1",
            "away_id": "2",
            "completed": True,
        },
        {
            "id": "2",
            "slug": "uefa.europa",
            "kickoff": today - pd.Timedelta(hours=3),
            "home": "Home",
            "away": "Away",
            "home_id": "1",
            "away_id": "2",
            "completed": True,
        },
        {
            "id": "3",
            "slug": "uefa.europa",
            "kickoff": today - pd.Timedelta(days=9),
            "home": "Home",
            "away": "Away",
            "home_id": "1",
            "away_id": "2",
            "completed": True,
        },
        {
            "id": "4",
            "slug": "uefa.europa",
            "kickoff": today + pd.Timedelta(days=2),
            "home": "Home",
            "away": "Away",
            "home_id": "1",
            "away_id": "2",
            "completed": False,
        },
    ]
    log = []

    def get(url, params=None, timeout=None):
        assert url.startswith("https://site.api.espn.com/"), url
        log.append(params["event"])
        if params["event"] == "3":
            return Resp({"boxscore": {"teams": []}})  # final, but no corner counts
        return Resp(summary_body(6, 2))

    got, stats = cc.fetch_corners(events, tmp_path, get, today=today, workers=2)
    assert got == {"1": (6, 2), "2": (6, 2), "3": None}
    assert sorted(log) == ["1", "2", "3"] and stats["requests"] == 3
    cached = json.loads((tmp_path / "cups/corners/uefa.europa_1.json").read_text())
    assert cached["corners"] == [6, 2]
    assert not (tmp_path / "cups/corners/uefa.europa_2.json").exists()  # not final yet
    log.clear()
    got2, stats2 = cc.fetch_corners(events, tmp_path, get, today=today, workers=2)
    assert got2 == got and log == ["2"] and stats2["cached"] == 2


def test_fetch_corners_stops_after_repeated_failures(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "MAX_FAILURES", 2)
    monkeypatch.setattr(cc, "RETRIES", 0)
    today = pd.Timestamp("2026-10-20", tz="UTC")
    events = [
        {
            "id": str(i),
            "slug": "uefa.champions",
            "kickoff": today - pd.Timedelta(days=5),
            "home": "H",
            "away": "A",
            "completed": True,
        }
        for i in range(10)
    ]
    calls = []

    def get(url, params=None, timeout=None):
        calls.append(1)
        raise requests.ConnectionError("down")

    got, stats = cc.fetch_corners(events, tmp_path, get, today=today, workers=1)
    assert got == {} and stats["stopped"] and len(calls) == 2


def test_cup_frame_reads_events_counts_and_keys():
    t = pd.Timestamp("2025-07-10 18:00", tz="UTC")
    events = [
        {
            "id": "1",
            "cup": "UECL",
            "slug": "uefa.europa.conf_qual",
            "kickoff": t,
            "home": "Sturm Graz",
            "away": "Qarabag",
            "home_id": "5",
            "away_id": "6",
            "completed": True,
            "neutral": False,
        },
        {
            "id": "2",
            "cup": "UECL",
            "slug": "uefa.europa.conf",
            "kickoff": t,
            "home": "A",
            "away": "B",
            "completed": False,
        },
    ]
    f = cc.cup_frame(events, {"1": (7, 3)}, {"Sturm Graz": {"key": "Austria:Sturm Graz"}})
    assert len(f) == 1
    r = f.iloc[0]
    assert r["home_key"] == "Austria:Sturm Graz" and r["away_key"] == "UEFA:6"
    assert r["season"] == 2025 and r["home_corners"] == 7 and r["kickoff"].tz is None
    assert cc.season_of(pd.Timestamp("2025-06-25")) == 2025
    assert cc.season_of(pd.Timestamp("2026-05-27")) == 2025


# ---------- the whole run, offline ----------


def fake_espn_world(seed=3):
    """ESPN scoreboards (main slugs only; qualifying slugs empty) and summaries for cup
    matches every 14 days from Sep 2023 to Jan 2027 (finished before 19 Oct 2026)."""
    rng = np.random.default_rng(seed)
    days: dict[str, dict[str, list]] = {}
    summaries = {}
    eid = 5000
    names = CLUBS["E0"][:6] + CLUBS["P1"][:6] + ["Sturm Graz", "Rapid Vienna", "Qarabag"]

    def team(n, side, score):
        return {"homeAway": side, "score": score, "team": {"id": f"t{n}", "displayName": n}}

    def stat(n, c):
        return {
            "team": {"id": f"t{n}"},
            "statistics": [{"name": "wonCorners", "displayValue": str(c)}],
        }

    for d in pd.date_range("2023-09-19 19:00", "2027-01-20", freq="14D", tz="UTC"):
        for slug in cc.CUPS.values():
            perm = rng.permutation(names)
            for h, a in ((perm[0], perm[1]), (perm[2], perm[3])):
                done = bool(d < pd.Timestamp("2026-10-19", tz="UTC"))
                ev = {
                    "id": str(eid),
                    "date": d.isoformat(),
                    "season": {"slug": "league-phase"},
                    "status": {"type": {"completed": done, "name": "STATUS_FULL_TIME"}},
                    "competitions": [{"competitors": [team(h, "home", "1"), team(a, "away", "0")]}],
                }
                days.setdefault(slug, {}).setdefault(f"{d:%Y%m%d}", []).append(ev)
                hc, ac = int(rng.poisson(5)), int(rng.poisson(4))
                summaries[str(eid)] = {"boxscore": {"teams": [stat(h, hc), stat(a, ac)]}}
                eid += 1

    def get(url, params=None, timeout=None):
        assert url.startswith("https://site.api.espn.com/"), url
        if url.endswith("/summary"):
            return Resp(summaries.get(params["event"], {}))
        slug = url.split("/soccer/")[1].split("/")[0]
        mine = days.get(slug, {})
        cal = [f"{x[:4]}-{x[4:6]}-{x[6:]}T07:00Z" for x in sorted(mine)]
        return Resp({"events": mine.get(params["dates"], []), "leagues": [{"calendar": cal}]})

    return get


def test_the_whole_run_offline(tmp_path, monkeypatch):
    from soccer_stats.edge import corner_recal, totals

    monkeypatch.setattr(totals, "N_BOOT", 300)
    monkeypatch.setattr(corner_recal, "N_BOOT", 300)
    monkeypatch.setattr(totals, "_boot_slope", lambda p, y, g: (0.0, 2.0))
    teams = {lg: set(n) for lg, n in CLUBS.items()} | {"AUT": {"Sturm Graz", "Rapid Vienna"}}
    cov_rows = [
        {"league": "E0", "season": "2526", "played": 10, "with_corners": 10, "columns": True},
        {"league": "AUT", "season": "all", "played": 5, "with_corners": 0, "columns": False},
    ]
    monkeypatch.setattr(
        cc,
        "load_domestic",
        lambda raw_dir=None: {"matches": DOM, "teams": teams, "coverage": cov_rows, "errors": {}},
    )
    today = pd.Timestamp("2026-10-20 08:00", tz="UTC")
    data = cc.load_all(tmp_path, fake_espn_world(), today=today, workers=2)
    assert data["espn"]["complete"] and data["espn"]["summaries"]["failures"] == 0
    assert data["mapping"]["Sturm Graz"]["key"] == "Austria:Sturm Graz"
    assert data["mapping"]["Qarabag"] is None
    cov = cc.coverage(data)
    assert cov["corner_leagues"] == ["E0"]
    assert all(r["with_corners"] == r["finished"] for r in cov["cups"])
    assert set(cov["league_phase_2026"]) == {"UCL", "UEL", "UECL"}
    assert "== Cup corners: coverage" in cc.coverage_report(cov)
    est = cc.estimate(data)
    assert set(est) == {"UCL", "UEL", "UECL"} and est["UCL"]["corners_season"] > 0
    assert "Live cost" in cc.estimate_report(est)
    res = cc.evaluate(data, 0.99583)
    assert res["seasons"] == [2024, 2025] and res["matches"] > 0
    assert set(res["verdict"]) == {"UCL", "UEL", "UECL"}
    assert "Verdict UCL" in cc.report(res)
    cur = cc.current_clubs(data)
    cup_only = cur["UCL"]["cup_only"] + cur["UEL"]["cup_only"] + cur["UECL"]["cup_only"]
    assert cur["UCL"]["clubs"] > 0 and "Qarabag" in cup_only  # many cup matches, no league
    assert "league-phase clubs" in cc.current_report(cur)
    # nothing from July 2026 is scored, though 2026/27's counts were read for coverage
    assert (cc.walk_forward(DOM, data["cup"])["kickoff"] < cc.SCORE_END).all()
    assert (tmp_path / "cups/corners").exists()
