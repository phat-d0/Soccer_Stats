"""The corner Blend Lean paper track (paper_corner_lean; owner, 24 Nov 2026): fits shared
with the gate, locked leagues never in a fit, no look-ahead, backcast then live, one bet per
match x team, closes per PR #258, settlement, the judged read and the paper.run wiring."""

import json

import numpy as np
import pandas as pd
import pytest

from soccer_stats import paper
from soccer_stats import paper_corner_lean as pcl
from soccer_stats import trades as tr
from soccer_stats.lab import corner_lean as lean

T0 = pd.Timestamp("2026-10-16T15:00:00Z")
LEAGUES = ("SP1", "D1", "E1", "E2", "E3", "E0")
N = 240  # matches; 200 in the fit leagues -> 400 lines, the minimum met near the end
DAYS = 36


def _match(i):
    lg = LEAGUES[i % len(LEAGUES)]
    k = T0 + pd.Timedelta(hours=int(i * DAYS * 24 / N))
    return lg, f"H{i}", f"A{i}", k


def _row(lg, h, a, k, side, line, over, under, at, snapshot, book="pinnacle", model=None):
    r = {
        "market": "team_corners",
        "league": lg,
        "home": h,
        "away": a,
        "kickoff": k.isoformat(),
        "event_id": f"ev-{h}",
        "snapshot": snapshot,
        "book": book,
        "team": h if side == "home" else a,
        "side": side,
        "line": line,
        "over": over,
        "under": under,
        "downloaded_at": at.isoformat(),
        "fetched_at": (at - pd.Timedelta(minutes=5)).isoformat(),
        "minutes_before": (k - at) / pd.Timedelta(minutes=1),
    }
    fair = tr.devig_pair(over, under) if over and under else None
    r.update(
        fair_over=fair[0] if fair else None,
        fair_under=fair[1] if fair else None,
        margin=(1 / over + 1 / under - 1) if fair else None,
        p_model_over=model,
    )
    return r


def synth(seed=3, flip=False):
    """Log rows and results: outcomes at 50% whatever Pinnacle's price says, so the fit
    flattens (b near 0) and bets the long side of lopsided lines."""
    rng = np.random.default_rng(seed)
    rows, res = [], []
    for i in range(N):
        lg, h, a, k = _match(i)
        look = k - pd.Timedelta(hours=20)
        counts = {}
        for side in ("home", "away"):
            fair = float(rng.uniform(0.15, 0.85))
            over, under = 1 / (fair + 0.03), 1 / (1 - fair + 0.03)
            model = float(np.clip(fair + rng.normal(0, 0.05), 0.05, 0.95))
            rows.append(_row(lg, h, a, k, side, 4.5, over, under, look, "look", model=model))
            rows.append(
                _row(lg, h, a, k, side, 4.5, over * 0.99, under * 1.01,
                     k - pd.Timedelta(minutes=15), "close", model=model)
            )  # fmt: skip
            rows.append(
                _row(lg, h, a, k, side, 4.5, over * 1.02, under * 1.02, look, "look",
                     book="draftkings")
            )  # fmt: skip
            won = rng.random() < 0.5
            counts[side] = (6 if won else 3) if not flip else (3 if won else 6)
        res.append(
            {"league": lg, "season": "2627", "date": k.tz_convert(None).normalize(),
             "home": h, "away": a, "home_corners": counts["home"], "away_corners": counts["away"]}
        )  # fmt: skip
    return rows, pd.DataFrame(res)


def write_log(tmp, rows):
    od = tmp / "odds_log"
    od.mkdir(exist_ok=True)
    for f in od.glob("*_corners_*.jsonl"):
        f.unlink()
    by: dict[str, list] = {}
    for r in rows:
        by.setdefault(r["league"], []).append(json.dumps(r))
    for lg, lines in by.items():
        (od / f"{lg}_corners_2026-11.jsonl").write_text("\n".join(lines) + "\n")


NOW = T0 + pd.Timedelta(days=DAYS + 2)


