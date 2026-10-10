"""Live team corners: Pinnacle snapshots, model (f) on the cards, and the corners paper
portfolio (owner's live test, 10 Oct). No network."""

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from test_corners2 import _league

from soccer_stats import corners_live as cl
from soccer_stats import odds_log as ol
from soccer_stats import paper, publish
from soccer_stats import team_totals as tt
from soccer_stats import trades as tr
from soccer_stats.edge import corners2

K = pd.Timestamp("2026-10-17 14:00", tz="UTC")  # Saturday 14:00
LOOK = K - pd.Timedelta(hours=24)
DAY_BEFORE = LOOK - pd.Timedelta(days=1)
# P(corners <= k), k = 0..15: P(<= 4) = 0.40, so P(over 4.5) = 0.60; P(== 5) = 0.15.
HOME_CDF = [0.01, 0.04, 0.10, 0.22, 0.40, 0.55, 0.70, 0.82, 0.90, 0.95, 0.98, 0.99]
HOME_CDF += [0.995, 0.998, 0.999, 1.0]
AWAY_CDF = [0.03, 0.12, 0.28, 0.48, 0.66, 0.80, 0.89, 0.95, 0.98, 0.99, 1.0, 1.0]
AWAY_CDF += [1.0, 1.0, 1.0, 1.0]


def card(home="Arsenal", away="Leeds", league="E0", kickoff=K, model=True):
    c = {"league": league, "home": home, "away": away, "kickoff": kickoff.isoformat()}
    if model:
        c["corners"] = {
            "model": "f",
            "home": {"mean": 5.6, "cdf": HOME_CDF},
            "away": {"mean": 3.9, "cdf": AWAY_CDF},
        }
    return c


def dk_cache(raw, league="E0", events=(("ev1", "Arsenal", "Leeds United", K),), credits=20000):
    body = [
        {"id": i, "home_team": h, "away_team": a, "commence_time": k.isoformat()}
        for i, h, a, k in events
    ]
    (raw / f"odds_api_{league}_draftkings.json").write_text(json.dumps(body))
    meta = {"fetched_at": (K - pd.Timedelta(days=2)).isoformat(), "credits_left": credits}
    (raw / f"odds_api_{league}_draftkings.meta.json").write_text(json.dumps(meta))


def body(home="Arsenal", away="Leeds United", extra_book=True):
    def outcomes(team, line, o, u):
        return [
            {"name": "Over", "description": team, "price": o, "point": line},
            {"name": "Under", "description": team, "price": u, "point": line},
        ]

    books = [
        {
            "key": "pinnacle",
            "last_update": (K - pd.Timedelta(hours=25)).isoformat(),
            "markets": [
                {
                    "key": cl.MARKET,
                    "outcomes": outcomes(home, 4.5, 2.0, 1.85) + outcomes(away, 3.5, 1.95, 1.9),
                }
            ],
        }
    ]
    if extra_book:  # anything else in a body is ignored
        books.append({"key": "fanduel", "markets": [{"key": cl.MARKET, "outcomes": []}]})
    return {"id": "ev1", "bookmakers": books}


class Resp:
    def __init__(self, b, left=19999, cost="1", status=200):
        self.b, self.status_code, self.ok = b, status, status == 200
        self.headers = {"x-requests-remaining": str(left)}
        if cost is not None:
            self.headers["x-requests-last"] = cost

    def json(self):
        return self.b


def fake_get(calls, b=None, **kw):
    def get(url, params=None, timeout=None):
        calls.append((url, dict(params)))
        return Resp(b if b is not None else body(), **kw)

    return get


def quote(line=4.5, over=2.0, under=1.85, side="home", at=LOOK, snapshot="look", **kw):
    r = {
        "market": "team_corners",
        "league": "E0",
        "home": "Arsenal",
        "away": "Leeds",
        "kickoff": K.isoformat(),
        "event_id": "ev1",
        "snapshot": snapshot,
        "book": "pinnacle",
        "team": "Arsenal" if side == "home" else "Leeds",
        "side": side,
        "line": line,
        "over": over,
        "under": under,
        "downloaded_at": at.isoformat(),
        "fetched_at": at.isoformat(),
        "minutes_before": (K - at) / pd.Timedelta(minutes=1),
    }
    fair = tr.devig_pair(over, under)
    r.update(fair_over=fair[0], fair_under=fair[1], margin=1 / over + 1 / under - 1)
    r.update(kw)
    return r


