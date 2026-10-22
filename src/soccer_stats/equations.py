"""Each model's equation and its fitted numbers per league, for Portfolio (owner, 22 Oct).

`section(data)` reads only what the build already put in data.json: `match_model` (each
league's Dixon-Coles fit, publish.fit_info), `match_blends` (the live Blend Lean fits,
publish.add_lean) and `corners_model` (model (f), corners_live.add_model). No new fits,
no network. Published as data.json `portfolio.models`.
"""

from __future__ import annotations

from soccer_stats import leagues as lgs

MATCH = {
    "key": "match",
    "name": "Match model (Dixon-Coles)",
    "equation": [
        "home goals ~ Poisson(lambda), lambda = exp(intercept + home_adv + attack[home] "
        "+ defence[away])",
        "away goals ~ Poisson(mu), mu = exp(intercept + attack[away] + defence[home])",
        "P(home x, away y) = tau(x, y) x Poisson(x; lambda) x Poisson(y; mu), with "
        "tau(0,0) = 1 - lambda mu rho, tau(0,1) = 1 + lambda rho, tau(1,0) = 1 + mu rho, "
        "tau(1,1) = 1 - rho, else 1",
        "fitted by weighted likelihood: each match weighted exp(-xi x age in days); the "
        "goals are (1 - xg_weight) x goals + xg_weight x xG",
    ],
    "terms": {
        "intercept": "the league's baseline scoring rate (log goals a game)",
        "home_adv": "the home side's extra log goals (dropped at a neutral venue)",
        "attack / defence": "each team's log rating: attack raises its own goals, defence "
        "raises its opponent's (higher = a weaker defence); each sums to 0 over the league",
        "rho": "Dixon-Coles' correction for 0-0, 1-0, 0-1 and 1-1, which plain Poisson gets "
        "slightly wrong; rho < 0 makes 0-0 and 1-1 a little likelier",
        "xi": "time weight per day; half_life_days is how old a match is when it counts "
        "half as much",
        "xg_weight": "how much of each match's goals come from Understat's xG instead of "
        "the score (0 = goals only, where Understat doesn't cover the league)",
        "ridge": "a penalty pulling ratings toward 0 (only the Champions League's "
        "cross-league fit uses it, so a club with few matches stays sensible)",
    },
    "note": "The home, draw and away chances are the sums of P(home x, away y) over the "
    "score grid. The Champions League is one fit over every football-data league the cup's "
    "clubs play in plus the European results (goals only).",
}

BLEND = {
    "key": "blend",
    "name": "Blend Lean (blend of price and model)",
    "equation": [
        "score_k = a_k + b x log(price_k) + c x log(model_k), for k = home, draw, away "
        "(a_home = 0)",
        "p_bet_k = exp(score_k) / (exp(score_home) + exp(score_draw) + exp(score_away))",
        "z_k = (p_bet_k - 1/odds_k) / sigma; a Lean pick at z >= 1, Strong at z >= 2",
    ],
    "terms": {
        "price_k": "the bookmaker's margin-free chance (Shin): Pinnacle's close when the "
        "blend is fitted, DraftKings' price when it is used live",
        "model_k": "the match model's own chance",
        "a_draw, a_away": "fixed tilts toward the draw and the away side",
        "b": "the weight on the price: above 1 sharpens it (favourites up, long shots "
        "down), below 1 flattens it",
        "c": "the weight on the model: 0 ignores it; only its relative size across home, "
        "draw and away matters",
        "sigma": "how far the blend usually strays from the price in this competition "
        "(sd of blend - price on the fit's matches)",
        "1/odds_k": "DraftKings' break-even chance, margin included",
    },
    "note": "Fitted per league on every earlier match with Pinnacle's close; the live fit "
    "is in match_blends.json. A pick is market-driven when it still reaches 1 sigma with "
    "c set to 0.",
}

CORNERS = {
    "key": "corners_f",
    "name": "Team corners model (f)",
    "equation": [
        "home corners ~ NB2(mean_h, alpha_home), mean_h = mean_home x for[home] x against[away]",
        "away corners ~ NB2(mean_a, alpha_away), mean_a = mean_away x for[away] x against[home]",
        "for[t] = (sum of t's corners / league mean + k_for) / (matches + k_for); "
        "against[t] likewise with k_against",
        "NB2: variance = mean + alpha x mean^2",
    ],
    "terms": {
        "mean_home / mean_away": "the league's average corners for the home and away side "
        "over the fit window",
        "for[t] / against[t]": "how many corners a team takes / concedes as a ratio to the "
        "league average (1 = average)",
        "k_for / k_against": "empirical-Bayes pseudo-matches pulling each ratio toward 1: "
        "a team needs about k matches before its own record counts half",
        "alpha": "NB2 dispersion: how much more spread out corner counts are than Poisson",
    },
    "note": "Fitted per league on the 730 days of football-data corner counts before today; "
    "an over at line L wins when the team's corners exceed L.",
}

BLEND_KEYS = ("a_draw", "a_away", "b", "c")


def _blend_entry(e: dict) -> dict:
    coef = e.get("coef") or []
    return {
        "coef": dict(zip(BLEND_KEYS, coef[:4], strict=False)) if len(coef) >= 4 else None,
        **{k: e.get(k) for k in ("sigma", "matches", "fit_from", "fit_to", "fit_date")},
    }


def section(data: dict) -> dict:
    """{order, match, blend, corners_f}: each model's equation, its terms explained and
    `leagues` = {code: {name, ...that league's fitted numbers}} (or {error} where the fit
    failed). A model with nothing in data.json has empty `leagues`."""
    names = lambda code: lgs.name(code)  # noqa: E731

    match = {
        code: {"name": names(code), **fit} for code, fit in (data.get("match_model") or {}).items()
    }
    blends = ((data.get("match_blends") or {}).get("leagues")) or {}
    blend = {code: {"name": names(code), **_blend_entry(e)} for code, e in blends.items()}
    corners = {
        code: {"name": names(code), **info}
        for code, info in (data.get("corners_model") or {}).items()
    }
    return {
        "order": [MATCH["key"], BLEND["key"], CORNERS["key"]],
        "match": {**MATCH, "leagues": match},
        "blend": {
            **BLEND,
            "generated_at": (data.get("match_blends") or {}).get("generated_at"),
            "leagues": blend,
        },
        "corners_f": {**CORNERS, "leagues": corners},
    }