def run(tmp, rows, res, now=NOW, codes=LEAGUES):
    write_log(tmp, rows)
    return pcl.run(tmp, list(codes), now, res)


def test_fits_are_the_gate_schedule_on_looks_before_defined(tmp_path):
    rows, res = synth()
    out = run(tmp_path, rows, res)
    df = pcl.look_frame(rows, [res])
    gate = lean.fit_at_times(df, [t for t in df["look_at"].unique() if lean._ts(t) < NOW])
    assert out["fits"] and [f["fit_at"] for f in out["fits"]] == [f["fit_at"] for f in gate]
    for got, want in zip(out["fits"], gate, strict=True):
        assert got["source"] == "backcast"
        assert got["c"] == pytest.approx(want["c"]) and got["sigma"] == pytest.approx(want["sigma"])
    stored = json.loads((tmp_path / "paper_trades" / pcl.FITS_FILE).read_text())
    assert [f["fit_at"] for f in stored["fits"]] == [f["fit_at"] for f in out["fits"]]


def test_locked_leagues_never_enter_a_fit_and_their_counts_are_never_read(tmp_path):
    rows, res = synth()
    df = pcl.look_frame(rows, [res])
    assert df.loc[df["league"].isin(lean.LOCKED), "count"].isna().all()
    assert df.loc[df["league"].isin(lean.FIT_LEAGUES), "count"].notna().any()
    a = run(tmp_path / "a", rows, res) if (tmp_path / "a").mkdir() is None else None
    extreme = res.copy()
    locked = extreme["league"].isin(lean.LOCKED)
    extreme.loc[locked, ["home_corners", "away_corners"]] = 99
    b = run(tmp_path / "b", rows, extreme) if (tmp_path / "b").mkdir() is None else None
    assert [(f["fit_at"], f["c"]) for f in a["fits"]] == [(f["fit_at"], f["c"]) for f in b["fits"]]


def test_picks_in_every_league_backcast_one_per_team_with_their_fit(tmp_path):
    rows, res = synth()
    out = run(tmp_path, rows, res)
    main = [t for t in out["trades"] if t["strategy"] == pcl.KEY]
    assert main and all(t["backcast"] and t["backcast_from"] == pcl.BACKCAST_FROM for t in main)
    assert "E0" in {t["league"] for t in main}  # picks in the locked leagues too
    keys = [(t["home"], t["team_side"]) for t in main]
    assert len(keys) == len(set(keys))
    first = lean._ts(out["fits"][0]["fit_at"])
    for t in main:
        assert lean._ts(t["opened_at"]) >= first  # no pick before the first fit
        f = pcl.fit_for(out["fits"], lean._ts(t["opened_at"]))
        assert t["fit_at"] == f["fit_at"] and t["fit_c"] == pytest.approx(f["c"], abs=1e-6)
        assert t["z"] >= 1 and t["bet_type"] == pcl.BET_TYPE and t["portfolio"] == "corners"
        assert t["driver"] in ("market", "model") and t["p_source"] == pcl.P_SOURCE
        assert t["model_p"] == pytest.approx(t["p_bet"], abs=1e-4)
        assert t["id"].endswith(f"|cornerlean|{t['team_side']}")
    dk = [t for t in out["trades"] if t["strategy"] == pcl.DK_KEY]
    assert dk and all(t["id"].endswith("|dk") and t["variant_of"] == pcl.KEY for t in dk)


def test_stored_fits_and_entries_never_change_when_results_change_later(tmp_path):
    rows, res = synth()
    first = run(tmp_path, rows, res)
    _, flipped = synth(flip=True)
    later = run(tmp_path, rows, flipped, now=NOW + pd.Timedelta(hours=1))
    n = len(first["fits"])
    assert later["fits"][:n] == first["fits"]
    assert later["new_fits"] == 0  # not due: under 28 days since the first fit
    before = {t["id"]: t["side"] for t in first["trades"]}
    assert {t["id"]: t["side"] for t in later["trades"]} == before