# ---------- model (f), live ----------


def test_live_fit_matches_the_bakeoffs_f():
    data = corners2.frame(_league(seasons=range(2014, 2018)).sort_values("date").reset_index())
    cut = pd.Timestamp("2017-01-01")
    train, test = data[data["time"] < cut], data[data["time"] >= cut].iloc[:40]
    pred = corners2.Bakeoff2().fit(train).predict(test)
    n = corners2.N
    f = pred[:, 2 * 2 * n : 3 * 2 * n]  # CANDS order a, b, f, ...
    m = cl.TeamCornersF().fit(data, now=cut.tz_localize("UTC"))
    ph, pa = m.pmfs(test["home"], test["away"])
    np.testing.assert_allclose(ph, f[:, :n], atol=1e-9)
    np.testing.assert_allclose(pa, f[:, n:], atol=1e-9)


def test_live_fit_uses_earlier_matches_only():
    df = _league(seasons=range(2014, 2018))
    now = pd.Timestamp("2017-01-01", tz="UTC")
    a = cl.TeamCornersF().fit(df, now=now)
    later = df.copy()
    late = later["date"] >= now.tz_convert(None)
    later.loc[late, ["home_corners", "away_corners"]] = 25  # absurd future counts
    b = cl.TeamCornersF().fit(later, now=now)
    teams = sorted(set(df["home"]))[:6]
    np.testing.assert_array_equal(a.rates(teams, teams[::-1])[0], b.rates(teams, teams[::-1])[0])
    assert a.window_[1] < now.tz_convert(None)
    assert (a.window_[1] - a.window_[0]).days <= cl.TRAIN_DAYS
    with pytest.raises(ValueError):  # too few matches: no model rather than a thin one
        cl.TeamCornersF().fit(df.iloc[:50], now=now)


def _write_seasons(raw, league, df):
    for code, g in df.groupby("season"):
        out = pd.DataFrame(
            {
                "Date": g["date"].dt.strftime("%d/%m/%Y"),
                "HomeTeam": g["home"],
                "AwayTeam": g["away"],
                "FTHG": 1,
                "FTAG": 0,
                "HC": g["home_corners"],
                "AC": g["away_corners"],
            }
        )
        out.to_csv(raw / f"{league}_{code}.csv", index=False)


def test_add_model_puts_each_teams_distribution_on_the_card(tmp_path):
    df = _league(seasons=range(2024, 2027))
    _write_seasons(tmp_path, "E0", df[df["date"] < "2026-10-01"])
    now = pd.Timestamp("2026-10-10", tz="UTC")
    cards = [card("T1", "T2", model=False), card("T3", "T4", league="SP1", model=False)]
    fits = cl.add_model(cards, raw_dir=tmp_path, now=now, seasons=[2024, 2025, 2026])
    assert fits["E0"]["matches"] >= cl.MIN_TRAIN and fits["E0"]["cards"] == 1
    assert "error" in fits["SP1"] and "corners" not in cards[1]  # no files: no model, no crash
    c = cards[0]["corners"]
    assert c["model"] == "f" and set(c) == {"model", "home", "away"}
    for side in ("home", "away"):
        cdf = c[side]["cdf"]
        assert len(cdf) == cl.CDF_MAX + 1 and all(np.diff(cdf) >= 0) and cdf[-1] <= 1
        assert c[side]["mean"] > 0
    json.dumps(cards)


