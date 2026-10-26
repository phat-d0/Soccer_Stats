"""A stand-in for src/soccer_stats/lab/comeback_research.json (Research lab builds the real
file), in the shape the lead specified on 26 Oct, so the app's "Comeback bets" portfolio
can be built and smoke-tested before it lands. The headline numbers are the real ones
(design C 732 bets, 123 won, ROI -25.1%; B 198, +7.3%; pooled 930, -18.2%); the bet list,
leagues, seasons and bands are synthetic but add up to them. Deterministic (seeded).
make_fixture.py uses the real file when it exists, this otherwise.
"""

from __future__ import annotations

import math
import random

LEAGUES_C = ["E0", "SP1", "D1", "I1", "F1", "E1", "E2", "E3"]
LEAGUES_B = ["E0", "SP1", "D1", "I1", "F1"]
SEASONS_C = ["2022/23", "2023/24", "2024/25"]
BANDS = [(0.45, 0.55), (0.55, 0.65), (0.65, 0.75), (0.75, 1.0)]
TEAMS = {
    "E0": [
        "Arsenal",
        "Chelsea",
        "Liverpool",
        "Man City",
        "Newcastle",
        "Spurs",
        "Wolves",
        "Everton",
    ],
    "SP1": [
        "Barcelona",
        "Real Madrid",
        "Sevilla",
        "Valencia",
        "Betis",
        "Getafe",
        "Osasuna",
        "Celta",
    ],
    "D1": [
        "Bayern Munich",
        "Dortmund",
        "Leipzig",
        "Leverkusen",
        "Mainz",
        "Bochum",
        "Freiburg",
        "Koln",
    ],
    "I1": ["Inter", "Milan", "Juventus", "Napoli", "Roma", "Lazio", "Torino", "Lecce"],
    "F1": ["Paris SG", "Marseille", "Lyon", "Monaco", "Lille", "Nantes", "Rennes", "Brest"],
    "E1": ["Leeds", "Burnley", "Sunderland", "Norwich", "Hull", "Millwall", "Stoke", "QPR"],
    "E2": ["Derby", "Portsmouth", "Bolton", "Barnsley", "Oxford", "Wigan", "Lincoln", "Exeter"],
    "E3": [
        "Wrexham",
        "Stockport",
        "MK Dons",
        "Notts County",
        "Gillingham",
        "Crewe",
        "Walsall",
        "Bradford",
    ],
}


def _season_dates(season: str, rng: random.Random) -> str:
    y = int(season[:4])
    month = rng.choice([8, 9, 10, 11, 12, 1, 2, 3, 4, 5])
    year = y if month >= 8 else y + 1
    return f"{year}-{month:02d}-{rng.randint(1, 28):02d}"


def _bets(design: str, n: int, wins: int, roi: float, leagues, seasons, rng) -> list[dict]:
    rows = []
    for _ in range(n):
        lg = rng.choice(leagues)
        season = rng.choice(seasons)
        fav_p = rng.choice(BANDS)
        pre = round(rng.uniform(fav_p[0], min(fav_p[1], 0.9)), 3)
        implied = max(0.06, min(0.6, rng.gauss(0.155 + (pre - 0.55) * 0.5, 0.05)))
        home, away = rng.sample(TEAMS[lg], 2)
        rows.append(
            {
                "date": _season_dates(season, rng),
                "lg": lg,
                "home": home,
                "away": away,
                "fav": rng.choice(["home", "away"]),
                "pre": pre,
                "ht": rng.choice(["0-1", "1-2", "0-2", "1-0", "2-1"]),
                "implied": round(implied, 4),
                "won": False,
                "design": design,
            }
        )
    # Winners lean towards the shorter prices; then scale their odds so ROI hits the target.
    order = sorted(range(n), key=lambda i: -rows[i]["implied"] * rng.random())
    for i in order[:wins]:
        rows[i]["won"] = True
    for r in rows:
        r["odds"] = round(1 / r["implied"] * 0.97, 2)
    target = n * (1 + roi)  # total returned on $1 a bet = sum of the winners' odds
    got = sum(r["odds"] for r in rows if r["won"])
    for r in rows:
        if r["won"]:
            r["odds"] = round(max(1.05, r["odds"] * target / got), 2)
    return rows