def test_a_live_look_after_defined_opens_live_and_a_started_match_never_does(tmp_path):
    rows, res = synth()
    first = run(tmp_path, rows, res)
    k = NOW + pd.Timedelta(hours=20)
    look = NOW + pd.Timedelta(minutes=30)
    new = [
        _row("SP1", "Live", "Side", k, "home", 4.5, 1 / 0.2, 1 / 0.86, look, "look", model=0.2),
        _row("SP1", "Gone", "Past", look + pd.Timedelta(minutes=10), "home", 4.5, 1 / 0.2,
             1 / 0.86, look, "look", model=0.2),
    ]  # fmt: skip
    later = run(tmp_path, rows + new, res, now=look + pd.Timedelta(minutes=30))
    live = [t for t in later["trades"] if t["home"] == "Live" and t["strategy"] == pcl.KEY]
    assert len(live) == 1 and not live[0].get("backcast")
    assert live[0]["fit_at"] == first["fits"][-1]["fit_at"]
    assert live[0]["close_missing"] == paper.NO_CLOSE_AT_LINE
    assert not [t for t in later["trades"] if t["home"] == "Gone"]


def test_close_then_settle(tmp_path):
    rows, res = synth()
    out = run(tmp_path, rows, res)
    settled = [t for t in out["trades"] if t["strategy"] == pcl.KEY]
    t = settled[0]
    assert t["status"] in ("won", "lost") and t["close_snapshot"] == "close"
    assert t["clv_pinnacle"] is not None and not t.get("close_missing")
    assert t["result_source"] == "football-data"
    dk = next(x for x in out["trades"] if x["strategy"] == pcl.DK_KEY)
    assert dk["clv_pinnacle"] is not None  # Pinnacle's close at the same line (no DK close logged)


def test_a_look_entry_whose_line_has_no_close_keeps_no_close(tmp_path):
    rows, res = synth()
    rows = [r for r in rows if r["snapshot"] != "close"]
    out = run(tmp_path, rows, res)
    t = next(x for x in out["trades"] if x["strategy"] == pcl.KEY)
    assert t.get("close_odds") is None and t["close_missing"] == paper.NO_CLOSE_AT_LINE


def _jt(i, clv, opened="2026-12-01T10:00:00+00:00", status="won", backcast=False):
    return {
        "id": f"SP1|2627|H{i}|A{i}|cornerlean|home",
        "opened_at": opened,
        "kickoff": "2026-12-02T15:00:00+00:00",
        "status": status,
        "clv_pinnacle": clv,
        "backcast": backcast,
    }


def test_judged_waits_then_reads_once_and_freezes(tmp_path):
    defined = "2026-11-25T00:00:00+00:00"
    now = pd.Timestamp("2026-12-10", tz="UTC")
    few = [_jt(i, 0.01) for i in range(10)]
    r = pcl.judged(few, defined, now, tmp_path)
    assert r["verdict"] is None and r["bets"] == 10 and r["final"] == 10
    many = [_jt(i, 0.02) for i in range(300)] + [_jt(999, -0.5, opened="2026-11-20T00:00Z")]
    r = pcl.judged(many, defined, now, tmp_path)
    assert r["verdict"] == "pass" and r["read"] == 300 and r["frozen"]
    again = pcl.judged([_jt(i, -0.2) for i in range(300)], defined, now, tmp_path)
    assert again["verdict"] == "pass" and again["clv"] == pytest.approx(0.02)


def test_judged_on_the_deadline_with_too_few_is_inconclusive(tmp_path):
    defined = "2026-11-25T00:00:00+00:00"
    few = [_jt(i, 0.05) for i in range(40)]
    before = pcl.judged(few, defined, pd.Timestamp("2027-05-30", tz="UTC"), tmp_path)
    assert before["verdict"] is None
    r = pcl.judged(few, defined, pd.Timestamp("2027-05-31T06:00Z"), tmp_path)
    assert r["verdict"] == "inconclusive" and r["read"] == 40 and r["clv"] == pytest.approx(0.05)