def test_side_probs_and_the_pick():
    p = cl.side_probs(HOME_CDF, 4.5)
    assert p == pytest.approx({"over": 0.60, "under": 0.40, "push": 0.0})
    w = cl.side_probs(HOME_CDF, 5.0)  # whole line: push at exactly 5
    assert w == pytest.approx({"over": 0.45, "under": 0.40, "push": 0.15})
    assert cl.side_probs(HOME_CDF, 4.25) is None and cl.side_probs(None, 4.5) is None
    pk = cl.pick(HOME_CDF, 4.5, 2.0, 1.85, 0.12)
    assert pk["side"] == "over" and pk["edge"] == pytest.approx(0.20)
    assert cl.pick(HOME_CDF, 4.5, 1.80, 1.85, 0.12) is None  # 8% edge: below the rule
    assert cl.pick(HOME_CDF, 4.5, 1.866, 2.0, 0.12) is None  # 11.96%: just below
    assert cl.pick(HOME_CDF, 4.5, 1.12 / 0.6, 2.0, 0.12)["side"] == "over"  # exactly 12%
    pw = cl.pick(HOME_CDF, 5.0, 2.6, 1.5, 0.12)  # 0.45 * 2.6 + 0.15 - 1 = 0.32
    assert pw["side"] == "over" and pw["edge"] == pytest.approx(0.32) and pw["p_push"] == 0.15


# ---------- fetching ----------


def test_rows_are_pinnacle_team_corners_with_the_model(tmp_path):
    rows = cl.rows_from_body(body(), card(), "ev1", "look", LOOK)
    assert len(rows) == 2 and {r["market"] for r in rows} == {"team_corners"}
    home = next(r for r in rows if r["side"] == "home")
    assert home["book"] == "pinnacle" and home["team"] == "Arsenal" and home["line"] == 4.5
    assert home["fair_over"] + home["fair_under"] == pytest.approx(1)
    assert home["margin"] == pytest.approx(1 / 2.0 + 1 / 1.85 - 1, abs=1e-6)
    assert home["p_model_over"] == pytest.approx(0.60)
    assert home["minutes_before"] == 1440.0 and home["snapshot"] == "look"


def test_run_fetches_each_snapshot_once_in_every_league(tmp_path):
    raw, out = tmp_path / "raw", tmp_path / "out"
    raw.mkdir()
    dk_cache(raw)
    dk_cache(raw, "E1", events=(("ev9", "West Ham", "QPR", K),))
    calls = []
    cards = [card(), card("West Ham", "QPR", league="E1")]
    s = cl.run(cards, out, raw_dir=raw, api_key="k", now=LOOK, get=fake_get(calls))
    assert s["calls"] == 2 and s["credits"] == 2 and s["stop"] is None
    url, params = calls[0]
    assert "/events/ev1/odds" in url and params["bookmakers"] == "pinnacle"
    assert params["markets"] == "alternate_team_totals_corners"
    assert "/soccer_efl_champ/events/ev9/odds" in calls[1][0]  # E1: its own call
    rec = [json.loads(x) for x in (out / "calls.jsonl").read_text().splitlines()]
    assert {(c["event_id"], c["snapshot"], c["group"]) for c in rec} == {
        ("ev1", "look", "team_corners"),
        ("ev9", "look", "team_corners"),
    }
    again = cl.run(cards, out, raw_dir=raw, api_key="k", now=LOOK, get=fake_get(calls))
    assert again["calls"] == 0 and len(calls) == 2  # the local record dedupes
    fresh = tmp_path / "raw2"  # a new cache: data-log's record still dedupes
    fresh.mkdir()
    dk_cache(fresh)
    state = tmp_path / "state"
    state.mkdir()
    (state / "corners_calls_2026-10.jsonl").write_text((out / "calls.jsonl").read_text())
    s3 = cl.run([card()], out, [state], raw_dir=fresh, api_key="k", now=LOOK, get=fake_get(calls))
    assert s3["calls"] == 0 and len(calls) == 2


def _record(path, cost, credits=None, at=DAY_BEFORE, name="corners_calls"):
    row = {"event_id": "old", "snapshot": "look", "fetched_at": at.isoformat(), "cost": cost}
    if credits is not None:
        row["credits_left"] = credits
    (path / f"{name}_{at:%Y-%m}.jsonl").write_text(json.dumps(row) + "\n")


