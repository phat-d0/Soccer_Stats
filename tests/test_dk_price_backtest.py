"""Both live match rules at DraftKings' own historical prices (lead, 28 Oct)."""

import json

import pandas as pd

from soccer_stats import dk_price_backtest as dpb
from soccer_stats import lean_backtest as lb
from soccer_stats import trades as tr


def ts(s):
    return pd.Timestamp(s, tz="UTC")


def event(kickoff, home, away, books):
    return {
        "commence_time": kickoff,
        "home_team": home,
        "away_team": away,
        "bookmakers": [
            {
                "key": b,
                "markets": [
                    {
                        "key": "h2h",
                        "last_update": upd,
                        "outcomes": [
                            {"name": home, "price": p[0]},
                            {"name": "Draw", "price": p[1]},
                            {"name": away, "price": p[2]},
                        ],
                    }
                ],
            }
            for b, (p, upd) in books.items()
        ],
    }


def write_snap(raw, league, at, events):
    d = raw / "odds_history" / league
    d.mkdir(parents=True, exist_ok=True)
    body = {"requested": at.isoformat(), "timestamp": at.isoformat(), "data": events}
    (d / f"{at:%Y%m%dT%H%MZ}.json").write_text(json.dumps(body))


KO = "2024-09-14T14:00:00Z"
LOOK = ts("2024-09-12 14:00")
CLOSE = ts("2024-09-14 13:55")


def history(tmp_path, with_pinnacle_close=True):
    look = {
        "draftkings": ((1.9, 3.6, 4.5), "2024-09-12T13:00:00Z"),
        "pinnacle": ((1.95, 3.7, 4.6), "2024-09-12T13:30:00Z"),
    }
    close = {"draftkings": ((1.8, 3.7, 5.0), "2024-09-14T13:50:00Z")}
    if with_pinnacle_close:
        close["pinnacle"] = ((1.85, 3.8, 5.2), "2024-09-14T13:52:00Z")
    write_snap(tmp_path, "E0", LOOK, [event(KO, "Arsenal", "Chelsea", look)])
    write_snap(tmp_path, "E0", CLOSE, [event(KO, "Arsenal", "Chelsea", close)])
    # A stale DraftKings quote at the look of another match: not a candidate.
    stale = {"draftkings": ((2.5, 3.3, 2.9), "2024-09-11T00:00:00Z")}
    write_snap(tmp_path, "E0", ts("2024-09-12 16:00"), [
        event("2024-09-14T16:30:00Z", "Everton", "Fulham", stale)
    ])  # fmt: skip
    return dpb.load_books("E0", {"Arsenal", "Chelsea", "Everton", "Fulham"}, tmp_path)


def merged_rows():
    base = {"season": "2425", "home_goals": 2, "away_goals": 1}
    return pd.DataFrame(
        [
            {**base, "date": "2024-09-14", "home": "Arsenal", "away": "Chelsea",
             "p_home": 0.60, "p_draw": 0.24, "p_away": 0.16,
             "odds_home": 2.0, "odds_draw": 3.6, "odds_away": 4.4,
             "close_home": 1.85, "close_draw": 3.8, "close_away": 5.2},
            {**base, "date": "2024-09-14", "home": "Everton", "away": "Fulham",
             "p_home": 0.40, "p_draw": 0.30, "p_away": 0.30,
             "odds_home": 2.6, "odds_draw": 3.3, "odds_away": 2.9,
             "close_home": 2.5, "close_draw": 3.3, "close_away": 3.0},
        ]
    )  # fmt: skip


def test_quote_takes_the_latest_snapshot_before_and_drops_stale_prices(tmp_path):
    hist = history(tmp_path)
    g = hist[hist["home"] == "Arsenal"].sort_values("snapshot_ts")
    assert dpb.quote(g, ts(KO) - dpb.LOOK, "draftkings") == {"home": 1.9, "draw": 3.6, "away": 4.5}
    assert dpb.quote(g, ts(KO), "pinnacle") == {"home": 1.85, "draw": 3.8, "away": 5.2}
    assert dpb.quote(g, LOOK - pd.Timedelta(hours=1), "draftkings") is None  # nothing before
    ev = hist[hist["home"] == "Everton"]
    assert dpb.quote(ev, ts("2024-09-14 14:30"), "draftkings") is None  # updated 2+ days before


def test_candidates_carry_draftkings_look_and_pinnacles_close_from_the_same_body(tmp_path):
    hist = history(tmp_path)
    target, cov = dpb.candidates(merged_rows(), hist, {"2425"})
    assert len(target) == 1 and cov["2425"] == {
        "matches": 2, "look": 1, "close": 1, "pinnacle_close": 1, "candidates": 1,
    }  # fmt: skip
    r = target.iloc[0]
    assert (r["odds_home"], r["close_home"], r["dk_close_home"]) == (1.9, 1.85, 1.8)
    assert abs(r["mkt_home"] + r["mkt_draw"] + r["mkt_away"] - 1) < 1e-9
    assert r["day"] == pd.Timestamp("2024-09-12 14:00") and r["y"] == 0
    assert dpb.candidates(merged_rows(), hist, {"2324"})[0].empty  # season not scored