def _ci(xs: list[float]) -> list[float] | None:
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))
    h = 1.96 * sd / math.sqrt(len(xs))
    return [round(m - h, 4), round(m + h, 4)]


def _group(rows: list[dict]) -> dict:
    pl = [(r["odds"] - 1) if r["won"] else -1.0 for r in rows]
    n = len(rows)
    wins = sum(r["won"] for r in rows)
    return {
        "bets": n,
        "wins": wins,
        "win_rate": round(wins / n, 4) if n else None,
        "implied": round(sum(r["implied"] for r in rows) / n, 4) if n else None,
        "roi": round(sum(pl) / n, 4) if n else None,
        "roi_ci95": _ci(pl),
    }


def _design(role, label, rows, leagues, seasons, head) -> dict:
    by_league = [{"code": c, **_group([r for r in rows if r["lg"] == c])} for c in leagues]
    by_season = []
    for s in seasons:
        y = int(s[:4])
        part = [r for r in rows if f"{y}-08-01" <= r["date"] < f"{y + 1}-08-01"]
        by_season.append({"season": s, **_group(part)})
    by_band = [
        {"lo": lo, "hi": hi, **_group([r for r in rows if lo <= r["pre"] < hi])} for lo, hi in BANDS
    ]
    return {
        "role": role,
        "label": label,
        "leagues": leagues,
        "seasons": seasons,
        "by_league": [g for g in by_league if g["bets"]],
        "by_season": [g for g in by_season if g["bets"]],
        "by_band": by_band,
        "avg_odds": round(sum(r["odds"] for r in rows) / len(rows), 2),
        **head,
    }