@pytest.mark.parametrize("spent, allowed", [(499, True), (500, False)])
def test_own_monthly_cap(tmp_path, spent, allowed):
    raw, state = tmp_path / "raw", tmp_path / "state"
    raw.mkdir()
    state.mkdir()
    dk_cache(raw)
    _record(state, spent)
    calls = []
    s = cl.run(
        [card()], tmp_path / "o", [state], raw_dir=raw, api_key="k", now=LOOK, get=fake_get(calls)
    )
    assert (len(calls) == 1) == allowed
    if not allowed:
        assert s["stop"].startswith("monthly cap: 500 of 500")


def test_team_total_spend_never_counts_toward_the_corner_cap_or_back(tmp_path):
    raw, tts, cs = tmp_path / "raw", tmp_path / "tt", tmp_path / "cs"
    for d in (raw, tts, cs):
        d.mkdir()
    dk_cache(raw)
    _record(tts, 800, name="team_totals_calls")  # the team-total test's month is full
    calls = []
    s = cl.run(
        [card()],
        tmp_path / "o",
        [cs],
        raw_dir=raw,
        api_key="k",
        now=LOOK,
        get=fake_get(calls),
        balance_dirs=[tts],
    )
    assert s["calls"] == 1 and s["month_spent"] == 1
    _record(cs, 499)
    assert tt.month_spend(tt.load_calls(cs), LOOK) == 0  # corners never in the 800
    assert tt.month_spend(cl.load_calls(tts), LOOK) == 0


