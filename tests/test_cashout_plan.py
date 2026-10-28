import pandas as pd

from soccer_stats.lab import cashout_plan as xp
from soccer_stats.lab import comeback_plan as cp


def frame(rows):
    """Football-data rows: an away underdog at 5.0 at Pinnacle's close by default."""
    base = {"FTHG": 1, "FTAG": 1, "HTHG": 0, "HTAG": 0, "PSCH": 1.6, "PSCD": 4.0, "PSCA": 5.0}
    base.update({"AvgCH": 1.62, "AvgCD": 3.9, "AvgCA": 4.8})
    return pd.DataFrame([{**base, **r} for r in rows])


def match(home, away, time="15:00", date="09/08/2025", **odds):
    return {"Date": date, "Time": time, "HomeTeam": home, "AwayTeam": away, **odds}


def setup(tmp_path, monkeypatch, rows, league="N1"):
    path = tmp_path / f"{league}.csv"
    frame(rows).to_csv(path, index=False)
    monkeypatch.setattr(cp, "download", lambda *a, **k: path)


def test_underdogs_from_pinnacle_else_the_average_and_both_moments(tmp_path, monkeypatch):
    rows = [
        match("A", "B"),  # away dog 5.0, Pinnacle
        match("C", "D", PSCH=4.2, PSCA=1.8),  # home dog 4.2
        match("E", "F", PSCH=None, AvgCA=3.5),  # average's close only, dog 3.5
        match("G", "H", PSCH=2.4, PSCA=2.9),  # longest price 2.9: not a heavy underdog
        match("I", "J", time="17:30"),  # a second slot
    ]
    setup(tmp_path, monkeypatch, rows)
    slots = xp.season_slots("N1", 2025, used=set(), raw_dir=tmp_path)
    assert [s["kickoff"] for s in slots] == ["2025-08-09T14:00Z", "2025-08-09T16:30Z"]
    first = {m["home"]: m for m in slots[0]["matches"]}
    assert set(first) == {"A", "C", "E"}
    assert (first["C"]["dog"], first["C"]["dog_odds"]) == ("home", 4.2)
    assert (first["E"]["price"], first["E"]["dog_odds"]) == ("average", 3.5)
    reqs = xp.requests_for(slots)
    assert [r["request_at"] for r in reqs] == [
        "2025-08-09T14:55Z", "2025-08-09T15:25Z", "2025-08-09T17:25Z", "2025-08-09T17:55Z",
    ]  # fmt: skip


def test_never_reads_a_score(tmp_path, monkeypatch):
    rows = [match("A", "B", HTHG=0, HTAG=3, FTHG=0, FTAG=3), match("C", "D", HTHG=2, FTHG=4)]
    setup(tmp_path, monkeypatch, rows)
    a = xp.season_slots("N1", 2025, used=set(), raw_dir=tmp_path)
    rows = [match("A", "B", HTHG=1, HTAG=1, FTHG=1, FTAG=1), match("C", "D")]
    setup(tmp_path, monkeypatch, rows)
    assert xp.season_slots("N1", 2025, used=set(), raw_dir=tmp_path) == a


def test_used_matches_are_left_out_and_held_snapshots_cost_nothing(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch, [match("A", "B"), match("C", "D")])
    used = {("N1", "2025-08-09T14:00Z", "A")}
    held = {("soccer_netherlands_eredivisie", "2025-08-09T14:55Z")}
    res = xp.freeze(priced={"N1": (2025,)}, raw_dir=tmp_path, used=used, held=held)
    assert [m["home"] for s in res["matches"] for m in s["matches"]] == ["C"]
    assert res["totals"]["requests"] == 2 and res["totals"]["held"] == 1
    assert res["totals"]["credits"] == xp.HIST_COST
    assert res["slots"][0]["for"] == [{"kickoff": "2025-08-09T14:00Z", "snapshot": "ht"}]


def test_requests_shared_by_two_slots_are_bought_once():
    slots = [
        {"league": "N1", "sport": "s", "kickoff": "2025-08-09T13:00Z", "matches": []},
        {"league": "N1", "sport": "s", "kickoff": "2025-08-09T13:30Z", "matches": []},
    ]
    reqs = xp.requests_for(slots)
    at = [r["request_at"] for r in reqs]
    assert at == ["2025-08-09T13:55Z", "2025-08-09T14:25Z", "2025-08-09T14:55Z"]
    assert len(reqs[1]["for"]) == 2  # 13:00 + 85 and 13:30 + 55


def test_trim_drops_the_oldest_seasons_first():
    per = {
        "N1 2022/23": {"year": 2022, "credits": 30},
        "P1 2022/23": {"year": 2022, "credits": 10},
        "N1 2025/26": {"year": 2025, "credits": 40},
        "P1 2023/24": {"year": 2023, "credits": 20},
        "B1 2024/25": {"error": "missing"},
    }
    kept, dropped = xp.trim(per, budget=65)
    assert dropped == ["N1 2022/23", "P1 2022/23"]
    assert kept == ["N1 2025/26", "P1 2023/24"]
    assert xp.trim(per, budget=1000) == (
        ["N1 2022/23", "P1 2022/23", "N1 2025/26", "P1 2023/24"],
        [],
    )


def test_every_priced_league_has_a_sport_key():
    for lg in xp.PRICED:
        assert xp.sport_of(lg).startswith("soccer_")
