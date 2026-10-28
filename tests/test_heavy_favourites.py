import pandas as pd

from soccer_stats.lab import comeback_plan as cp
from soccer_stats.lab import heavy_favourites as hf


def frame(rows):
    """Football-data rows: a home favourite near 80% at Pinnacle's close by default."""
    base = {"FTHG": 1, "FTAG": 1, "PSCH": 1.2, "PSCD": 7.0, "PSCA": 15.0}
    base.update({"AvgCH": 1.22, "AvgCD": 6.5, "AvgCA": 13.0})
    return pd.DataFrame([{**base, **r} for r in rows])


def match(home, away, ht, time="15:00", date="09/08/2025", **odds):
    return {"Date": date, "Time": time, "HomeTeam": home, "AwayTeam": away,
            "HTHG": ht[0], "HTAG": ht[1], **odds}  # fmt: skip


def test_counts_heavy_favourites_trailing_and_their_slots(tmp_path, monkeypatch):
    raw = frame(
        [
            match("A", "B", (0, 1)),  # heavy, trailing
            match("C", "D", (0, 2)),  # heavy, two down, same slot
            match("E", "F", (1, 0)),  # heavy, ahead
            match("G", "H", (0, 1), time="17:30", PSCH=None),  # average's close only
            match("I", "J", (0, 1), PSCH=2.0, PSCD=3.4, PSCA=3.8),  # not heavy
            match("K", "L", (0, 1), PSCH=None, AvgCH=None),  # no price at all
        ]
    )
    path = tmp_path / "N1.csv"
    raw.to_csv(path, index=False)
    monkeypatch.setattr(cp, "download", lambda *a, **k: path)
    r = hf.season_scope("N1", 2025, tmp_path)
    assert r["matches"] == 6
    assert (r["priced_pinnacle"], r["priced_average"], r["unpriced"]) == (4, 1, 1)
    assert r["heavy"] == 4 and r["heavy_from_average"] == 1
    assert r["trailing"] == 3 and r["trailing_from_average"] == 1 and r["one_down"] == 2
    assert r["slots"] == 2 and r["credits"] == 2 * hf.HIST_COST
    assert r["snapshot_times"] == ["2025-08-09T14:55Z", "2025-08-09T17:25Z"]


def test_away_favourites_count_from_the_away_side(tmp_path, monkeypatch):
    raw = frame([match("A", "B", (1, 0), PSCH=15.0, PSCA=1.2)])  # away fav trails 1-0
    path = tmp_path / "P1.csv"
    raw.to_csv(path, index=False)
    monkeypatch.setattr(cp, "download", lambda *a, **k: path)
    r = hf.season_scope("P1", 2025, tmp_path)
    assert r["heavy"] == 1 and r["trailing"] == 1


def test_bets_needed_for_each_gap():
    assert hf.needed(0.12) == 136 and hf.needed(0.12, power=None) == 67
    assert hf.needed(0.06) == 542 and hf.needed(0.06, power=None) == 266
    assert hf.power() == {
        "12pts": {"power80": 136, "power50": 67},
        "6pts": {"power80": 542, "power50": 266},
    }


def test_scope_reports_a_missing_file_and_keeps_going(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise OSError("404")

    monkeypatch.setattr(cp, "download", boom)
    rows = hf.scope({"b": (("G1",), (2022,))}, tmp_path)
    assert rows[0]["error"] == "404" and rows[0]["sport"] == "soccer_greece_super_league"
    assert hf.totals(rows) == {
        "b": {"matches": 0, "heavy": 0, "heavy_from_average": 0, "trailing": 0,
              "trailing_from_average": 0, "one_down": 0, "slots": 0, "credits": 0,
              "usable_estimate": 0}
    }  # fmt: skip


def test_freeze_lists_trailing_heavy_slots_and_drops_used_ones(tmp_path, monkeypatch):
    raw = frame(
        [
            match("A", "B", (0, 1)),  # heavy, trailing, 15:00
            match("C", "D", (0, 2)),  # heavy, trailing, same slot
            match("G", "H", (0, 1), time="17:30", PSCH=None),  # average's close, 17:30
            match("E", "F", (1, 0)),  # ahead: not listed
        ]
    )
    path = tmp_path / "N1.csv"
    raw.to_csv(path, index=False)
    monkeypatch.setattr(cp, "download", lambda *a, **k: path)
    out = hf.freeze({"N1": (2025,)}, tmp_path, used={("N1", "2025-08-09T16:30Z")})
    assert out["design"] == "H" and out["totals"] == {"slots": 1, "matches": 2, "credits": 10}
    slot = out["slots"][0]
    assert slot["request_at"] == "2025-08-09T14:55Z" and slot["sport"] == hf.OTHER["N1"][1]
    assert [m["home"] for m in slot["matches"]] == ["A", "C"]
    assert slot["matches"][0]["price"] == "pinnacle" and slot["matches"][0]["favourite"] == "home"
    assert out["per_league_season"]["N1 2025/26"]["dropped_as_used"] == 1


def test_comebacks2_reads_design_h_with_the_average_close(tmp_path, monkeypatch):
    from soccer_stats.lab import comebacks2 as c2

    raw = frame([match("A", "B", (0, 1)), match("G", "H", (0, 1), PSCH=None)])
    raw["PC>2.5"], raw["PC<2.5"] = 1.8, 2.0
    path = tmp_path / "N1.csv"
    raw.to_csv(path, index=False)
    monkeypatch.setattr(cp, "download", lambda *a, **k: path)
    fd = c2.load_fd([{"league": "N1", "kickoff": "2025-08-09T14:00Z"}], any_price=True)
    assert list(fd["source"]) == ["pinnacle", "average"]
    assert fd["fd"].notna().all() and fd["f_over"].notna().all() and fd["p_fav"].min() > 0.75