def test_reserve_on_the_freshest_balance(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    dk_cache(raw, credits=3000)
    calls = []
    s = cl.run([card()], tmp_path / "o", raw_dir=raw, api_key="k", now=LOOK, get=fake_get(calls))
    assert calls == [] and s["stop"].startswith("reserve")
    raw2, tts = tmp_path / "raw2", tmp_path / "tt"
    raw2.mkdir()
    tts.mkdir()
    dk_cache(raw2, credits=20000)  # an old meta, but a newer team-total call saw 2,999
    _record(tts, 2, credits=2999, at=LOOK - pd.Timedelta(hours=1), name="team_totals_calls")
    s = cl.run(
        [card()],
        tmp_path / "o",
        raw_dir=raw2,
        api_key="k",
        now=LOOK,
        get=fake_get(calls),
        balance_dirs=[tts],
    )
    assert calls == [] and "2999" in s["stop"]


@pytest.mark.parametrize(
    "cost, status, want",
    [("1", 200, 1), (None, 200, 1), (None, 500, 0), ("0", 200, 0)],
)
def test_cost_comes_from_the_header(tmp_path, cost, status, want):
    raw = tmp_path / "raw"
    raw.mkdir()
    dk_cache(raw)
    out = tmp_path / "o"
    s = cl.run(
        [card()],
        out,
        raw_dir=raw,
        api_key="k",
        now=LOOK,
        get=fake_get([], cost=cost, status=status, left=18000),
    )
    rec = json.loads((out / "calls.jsonl").read_text())
    assert rec["cost"] == want == s["credits"] and rec["credits_left"] == 18000
    assert rec["rows"] == (2 if status == 200 else 0)


def test_a_crash_part_way_keeps_the_calls_and_the_key_stays_out(tmp_path):
    raw, out = tmp_path / "raw", tmp_path / "o"
    raw.mkdir()
    events = (("ev1", "Arsenal", "Leeds United", K), ("ev2", "Chelsea", "Burnley", K))
    dk_cache(raw, events=events)
    n = []

    def get(url, params=None, timeout=None):
        n.append(url)
        if len(n) == 2:
            raise RuntimeError("boom https://x?apiKey=SECRETKEY")
        return Resp(body())

    cards = [card(), card("Chelsea", "Burnley")]
    s = cl.run(cards, out, raw_dir=raw, api_key="SECRETKEY", now=LOOK, get=get)
    assert s["stop"] == "error (RuntimeError)" and s["calls"] == 1
    assert len((out / "calls.jsonl").read_text().splitlines()) == 1
    for f in [*out.iterdir(), raw / cl.LOCAL_CALLS]:
        assert "SECRETKEY" not in f.read_text()
    assert "SECRETKEY" not in cl.summary_line(s)
    assert cl.run(cards, out, raw_dir=raw, api_key="", now=LOOK)["stop"] == "no ODDS_API_KEY"


# ---------- data-log, and kept apart from the team totals ----------


def test_log_writes_corner_files_deduplicated_and_apart(tmp_path):
    raw, pend, root = tmp_path / "raw", tmp_path / "pend", tmp_path / "log"
    raw.mkdir()
    dk_cache(raw)
    cl.run([card()], pend, raw_dir=raw, api_key="k", now=LOOK, get=fake_get([]))
    assert cl.log(pend, root) == (2, 1)
    assert cl.log(pend, root) == (0, 0)
    names = sorted(p.name for p in (root / "odds_log").iterdir())
    assert names == ["E0_corners_2026-10.jsonl", "corners_calls_2026-10.jsonl"]
    assert tt.load_rows(root).empty  # the team-total test never reads them
    assert tt.load_calls(root / "odds_log") == []
    assert ol.load(root, "E0").empty  # nor the DraftKings log
    assert len(cl.load_rows(root / "odds_log")) == 2
    assert len(cl.load_calls(root / "odds_log")) == 1


def test_team_total_readers_ignore_corner_rows(tmp_path):
    d = tmp_path / "state"
    d.mkdir()
    rows = [quote(), quote(side="away", line=3.5, over=1.95, under=1.9)]
    tt_row = {**quote(), "market": "team_totals", "book": "fanduel", "line": 1.5}
    (d / "E0_corners_2026-10.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    # Corner rows even inside a team-total file or a pending rows.jsonl are skipped.
    (d / "E0_team_totals_2026-10.jsonl").write_text(json.dumps(rows[0]) + "\n")
    (d / "rows.jsonl").write_text(json.dumps(rows[1]) + "\n" + json.dumps(tt_row) + "\n")
    cards = [{"league": "E0", "home": "Arsenal", "away": "Leeds", "kickoff": K.isoformat()}]
    assert publish.add_team_totals(cards, [d]) == 1
    assert [x["line"] for x in cards[0]["team_totals"]["home"]] == [1.5]  # the FanDuel row only
    prog = publish.team_total_progress([d], pd.DataFrame())
    assert prog["rows"] == 1 and prog["looks"] == 1
    corner_only = pd.DataFrame(rows)
    rep = tt.market_report(corner_only, pd.DataFrame(), pd.DataFrame())
    assert rep["plumbing"]["look_rows"] == 0 and all(v == 0 for v in rep["matches"].values())
    assert tt.pairs(corner_only, pd.DataFrame({"x": [1]})).empty


def test_add_quotes_shows_the_newest_quote_before_kickoff_with_the_pick(tmp_path):
    d = tmp_path / "state"
    d.mkdir()
    rows = [
        quote(over=1.7, under=2.1, at=LOOK),
        quote(over=2.0, under=1.85, at=K - pd.Timedelta(minutes=20), snapshot="close"),
        quote(over=3.0, under=1.3, at=K + pd.Timedelta(minutes=5), snapshot="close"),  # late
    ]
    (d / "E0_corners_2026-10.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    cards = [card(), card("Chelsea", "Burnley")]
    assert cl.add_quotes(cards, [d, None]) == 1
    pin = cards[0]["corners"]["pinnacle"]
    assert pin["book"] == "pinnacle" and pin["away"] == []
    (q,) = pin["home"]
    assert q["over"] == 2.0 and q["snapshot"] == "close" and q["p_over"] == pytest.approx(0.6)
    assert q["pick"]["side"] == "over" and q["pick"]["edge"] == pytest.approx(0.2)
    assert "pinnacle" not in cards[1].get("corners", {})


# ---------- the corners paper portfolio ----------


def _book(tmp_path, cards, rows, now, results=None):
    ledger = paper.load_corner_ledger(tmp_path, "E0")
    ev = paper.update_corner_ledger(ledger, cards, rows, results, now, "E0")
    paper.append_corner_events(tmp_path, ev)
    return paper.load_corner_ledger(tmp_path, "E0"), ev


def corner_results(hc=7, ac=3, date="2026-10-17"):
    return pd.DataFrame(
        {
            "league": ["E0"],
            "season": ["2627"],
            "date": [pd.Timestamp(date)],
            "home": ["Arsenal"],
            "away": ["Leeds"],
            "home_corners": [hc],
            "away_corners": [ac],
        }
    )


TID = "E0|2627|Arsenal|Leeds|corners|home|4.5"


def test_opens_one_trade_per_team_line_on_a_fresh_quote(tmp_path):
    now = LOOK + pd.Timedelta(minutes=10)
    rows = [quote(), quote(side="away", line=3.5, over=1.95, under=1.9)]
    led, ev = _book(tmp_path, [card()], rows, now)
    assert [e["type"] for e in ev] == ["open"]  # the away line has no 12% edge
    t = led[TID]
    assert t["market"] == "team_corners" and t["team_side"] == "home" and t["team"] == "Arsenal"
    assert t["side"] == "over" and t["line"] == 4.5 and t["odds"] == 2.0
    assert t["model_p"] == pytest.approx(0.6) and t["edge"] == pytest.approx(0.2)
    assert (t["threshold"], t["rule"], t["p_source"]) == (0.12, "fixed_raw", "model_f")
    assert t["portfolio"] == "corners" and tr.portfolio_of(t) == "corners"
    assert t["stake"] == 10.0 and t["look"] == "look" and t["bookmaker"] == "pinnacle"
    assert set(tr.CORNER_ENTRY_FIELDS) <= set(t)
    _, ev2 = _book(tmp_path, [card()], rows, now + pd.Timedelta(minutes=30))
    assert not any(e["type"] == "open" for e in ev2)  # never twice
    assert (tmp_path / "paper_trades" / "E0_corners_2627.jsonl").exists()
    assert paper.load_ledger(tmp_path) == {}  # the match ledger never sees corner trades


@pytest.mark.parametrize(
    "now, kickoff, why",
    [
        (LOOK + pd.Timedelta(hours=3, minutes=1), K, "stale quote"),
        (K + pd.Timedelta(minutes=1), K, "past kickoff"),
        (LOOK - pd.Timedelta(minutes=1), K, "quote from the future"),
    ],
)
def test_no_trade_on_a_stale_quote_or_after_kickoff(tmp_path, now, kickoff, why):
    _, ev = _book(tmp_path, [card(kickoff=kickoff)], [quote()], now)
    assert ev == [], why


def test_no_trade_without_the_model_or_the_card(tmp_path):
    now = LOOK + pd.Timedelta(minutes=10)
    assert _book(tmp_path / "a", [card(model=False)], [quote()], now)[1] == []
    assert _book(tmp_path / "b", [card("Chelsea", "Burnley")], [quote()], now)[1] == []
    assert _book(tmp_path / "c", [card()], [quote(line=4.25)], now)[1] == []  # quarter line


def test_close_snapshot_sets_clv_and_the_count_settles(tmp_path):
    _book(tmp_path, [card()], [quote()], LOOK + pd.Timedelta(minutes=10))
    close = quote(over=1.8, under=2.05, at=K - pd.Timedelta(minutes=20), snapshot="close")
    after = quote(over=1.5, under=2.6, at=K + pd.Timedelta(minutes=10), snapshot="close")
    led, ev = _book(tmp_path, [card()], [quote(), close, after], K - pd.Timedelta(minutes=15))
    t = led[TID]
    fair = tr.devig_pair(1.8, 2.05)[0]
    assert t["close_odds"] == 1.8 and t["close_snapshot"] == "close"
    assert t["clv_pinnacle"] == pytest.approx(2.0 * fair - 1) and t["beat_close_pinnacle"]
    assert t["close_minutes_before"] == 20.0
    led, _ = _book(tmp_path, [], [quote(), close], K + pd.Timedelta(hours=3), corner_results(7))
    t = led[TID]
    assert t["status"] == "won" and t["profit"] == 10.0 and t["actual"] == 7
    assert t["score"] == "7-3 corners" and t["clv_pinnacle"] == pytest.approx(2.0 * fair - 1)


@pytest.mark.parametrize(
    "line, side, count, status, profit",
    [
        (4.5, "over", 4, "lost", -10.0),
        (4.5, "under", 4, "won", 8.5),
        (5.0, "over", 5, "void", 0.0),  # a whole line that lands exactly: push
        (5.0, "under", 5, "void", 0.0),
        (5.0, "over", 6, "won", 15.0),
    ],
)
def test_settle_corner(line, side, count, status, profit):
    odds = 2.5 if line == 5.0 else (2.0 if side == "over" else 1.85)
    t = {"line": line, "side": side, "odds": odds, "stake": 10.0}
    s = tr.settle_corner(t, count)
    assert s["status"] == status and s["profit"] == pytest.approx(profit)
    assert s["push"] == (line == count)
    assert tr.settle_corner(t, None)["status"] == "void"


def test_whole_line_push_through_the_ledger_and_void_without_a_result(tmp_path):
    now = LOOK + pd.Timedelta(minutes=10)
    q = quote(line=5.0, over=2.6, under=1.5)
    led, ev = _book(tmp_path / "a", [card()], [q], now)
    tid = "E0|2627|Arsenal|Leeds|corners|home|5"
    assert led[tid]["side"] == "over" and led[tid]["edge"] == pytest.approx(0.32)
    led, _ = _book(tmp_path / "a", [], [q], K + pd.Timedelta(hours=3), corner_results(5))
    assert led[tid]["status"] == "void" and led[tid]["push"] and led[tid]["profit"] == 0.0
    _book(tmp_path / "b", [card()], [quote()], now)
    led, _ = _book(
        tmp_path / "b", [], [quote()], K + pd.Timedelta(days=3), corner_results(date="2026-11-30")
    )
    assert led[TID]["status"] == "void" and not led[TID]["push"]  # match moved: void
    _book(tmp_path / "c", [card()], [quote()], now)
    led, _ = _book(tmp_path / "c", [], [quote()], K + pd.Timedelta(days=2))
    assert led[TID]["status"] == "open"  # waiting for the result
    led, _ = _book(tmp_path / "c", [], [quote()], K + pd.Timedelta(days=15))
    assert led[TID]["status"] == "void"


def test_corner_clv():
    fair = tr.devig_pair(1.9, 1.9)
    assert tr.corner_clv(2.0, "over", 1.9, 1.9) == pytest.approx(2.0 * fair[0] - 1)
    assert tr.corner_clv(2.0, "under", None, 1.9) is None


def test_paper_run_shows_corners_as_their_own_portfolio(tmp_path):
    from test_paper import card as match_card
    from test_paper import src

    now = LOOK + pd.Timedelta(minutes=10)
    od = tmp_path / "odds_log"
    od.mkdir()
    rows = [quote(), {**quote(), "league": "SP1", "home": "Betis", "away": "Getafe"}]
    (od / "E0_corners_2026-10.jsonl").write_text(json.dumps(rows[0]) + "\n")
    (od / "SP1_corners_2026-10.jsonl").write_text(json.dumps(rows[1]) + "\n")
    m = match_card(kickoff=K, league="E0")
    m["corners"] = card()["corners"]
    sp = {**card("Betis", "Getafe", league="SP1"), "p": m["p"], "odds": {}, "low_data": False}
    data = {
        "fixtures": [m, sp],
        "odds_source": src(now - pd.Timedelta(minutes=30)),
        "portfolio": {},
    }
    data["odds_sources"] = {"SP1": {"name": "DraftKings", "fetched_at": None}}
    n = paper.run(data, tmp_path, None, now=now)
    pfs = {p["id"]: p for p in data["portfolio"]["portfolios"]}
    c = pfs["corners"]
    assert c["status"] == "testing" and c["live"]["rule"]["p_source"] == "model_f"
    assert c["live"]["summary"]["trades"] == 2 and c["live"]["error"] is None
    assert set(c["live"]["by_league"]) == {"E0", "SP1"}
    assert c["backtest"]["kind"] == "research" and "summary" not in c["backtest"]
    dev = c["backtest"]["development"]
    assert {r["league"] for r in dev["rows"]} == {"E0", "SP1", "D1", "I1", "F1", "E1"}
    assert {r["league"] for r in dev["rows"] if r["passes"]} == {"E0", "I1", "F1"}
    assert c["backtest"]["holdout"] and c["backtest"]["recalibration"]
    ml = pfs["moneyline"]["live"]["trades"]
    assert all(t["market"] != "team_corners" for t in ml)  # moneyline keeps its own trades
    assert all(t.get("market") != "team_corners" for t in data["portfolio"]["live"]["trades"])
    assert n == len(ml) + 2  # the match opens plus the two corner opens
    json.dumps(data)

    off = {"fixtures": [m], "portfolio": {}}  # no ledger (a branch build): says why
    paper.run(off, None, None, now=now)
    oc = next(p for p in off["portfolio"]["portfolios"] if p["id"] == "corners")
    assert oc["live"]["error"] and oc["backtest"]["kind"] == "research"


def test_corners_research_matches_the_doc():
    r = paper.corners_research()
    rows = {x["league"]: x for x in r["development"]["rows"]}
    assert rows["E0"]["gain"] == 0.0471 and rows["E0"]["slopes"] == "0.86–0.98"
    assert rows["SP1"]["finalist"] is None and rows["F1"]["finalist"] == "f"
    assert r["development"]["level"] == "99.722%"
    text = (Path(__file__).resolve().parents[1] / "docs" / "totals.md").read_text()
    for x in rows.values():
        assert f"{x['gain']:+.4f} ({x['lo']:+.4f} to {x['hi']:+.4f}), slopes {x['slopes']}" in text


def test_estimate_counts_one_credit_a_snapshot():
    ks = [K, K + pd.Timedelta(days=7)]
    start = pd.Timestamp("2026-10-01", tz="UTC")
    e = cl.estimate({"E0": ks}, start, start + pd.offsets.MonthBegin(1))
    assert e["E0"] == {"matches": 2, "calls": 4, "credits": 4}  # look + close each


def test_publish_corners_never_stop_the_build(tmp_path, monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("no")

    monkeypatch.delenv("CORNERS_DIR", raising=False)
    monkeypatch.setenv("CORNERS_STATE", str(tmp_path))
    monkeypatch.setattr(cl, "add_model", boom)
    monkeypatch.setattr(cl, "add_quotes", boom)
    monkeypatch.setattr(cl, "run", boom)
    data = {"fixtures": [card(model=False)]}
    publish.add_corners(data)  # no exception
    out = capsys.readouterr().out
    assert "Corners model (f): failed (RuntimeError)" in out and data["corners_model"] is None
    assert "Corners shown: none (RuntimeError)" in out and "Corners (Pinnacle)" not in out
    monkeypatch.setenv("CORNERS_DIR", str(tmp_path / "pending"))
    publish.add_corners(data)
    assert "Corners (Pinnacle): failed (RuntimeError)" in capsys.readouterr().out


def test_corner_trades_belong_to_the_corners_portfolio():
    assert tr.portfolio_of({"market": "team_corners"}) == "corners"
    assert tr.portfolio_of({"bet_type": "corners", "market": "x"}) == "corners"
    pf = next(p for p in tr.PORTFOLIOS if p["id"] == "corners")
    assert pf["status"] == "testing" and "February" in pf["note"]
    rule = tr.corners_rule()
    assert rule["threshold"] == 0.12 and rule["rule"] == "fixed_raw" and rule["book"] == "pinnacle"
