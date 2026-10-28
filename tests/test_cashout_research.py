import json

import numpy as np

from soccer_stats.lab import cashout_research as cr
from soccer_stats.lab import cashout_test as ct


def positions(n=400, seed=1):
    rng = np.random.default_rng(seed)
    out = []
    for i in range(n):
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
                "q_m70": 0.55 if i % 5 == 0 else None,
                "p_model": 0.4,
            }
        )
    return out


def test_the_app_file_has_the_shape_the_app_reads_and_round_trips():
    pos = positions()
    res = ct.analyse(pos, {"positions": len(pos)})
    extra = [{**p, "home": f"X{i}"} for i, p in enumerate(positions(60, seed=2))]
    out = cr.assemble(pos, res, extra, 7)
    assert out["kind"] == "cashout" and out["primary_rule"] == {"t": 0.5, "m": 0.05, "s": 0.02}
    assert out["test_verdict"] == res["verdict"] and out["verdict"] == res["verdict"]["verdict"]
    ds = out["designs"]
    assert set(ds) == {"primary", "band_3_4", "put_back", "frozen_ht", "season_2025"}
    assert ds["primary"]["role"] == "judged"
    assert all(d["role"] == "descriptive" for k, d in ds.items() if k != "primary")
    assert ds["primary"]["positions"] == res["primary"]["positions"]
    assert ds["primary"]["variants"] == res["primary"]["variants"]
    assert "design H's 7 are" in ds["season_2025"]["label"]
    # the half-time-only designs never cash out at the 70th minute
    for k in ("put_back", "frozen_ht"):
        assert all(not v.get("at_m70") for v in ds[k]["variants"])
    first = ds["frozen_ht"]["positions"]
    assert ds["put_back"]["positions"] == first + sum(p["entry"] >= 4 for p in extra)
    # the curve starts from $1,000 at 2% stakes and stays within 200 points
    cv = out["curves"]
    assert cv["start"] == 1000 and cv["stake"] == 0.02 and 0 < len(cv["points"]) <= 200
    # the bets are the judged rule's cash-outs only, in kickoff order
    rule = next(v for v in ds["primary"]["variants"] if v.get("t") == 0.5 and v.get("m") == 0.05)
    assert len(out["bets"]) == rule["decisions"]
    assert all(b["moment"] in ("ht", "m70") and b["odds"] >= 4 for b in out["bets"])
    assert [b["date"] for b in out["bets"]] == sorted(b["date"] for b in out["bets"])
    assert out["why"][0]["rows"][0]["n"] == first
    assert out["verdict_text"].startswith("No. First, why holding looks so bad")
    assert out["sample_note"].startswith("To avoid buying any match twice")
    assert json.loads(cr.canonical(out)) == json.loads(json.dumps(out))


def test_the_curve_is_thinned_to_200_points_and_keeps_the_last_day():
    pos = []
    for d in range(400):
        day = np.datetime64("2024-08-01") + np.timedelta64(d, "D")
        pos.append({**positions(1)[0], "kickoff": f"{day}T14:00Z", "home": f"H{d}"})
    cv = cr.curves(pos)
    assert len(cv["points"]) == 200
    assert cv["points"][-1]["date"] == str(np.datetime64("2024-08-01") + np.timedelta64(399, "D"))


def test_halftime_only_drops_the_70th_minute_quote():
    pos = positions()
    assert all(p["q_m70"] is None for p in cr.halftime_only(pos))
    assert any(p["q_m70"] is not None for p in pos)


def test_the_check_names_every_difference_from_the_published_run():
    pos = positions()
    counts = {"matches": 1, "not_priced": 0, "unmatched": 0, "no_result": 0, "positions": 400}
    bad = cr.check(ct.analyse(pos, counts))
    assert any(line.startswith("counts matches") for line in bad)
    assert any(line.startswith("verdict decisions") for line in bad)