def test_ledger_files_stay_apart_from_the_corner_and_match_ledgers(tmp_path):
    d = tmp_path / "paper_trades"
    d.mkdir()
    (d / "SP1_cornerlean_2627.jsonl").write_text(json.dumps({"id": "SP1|2627|x"}) + "\n")
    assert paper.load_ledger(tmp_path, "SP1") == {}
    assert paper.load_corner_ledger(tmp_path, "SP1") == {}
    assert list(pcl.load_ledger(tmp_path, "SP1")) == ["SP1|2627|x"]


def test_sections_and_kelly_on_the_blend_chance(tmp_path):
    rows, res = synth()
    out = run(tmp_path, rows, res)
    secs = pcl.sections(out["trades"], out["defined_at"], out["fits"], NOW, out["judged"])
    main, dk = secs
    assert main["key"] == pcl.KEY and dk["variant_of"] == pcl.KEY and dk["descriptive"]
    assert main["judged"]["target"] == 300 and main["fits"][0]["source"] == "backcast"
    assert main["backcast"]["trades"] == main["corner_lean_trades"] > 0
    assert main["odds_bands"]["clv_field"] == "clv_pinnacle"
    log = main["sizing"]["kelly"]["log"]
    t = {x["id"]: x for x in out["trades"]}
    assert log and all(r["chance"] == pytest.approx(t[r["id"]]["p_bet"], abs=1e-4) for r in log)
    json.dumps(secs, default=str)


def _paper_data():
    return {"fixtures": [], "portfolio": {}}


def test_paper_run_adds_the_track_beside_the_corner_tracks(tmp_path):
    rows, res = synth()
    write_log(tmp_path, rows)
    data = _paper_data()
    paper.run(data, tmp_path, None, now=NOW, corner_results=res)
    c = next(p for p in data["portfolio"]["portfolios"] if p["id"] == "corners")["live"]
    keys = [s["key"] for s in c["strategies"]]
    i = keys.index(pcl.KEY)
    assert keys[i + 1] == pcl.DK_KEY and keys[0] == "pinnacle"
    assert c["corner_lean_trades"] and c["summary"]["trades"] == 0  # never in the record
    json.dumps(data, default=str)


def test_a_failure_costs_only_the_track(tmp_path, monkeypatch):
    rows, res = synth()
    write_log(tmp_path, rows)

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(pcl, "run", boom)
    data = _paper_data()
    paper.run(data, tmp_path, None, now=NOW, corner_results=res)
    c = next(p for p in data["portfolio"]["portfolios"] if p["id"] == "corners")["live"]
    assert [s["key"] for s in c["strategies"]][:1] == ["pinnacle"]
    assert pcl.KEY not in [s["key"] for s in c["strategies"]] and c["error"] is None


def test_a_log_without_look_rows_fits_nothing_and_opens_nothing(tmp_path):
    rows, res = synth()
    closes = [r for r in rows if r["snapshot"] == "close"]
    out = run(tmp_path, closes, res)
    assert out["fits"] == [] and out["trades"] == [] and out["new_fits"] == 0


def test_football_data_and_espn_results_together_never_break_the_counts(tmp_path):
    """27 Nov: publish passed ESPN's rows (with `kickoff`) beside football-data's (date only);
    concatenated, football-data's kickoffs were NaN and _result_index raised on NaT."""
    from soccer_stats import corners_live as cl

    rows, res = synth()
    espn = res.iloc[:5].copy()
    espn["kickoff"] = [
        pd.Timestamp(d).tz_localize("UTC") + pd.Timedelta(hours=15) for d in espn["date"]
    ]
    espn["result_source"] = "espn"
    fd = res.iloc[5:]
    joined = pd.concat([fd, espn], ignore_index=True)
    idx = cl._result_index(joined)
    assert len(idx) == len(res)
    write_log(tmp_path, rows)
    out = pcl.run(tmp_path, list(LEAGUES), NOW, fd, espn)  # the two frames as publish passes them
    (tmp_path / "ref").mkdir()
    ref = run(tmp_path / "ref", rows, res)
    assert [f["fit_at"] for f in out["fits"]] == [f["fit_at"] for f in ref["fits"]]
    assert out["fits"][0]["c"] == pytest.approx(ref["fits"][0]["c"])