def test_model_12_at_draftkings_price_with_both_clvs(tmp_path):
    hist = history(tmp_path)
    target, _ = dpb.candidates(merged_rows(), hist, {"2425"})
    from soccer_stats.lab import edge12_filters as ef

    b = dpb._with_dk_clv(ef.bets(target, "E0", "baseline"), target)
    assert len(b) == 1
    bet = b.iloc[0]
    assert bet["market"] == "home" and bet["odds"] == 1.9 and bet["won"]  # 0.60 x 1.9 - 1 = 14%
    assert abs(bet["clv"] - tr.clv(1.9, "home", {"home": 1.85, "draw": 3.8, "away": 5.2})) < 1e-12
    assert abs(bet["clv_dk"] - tr.clv(1.9, "home", {"home": 1.8, "draw": 3.7, "away": 5.0})) < 1e-12


def test_no_pinnacle_close_means_no_clv_and_it_is_counted(tmp_path):
    hist = history(tmp_path, with_pinnacle_close=False)
    target, cov = dpb.candidates(merged_rows(), hist, {"2425"})
    from soccer_stats.lab import edge12_filters as ef

    b = dpb._with_dk_clv(ef.bets(target, "E0", "baseline"), target)
    assert cov["2425"]["pinnacle_close"] == 0 and b["clv"].isna().all()
    block = dpb.clv_dk_block(b)
    assert block["pinnacle_close_missing"] == 1 and block["bets"] == 1


def tiny_league(tmp_path):
    hist = history(tmp_path)
    m = merged_rows()
    price_cols = [c for c in m.columns if c.startswith(("odds_", "close_"))]
    preds = m.drop(columns=price_cols)
    pins = m[["season", "date", "home", "away", *price_cols]]
    return dpb.league_reports("E0", preds, pins, hist)


def test_league_reports_in_the_pinnacle_files_shape(tmp_path):
    lr, lbets, er, ebets = tiny_league(tmp_path)
    for rep in (lr, er):
        assert rep["kind"] == "draftkings" and rep["seasons_included"] == ["2024/25"]
        c = rep["coverage"][0]
        assert (c["early_share"], c["close_share"], c["included"]) == (0.5, 0.5, True)
    assert lr["fits"] == 0 and lr["lean"]["bets"] == 0  # too few rows for a blend
    assert er["edge12"]["bets"] == 1 and ebets.iloc[0]["odds"] == 1.9  # DraftKings', not 2.0
    assert er["pinnacle_same_matches"]["edge12"]["bets"] == 1  # 0.60 x 2.0 at Pinnacle early
    assert er["clv_dk"]["bets"] == 1 and "p_blend" in ebets
    assert any("Model 12% at DraftKings: 1 bets" in line for line in dpb.headline("E0", lr, er))


def test_build_files_are_app_ready_and_pool_every_league(tmp_path):
    from soccer_stats import edge12_backtest as eb
    from soccer_stats import paper

    lr, lbets, er, ebets = tiny_league(tmp_path)
    sp1 = er.copy()
    lean = {"E0": (lr, lbets), "SP1": (lr, lbets.assign(league="SP1"))}
    e12 = {"E0": (er, ebets), "SP1": (sp1, ebets.assign(league="SP1"))}
    lf, ef = dpb.build_files(lean, e12, from_season=2019, names={"E0": "Premier League"})
    assert lf["kind"] == "lean_backtest" and ef["kind"] == eb.KIND  # the app's sections read them
    assert lf["book"] == ef["book"] == "draftkings" and "coverage_note" in lf
    assert ef["all"]["edge12"]["bets"] == 2 and ef["bets_total"] == 2
    assert "by_odds_band" in ef["leagues"]["E0"] and "sizing" in ef["leagues"]["E0"]
    assert ef["verdict"]["bets"] == 2 and ef["verdict"]["passes"] in (True, False)
    assert lf["verdict"]["live_rule"]["odds_range"] == list(tr.LEAN_ODDS_RANGE)
    assert "price" in lf["rule"] and "DraftKings" in lf["rule"]["price"]
    for rep in lf["leagues"].values():
        assert lb.OUTCOMES not in rep  # outcome rows are never saved
    sec = eb.app_section(json.loads(json.dumps(ef, default=str)))
    assert sec["available"] and sec["bets_shown"] == 2
    assert paper._dk_section({"available": False, "note": "x"})["note"] == paper.DK_MISSING_NOTE


def test_unmatched_feed_names_are_listed(tmp_path):
    history(tmp_path)
    missing = dpb.unmatched_names("E0", {"Arsenal", "Chelsea", "Everton"}, tmp_path)
    assert missing == {"Fulham": 1}


def test_serie_a_feed_names_land_on_football_data():
    from soccer_stats.odds_feed import _team

    known = {"Inter", "Milan", "Verona", "Roma"}
    for feed, ours in {
        "Inter Milan": "Inter",
        "FC Internazionale": "Inter",
        "Hellas Verona FC": "Verona",
        "Hellas Verona": "Verona",
        "AC Milan": "Milan",
    }.items():
        assert _team(feed, known) == ours


def test_bundesliga_ligue_1_and_championship_feed_names_land_on_football_data():
    from soccer_stats.odds_feed import _team

    known = {"Darmstadt", "St Pauli", "St Etienne", "West Brom", "West Ham"}
    for feed, ours in {
        "SV Darmstadt 98": "Darmstadt",
        "FC St. Pauli": "St Pauli",
        "St. Pauli": "St Pauli",
        "Saint Etienne": "St Etienne",
        "Saint-Etienne": "St Etienne",
        "West Bromwich": "West Brom",
        "West Bromwich Albion": "West Brom",
    }.items():
        assert _team(feed, known) == ours
