import pandas as pd

from soccer_stats.lab import h2_slots as h2


def m(league, kickoff, home, source="H2P"):
    return {"league": league, "kickoff": kickoff, "home": home, "away": "X", "favourite": "home",
            "p_fav": 0.8, "ht": [0, 1], "price": "pinnacle", "source": source}  # fmt: skip


def test_freeze_groups_by_slot_and_leaves_out_used_and_later_kickoffs():
    ms = [m("FAC", "2025-01-11T15:00Z", "B"), m("FAC", "2025-01-11T15:00Z", "A"),
          m("UEL", "2025-01-11T15:00Z", "C", "H2U"),
          m("AUT", "2023-05-06T15:00Z", "D", "extra"), m("FAC", "2026-11-01T15:00Z", "E"),
          m("CHN", "2024-04-01T11:30Z", "F", "extra")]  # fmt: skip
    plan = h2.freeze(ms, {}, pd.Timestamp("2026-10-28", tz="UTC"),
                     used={("CHN", "2024-04-01T11:30Z")})  # fmt: skip
    assert plan["dropped"] == {"after_freeze": 1, "used": 1}
    assert [(s["league"], s["request_at"]) for s in plan["slots"]] == [
        ("AUT", "2023-05-06T15:55Z"),
        ("FAC", "2025-01-11T15:55Z"),
        ("UEL", "2025-01-11T15:55Z"),
    ]
    fac = plan["slots"][1]
    assert fac["sport"] == "soccer_fa_cup" and [x["home"] for x in fac["matches"]] == ["A", "B"]
    assert plan["totals"]["slots"] == 3 and plan["totals"]["credits"] == 30
    assert plan["totals"]["by_source"] == {"H2P": 2, "H2U": 1, "extra": 1}
    assert plan["slots"][0]["sport"] == "soccer_austria_bundesliga"


def test_no_used_design_shares_a_competition_with_h2():
    lgs = {lg for lg, _ in h2.used_slots()}
    assert not lgs & (set(h2.hm.CUPS) | set(h2.hm.EXTRA))
