import json

import numpy as np

from soccer_stats.lab import cashout_research as cr
from soccer_stats.lab import cashout_test as ct


def positions():
    rng = np.random.default_rng(1)
    out = []
    for i in range(400):
        q = float(rng.choice([0.2, 0.45, 0.6]))
        out.append(
            {
                "league": ["N1", "B1"][i % 2],
                "season": 2024 + i % 2,
                "kickoff": f"2024-{9 + i % 3:02d}-{1 + i % 28:02d}T14:00Z",
                "home": f"H{i}",
                "away": f"A{i}",
                "dog": "away",
                "entry": [3.5, 5.0, 7.0][i % 3],
                "price": "pinnacle",
                "won": int(rng.random() < q),
                "q_ht": q if i % 4 else None,
                "live_ht": 1 / q if i % 4 else None,
                "q_m70": None,
                "p_model": 0.4,
            }
        )
    return out


def test_the_app_file_has_the_comeback_shape_and_round_trips():
    pos = positions()
    res = ct.analyse(pos, {"positions": len(pos)})
    out = cr.assemble(pos, res, {"label": "Already read, not judged"})
    for key in ("title", "question", "rule", "verdict", "verdict_text", "cost", "runs"):
        assert out[key]
    assert set(out["designs"]) == {"primary", "band_3_4"}
    prim = out["designs"]["primary"]
    assert prim["role"] == "judged" and prim["positions"] == res["primary"]["positions"]
    # one hold row, nine threshold x fee rows and three Kelly rows at the primary stake
    kinds = [r["kind"] for r in prim["rules"]]
    assert kinds.count("hold") == 1 and kinds.count("cash") == 9 and kinds.count("kelly") == 3
    assert sum(r["primary"] for r in prim["rules"]) == 1
    assert [r["stake"] for r in prim["by_stake"]] == [0.01, 0.02, 0.05]
    assert sum(r["positions"] for r in prim["by_league"]) == prim["positions"]
    # the bets are the judged band only, in kickoff order, and the curve adds up
    assert len(out["bets"]) == prim["positions"]
    assert all(b["odds"] >= ct.PRIMARY_DOG for b in out["bets"])
    assert [b["date"] for b in out["bets"]] == sorted(b["date"] for b in out["bets"])
    cash = np.mean([b["cash"] for b in out["bets"]])
    primary_row = next(r for r in prim["rules"] if r["primary"])
    assert abs(cash - primary_row["mean"]) < 1e-4
    assert json.loads(cr.canonical(out)) == json.loads(json.dumps(out))


def test_the_check_names_every_difference_from_the_published_run():
    pos = positions()
    res = ct.analyse(
        pos, {"matches": 1, "not_priced": 0, "unmatched": 0, "no_result": 0, "positions": len(pos)}
    )
    bad = cr.check(res)
    assert any(line.startswith("counts matches") for line in bad)
    assert any(line.startswith("verdict decisions") for line in bad)


def test_halftime_only_ignores_the_70th_minute():
    pos = positions()
    for p in pos:
        p["q_m70"] = 0.9  # would cash out every bet at the 70th minute
    ht = cr.halftime_only(pos)
    decisions = sum(p["q_ht"] is not None and p["q_ht"] >= 0.5 for p in pos)
    assert [r["decisions"] for r in ht["rules"]] == [decisions] * 3
    assert ht["led_cases"] == decisions and ht["positions"] == len(pos)