def stub() -> dict:
    rng = random.Random(26)
    c_rows = _bets("C", 732, 123, -0.251, LEAGUES_C, SEASONS_C, rng)
    b_rows = _bets("B", 198, 55, 0.073, LEAGUES_B, ["2024/25"], rng)
    bets = sorted(c_rows + b_rows, key=lambda r: (r["date"], r["lg"], r["home"]))
    head_c = {
        "slots": 709,
        "matches": 902,
        "not_priced": 115,
        "bets": 732,
        "wins": 123,
        "win_rate": 0.170,
        "implied": 0.213,
        "gap": -0.043,
        "gap_ci95": [-0.069, -0.016],
        "roi": -0.251,
        "roi_ci95": [-0.383, -0.102],
        "betfair_roi": -0.181,
        "betfair_roi_ci95": [-0.346, -0.001],
        "blend_c": 0.54,
        "blend_c_ci95": [-0.05, 1.23],
    }
    head_b = {
        "slots": 192,
        "matches": 210,
        "not_priced": 12,
        "bets": 198,
        "wins": 55,
        "win_rate": 0.278,
        "implied": 0.245,
        "gap": 0.033,
        "gap_ci95": [-0.027, 0.092],
        "roi": 0.073,
        "roi_ci95": [-0.192, 0.343],
        "betfair_roi": 0.149,
        "betfair_roi_ci95": [-0.137, 0.455],
        "blend_c": 0.30,
        "blend_c_ci95": [-0.58, 1.19],
    }
    head_p = {
        "slots": 901,
        "matches": 1112,
        "not_priced": 127,
        "bets": 930,
        "wins": 178,
        "win_rate": 0.191,
        "implied": 0.220,
        "gap": -0.029,
        "gap_ci95": [-0.052, -0.006],
        "roi": -0.182,
        "roi_ci95": [-0.300, -0.051],
        "betfair_roi": -0.111,
        "betfair_roi_ci95": [-0.248, 0.031],
        "blend_c": 0.46,
        "blend_c_ci95": [-0.03, 0.97],
    }
    designs = {
        "C": _design(
            "judged",
            "Design C: eight leagues, 2022/23 to 2024/25 (judged)",
            c_rows,
            LEAGUES_C,
            SEASONS_C,
            head_c,
        ),
        "B": _design(
            "descriptive",
            "Design B: top five, 2024/25 (first look)",
            b_rows,
            LEAGUES_B,
            ["2024/25"],
            head_b,
        ),
        "pooled": _design(
            "descriptive",
            "C and B together (descriptive)",
            c_rows + b_rows,
            LEAGUES_C,
            SEASONS_C,
            head_p,
        ),
    }
    return {
        "title": "Favourites coming back from behind",
        "question": "When the pre-match favourite is behind at half-time, does backing it at "
        "the in-play price make money?",
        "rule": "$1 on the trailing favourite to win, at DraftKings' in-play price about 10 "
        "minutes into the second half; judged on design C's matches, pre-registered.",
        "verdict": "fail",
        "verdict_text": "No edge. The in-play price already expects the comeback and overrates "
        "it: trailing favourites won 17.0% of the time against the 21.3% the price implied, "
        "and $1 a bet lost 25.1% (95% range −38.3% to −10.2%).",
        "closed_at": "2026-10-25",
        "cost": {"design_b": 1920, "design_c": 6070, "probes": 21, "total": 8011},
        "runs": {
            "design_b_fetch": 40144988040,
            "design_b_analysis": 40145295663,
            "design_c_fetch": 40176031079,
            "design_c_analysis": 40176653428,
            "timelines": [40176002361, 40184239564],
            "probes": [40136522857, 40136975819, 40204253352],
        },
        "preregistered": {"b": "e4b3f8b", "c": "dca429e"},
        "designs": designs,
        "bets": [{k: v for k, v in r.items() if k != "pre"} for r in bets],
        "phase1": {
            "ht_behind": {"matches": 2166, "won": 0.197, "model": 0.140},
            "conceded_first": {"matches": 2174, "won": 0.288, "model": 0.245},
            "tests_above_zero": 5,
            "tests": 18,
            "note": "Free data: favourites behind at half-time won more often than a fair "
            "score-blind model expected. About a third of that gap is the second half's higher "
            "scoring rate.",
        },
        "timelines": {
            "matches": 15838,
            "joined_share": 0.983,
            "trail_first_share": 0.314,
            "won": 0.259,
            "model": 0.221,
            "gap": 0.041,
            "gap_ci95": [0.028, 0.054],
            "by_minute": [
                {"lo": lo, "hi": lo + 15, "matches": m, "won": w, "model": md}
                for lo, m, w, md in [
                    (0, 1620, 0.321, 0.279),
                    (15, 1104, 0.288, 0.247),
                    (30, 981, 0.262, 0.221),
                    (45, 712, 0.214, 0.176),
                    (60, 470, 0.163, 0.127),
                    (75, 87, 0.092, 0.061),
                ]
            ],
            "priced_test_cost": 18820,
            "note": "ESPN goal timelines: when the underdog scores first, the favourite comes "
            "back a steady 3 to 6 points more often than the score-blind model says, at every "
            "minute. That is the model's known shortfall, which the in-play price already "
            "covers.",
        },
        "notes": [
            "Prices: historical in-play snapshots from The Odds API, one per kickoff slot about "
            "55 minutes after kickoff; DraftKings' price is the bet, Betfair's exchange after "
            "2% commission a second view.",
            "Design C was pre-registered before any of its prices were bought; design B was the "
            "first look and is shown for description only.",
            "115 matches in three league-seasons had no fresh in-play price and count as not "
            "priced.",
        ],
        "stub": True,
    }
