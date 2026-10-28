"""Draw no bet paper tracks beside the match strategies (owner, 26 Oct; descriptive).

Each match strategy gets a DNB track (trades.DNB_TRACKS): `lean_dnb` beside Blend Lean,
`edge12_dnb` beside Model 12%, the live versions of the 24 Oct backtest's `dnb_lean` and
`dnb12` (docs/lab.md "Draw no bet"). The 1X2 trades stay the record: DNB trades live in
their own ledger files (paper_trades/<code>_dnb_<season>.jsonl, never read by
paper.load_ledger), out of Moneyline's combined trades and summary.

* Price: DraftKings' h2h already fetched (no new market), DNB = side x (1 - 1/draw)
  (trades.dnb_odds); margin-free = the Shin 1X2 with the draw taken out (trades.fair_dnb).
* Chance: the parent's own with the draw taken out: Model 12% the card's raw `p` (its
  rule's p_source), Lean the card's blend `p_bet`.
* Rules: trades.dnb_pick_12 at the parent's threshold; trades.dnb_pick_lean at the league's
  sigma_dnb (match_blends.json; none = no Lean DNB trades), the Lean odds range on the DNB
  price.
* Gates as the parent: DraftKings odds at most paper.FRESH_HOURS old, before kickoff, not
  low_data, priced; $10; one trade per match and track.
* Close: the last logged DraftKings h2h before kickoff (odds_log), CLV against its fair DNB;
  Pinnacle's DNB close (football-data) at settlement. A draw refunds (void, push).
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from soccer_stats import backcast as bc
from soccer_stats import odds_log as ol
from soccer_stats import trades as tr

UPDATE_FIELDS = {
    "close_odds",
    "close_prices",
    "close_fetched_at",
    "close_minutes_before",
    "clv_dk",
    "clv_dk_staked",
    "beat_close_dk",
    "clv_pinnacle",
    "clv_pinnacle_staked",
    "status",
    "score",
    "push",
    "profit",
    "settled_at",
    "result_source",
    "result_check",
    "check_score",
    # Pinnacle's live close from the every-book log (28 Oct; paper.pinnacle_live).
    "pinnacle_live_close_prices",
    "pinnacle_live_fetched_at",
    "pinnacle_live_minutes_before",
    "clv_pinnacle_live",
}
COMPARE_NOTE = (
    "Descriptive: the track and its parent bet different sides and prices, so this compares "
    "their records over the same period, not the same bets. CLV per $ multiplies a DNB bet's "
    "CLV by the close's chance of no draw, so it compares directly with the parent's."
)


def ledger_files(log_dir: Path, league: str) -> list[Path]:
    return sorted((Path(log_dir) / "paper_trades").glob(f"{league}_dnb_*.jsonl"))


def load_ledger(log_dir: Path, league: str) -> dict[str, dict]:
    """A league's DNB trades by id (paper._fold: "open" lines once, "update" lines change
    only UPDATE_FIELDS)."""
    from soccer_stats import paper

    return paper._fold(ledger_files(log_dir, league), UPDATE_FIELDS)


def append_events(log_dir: Path, events: list[dict]) -> int:
    """Append DNB events to <code>_dnb_<season>.jsonl (league and season from the id)."""
    if not events:
        return 0
    d = Path(log_dir) / "paper_trades"
    d.mkdir(parents=True, exist_ok=True)
    by_file: dict[Path, list[str]] = {}
    for ev in events:
        lg, season = ev["id"].split("|")[:2]
        by_file.setdefault(d / f"{lg}_dnb_{season}.jsonl", []).append(
            json.dumps(ev, separators=(",", ":"))
        )
    for path, lines in by_file.items():
        with open(path, "a") as f:
            f.write("\n".join(lines) + "\n")
    return len(events)


def _round(v, n=4):
    return None if v is None else round(float(v), n)


def _close_fields(t: dict, group: dict, quoted, kickoff) -> dict:
    """Closing fields from a DraftKings h2h group at `quoted`: the side's DNB close and the
    CLV against the group's fair DNB (per stake not refunded, and per $)."""
    from soccer_stats import paper

    c = tr.dnb_clv(t["odds"], t["market"], group)
    close = tr.dnb_odds(group.get(t["market"]), group.get("draw"))
    return {
        "close_odds": _round(close),
        "close_prices": group,
        "close_fetched_at": quoted,
        "close_minutes_before": paper._minutes_before(kickoff, quoted),
        "clv_dk": None if c is None else c[0],
        "clv_dk_staked": None if c is None else c[1],
        "beat_close_dk": None if c is None else bool(c[0] > 0),
    }


def _log_close(t: dict, log: pd.DataFrame, now: pd.Timestamp) -> dict | None:
    """An update event when the odds log holds a later h2h close than the trade has."""
    from soccer_stats import paper

    r = ol.last_before(log, t["home"], t["away"], t["kickoff"], "h2h")
    if r is None:
        return None
    quoted = r["fetched_at"].isoformat(timespec="seconds")
    group = {m: r["prices"].get(m) for m in tr.LEAN_MARKETS}
    if quoted == t.get("close_fetched_at") and group == t.get("close_prices"):
        return None
    if t.get("close_fetched_at") and pd.Timestamp(quoted) < pd.Timestamp(t["close_fetched_at"]):
        return None  # never step the close back in time
    return paper._update(t, now, **_close_fields(t, group, quoted, t["kickoff"]))


def _dnb_clv(t: dict, prices: dict) -> float | None:
    c = tr.dnb_clv(t["odds"], t["market"], prices)
    return None if c is None else c[0]


def _pinnacle(res: dict, t: dict) -> dict:
    """Pinnacle's DNB CLV from football-data's closing 1X2 on a result row."""
    c = tr.dnb_clv(t["odds"], t["market"], {m: res.get(f"close_{m}") for m in tr.LEAN_MARKETS})
    return {
        "clv_pinnacle": None if c is None else c[0],
        "clv_pinnacle_staked": None if c is None else c[1],
    }


def _fd_check(t: dict, results: pd.DataFrame | None, now: pd.Timestamp) -> dict | None:
    """paper._fd_check for a DNB trade ESPN settled: football-data's score check and
    Pinnacle's DNB CLV, once."""
    from soccer_stats import paper

    if t.get("result_source") != "espn" or t.get("result_check") or not t.get("score"):
        return None
    res = paper._result(results, t)
    if res is None or paper._source(res) != paper.FOOTBALL_DATA:
        return None
    if pd.isna(res.get("home_goals")) or pd.isna(res.get("away_goals")):
        return None
    fd = f"{int(res['home_goals'])}-{int(res['away_goals'])}"
    extra = _pinnacle(res, t) if t.get("clv_pinnacle") is None else {}
    return paper._update(
        t, now, result_check="ok" if fd == t["score"] else "mismatch", check_score=fd, **extra
    )


def _chances(c: dict, track: str, rule: dict) -> dict | None:
    """The chance a track trades on, with the draw taken out: Lean the card's blend p_bet;
    Model 12% the raw `p` under its p_source "model", else p_bet when set."""
    if track == "lean_dnb":
        return tr.without_draw(c.get("p_bet"))
    raw = rule.get("p_source") == "model"
    return tr.without_draw(c.get("p") if raw else (c.get("p_bet") or c.get("p")))


def _pick(c: dict, track: str, rule: dict, prices: dict) -> dict | None:
    q = _chances(c, track, rule)
    if q is None:
        return None
    if track == "lean_dnb":
        pk = tr.dnb_pick_lean(q, prices, rule.get("sigma"), rule.get("min_z", tr.LEAN_Z))
        if pk and not tr.in_lean_range(pk["odds"]):
            return None  # the odds range, on the DNB price; no other side is tried
        return pk
    th = rule.get("threshold")
    return None if th is None else tr.dnb_pick_12(q, prices, th)


def new_trade(c, pick, track, rule, kickoff, now, fetched, league, ref) -> dict:
    """A DNB trade record: trades.new_trade's shape with bet_type "dnb", strategy = the
    track, `variant_of` its parent, id = the parent's trade id + tr.DNB_SUFFIX, plus the
    entry 1X2 (`entry_prices`), the raw model's and the blend's DNB chances (`p_model`,
    `p_blend`; Kelly sizes on p_blend) and, for Lean, z and sigma."""
    parent = rule["variant_of"]
    t = tr.new_trade(
        pick,
        source="live",
        league=league,
        home=c["home"],
        away=c["away"],
        kickoff=kickoff,
        opened_at=now,
        odds_fetched_at=fetched,
        threshold=rule.get("min_z") if track == "lean_dnb" else rule.get("threshold"),
        news_applied=bool(c.get("news_applied")),
        model_ref={**(ref or {}), "probs": "blend" if track == "lean_dnb" else "raw"},
        rule=rule["rule"],
        p_source=rule.get("p_source"),
        strategy=parent,  # the parent's id shape (Lean "...|lean"), then the track below
    )
    raw, blend = tr.without_draw(c.get("p")), tr.without_draw(c.get("p_bet"))
    t.update(
        id=t["id"] + tr.DNB_SUFFIX,
        bet_type="dnb",
        strategy=track,
        variant_of=parent,
        entry_prices={m: (c.get("odds") or {}).get(m) for m in tr.LEAN_MARKETS},
        p_model=_round(raw[pick["market"]]) if raw else None,
        p_blend=_round(blend[pick["market"]]) if blend else None,
        push=False,
    )
    if track == "lean_dnb":
        t.update(
            z=round(pick["z"], 3),
            sigma=round(float(rule["sigma"]), 5),
            p_bet=_round(pick["model_p"]),
            odds_range=list(tr.LEAN_ODDS_RANGE),
        )
    return t


def _open(ledger, c, now, fetched, league, rules, ref, mark=None) -> list[dict]:
    """Open every track's trade on one card (kickoff ahead, not low_data, priced), once
    per match and track; `mark(t, track, rule)` tags a backcast entry. Returns events."""
    kickoff = pd.Timestamp(c["kickoff"])
    if kickoff <= now or c.get("low_data"):
        return []
    prices = tr.dnb_prices(c.get("odds"))
    if prices is None:
        return []
    events = []
    for track, rule in rules.items():
        pick = _pick(c, track, rule, prices)
        if not pick:
            continue
        t = new_trade(c, pick, track, rule, kickoff, now, fetched, league, ref)
        if tr.holds(ledger, t["id"]):
            continue
        group = {m: c["odds"].get(m) for m in tr.LEAN_MARKETS}
        quoted = c.get("odds_updated") or fetched
        t.update(_close_fields(t, group, quoted, kickoff.isoformat()))
        if mark is not None:
            mark(t, track, rule)
        ledger[t["id"]] = t
        events.append({"type": "open", **t})
    return events


SIGMA_FROM = "26 Oct refit"  # sigma_dnb first appears in the 26 Oct match_blends refit


def sigma_from(rule: dict) -> str:
    """Which refit a backcast Lean DNB trade's sigma comes from ("26 Oct refit"): the
    fit's date when the rule carries it."""
    made = rule.get("fit_made_at")
    try:
        return f"{pd.Timestamp(made):%-d %b} refit" if made else SIGMA_FROM
    except (TypeError, ValueError):
        return SIGMA_FROM


def backcast(
    ledger: dict[str, dict],
    odds_log: pd.DataFrame | None,
    league: str,
    rules: dict[str, dict],
    ref: dict | None = None,
    until: str = tr.DNB_FROM,
    start: str = bc.START,
) -> list[dict]:
    """The tracks replayed from bc.START up to `until` (their defined_at) on the odds log
    (owner, 28 Oct): each logged DraftKings h2h row is the card a publish saw at its
    `logged_at`, with the live gates (odds downloaded at most paper.FRESH_HOURS before,
    kickoff ahead, priced) and the live rules: Model 12% on the logged raw `p`, Lean on the
    logged `p_bet` at today's sigma_dnb (it didn't exist before its refit: flagged
    `sigma_from`) with the odds range on the DNB price. Entries are marked backcast (never
    judged); a match already holding a track's trade is left alone. Mutates `ledger`;
    returns the open events. Closes and settlement are update_ledger's, as live."""
    from soccer_stats import paper

    def tag(t, track, rule):
        bc.mark(t, **({"sigma_from": sigma_from(rule)} if track == "lean_dnb" else {}))

    events: list[dict] = []
    fresh = pd.Timedelta(hours=paper.FRESH_HOURS)
    for now, c, downloaded in bc.observations(odds_log, until, start):
        if downloaded is None or now - downloaded > fresh:
            continue
        fetched = downloaded.isoformat(timespec="seconds")
        events += _open(ledger, c, now, fetched, league, rules, ref, mark=tag)
    return events


def update_ledger(
    ledger: dict[str, dict],
    fixtures: list[dict],
    odds_source: dict,
    results: pd.DataFrame | None,
    now: pd.Timestamp,
    league: str,
    rules: dict[str, dict],
    ref: dict | None = None,
    odds_log: pd.DataFrame | None = None,
    espn: pd.DataFrame | None = None,
    books_log: pd.DataFrame | None = None,
) -> list[dict]:
    """Open, close and settle one league's DNB trades. Mutates `ledger`; returns events.

    `rules` maps a track (trades.DNB_TRACKS values) to its trades.dnb_rule. The gates are
    the parent strategies': DraftKings odds at most paper.FRESH_HOURS old, kickoff ahead,
    not low_data, priced. Closes come from `odds_log` (the last logged h2h before kickoff),
    else the card's price until kickoff. Settlement: football-data's result (`results`),
    else ESPN's (`espn`); a draw refunds; a fixture moved 48 hours or no result in 14 days
    voids, as paper.update_ledger."""
    from soccer_stats import paper

    events: list[dict] = []
    fetched = odds_source.get("fetched_at")
    fresh = (
        odds_source.get("name") == "DraftKings"
        and fetched is not None
        and now - pd.Timestamp(fetched) <= pd.Timedelta(hours=paper.FRESH_HOURS)
    )
    cards = {(c["home"], c["away"]): c for c in fixtures if c.get("kickoff")}

    # 1. Open: one trade per match and track.
    if fresh:
        for c in cards.values():
            events += _open(ledger, c, now, fetched, league, rules, ref)

    # 2a. The close from the odds log.
    if odds_log is not None:
        for t in ledger.values():
            if t["source"] != "live" or (t["status"] == "void" and not t.get("push")):
                continue  # a refunded draw keeps its close; a voided match doesn't need one
            ev = _log_close(t, odds_log, now)
            if ev:
                events.append(ev)
    if books_log is not None:  # Pinnacle's live close, priced as draw no bet
        for t in ledger.values():
            if t["source"] != "live" or (t["status"] == "void" and not t.get("push")):
                continue
            ev = paper.pinnacle_live(t, books_log, now, clv_fn=_dnb_clv)
            if ev:
                events.append(ev)

    # 2b. Moved fixtures, the card's price until kickoff; 3. settle.
    for t in ledger.values():
        if t["status"] != "open":
            ev = _fd_check(t, results, now)
            if ev:
                events.append(ev)
            continue
        kickoff = pd.Timestamp(t["kickoff"])
        c = cards.get((t["home"], t["away"]))
        settled_at = now.isoformat(timespec="seconds")
        if c and abs(pd.Timestamp(c["kickoff"]) - kickoff) > pd.Timedelta(
            hours=paper.VOID_MOVED_HOURS
        ):
            events.append(
                paper._update(t, now, **tr.settle_dnb(t, None, None, True), settled_at=settled_at)
            )
            continue
        if kickoff > now:
            if odds_log is None and c and fresh and pd.Timestamp(fetched) < kickoff:
                group = {m: (c.get("odds") or {}).get(m) for m in tr.LEAN_MARKETS}
                if all(group.values()) and group != t.get("close_prices"):
                    quoted = c.get("odds_updated") or fetched
                    events.append(paper._update(t, now, **_close_fields(t, group, quoted, kickoff)))
            continue
        res = paper._result(results, t)
        if res is None:
            res = paper._espn_result(espn, t)
        if res is not None:
            moved = (
                abs((pd.Timestamp(res["date"]) - kickoff.tz_convert(None).normalize()).days)
                > paper.VOID_MOVED_HOURS / 24
            )
            fields = tr.settle_dnb(t, res["home_goals"], res["away_goals"], void=moved)
            pin = {} if moved else _pinnacle(res, t)
            events.append(
                paper._update(
                    t,
                    now,
                    **fields,
                    **pin,
                    settled_at=settled_at,
                    result_source=paper._source(res),
                )
            )
        elif now - kickoff > pd.Timedelta(days=paper.VOID_AFTER_DAYS):
            events.append(
                paper._update(t, now, **tr.settle_dnb(t, None, None, True), settled_at=settled_at)
            )
    return events


# ---------- sections for data.json ----------


def overview(rules: dict[str, dict], track: str) -> dict:
    """One track's rule across leagues for the strategy list: the first league's fields,
    plus Lean's sigma per league (None = no fit with sigma_dnb, so no trades there)."""
    first = next(iter(rules.values()), tr.dnb_rule(None, track))
    out = dict(first)
    if track == "lean_dnb":
        out["sigma"] = {lg: r.get("sigma") for lg, r in rules.items()}
        out["source"] = "match_blends" if any(out["sigma"].values()) else "none"
        if out["source"] == "none":
            out["note"] = (
                "No Lean draw no bet trades yet: the blend fits (backtest/match_blends.json) "
                "carry no sigma for draw no bet. " + first["note"]
            )
    return out


def _clv_per_dollar(trades: list[dict], field: str) -> dict:
    """Mean CLV per $ staked over the settled trades with one, a refunded draw included
    (its stake faced the close like any other), with the count."""
    done = [t for t in trades if t.get("status") in ("won", "lost") or t.get("push")]
    vals = [t[field] for t in done if t.get(field) is not None]
    return {"trades": len(vals), "mean": sum(vals) / len(vals) if vals else None}


def section(
    track: str,
    trades: list[dict],
    parent_trades: list[dict],
    rule: dict,
    now: pd.Timestamp | None = None,
) -> dict:
    """One DNB track for Moneyline -> live -> strategies: paper.strategy_section over its
    trades, plus variant_of (the parent), adds "dnb", filter None, defined_at
    (tr.DNB_FROM), note, `dnb_trades` (count) and `compare`: the track's and the parent's
    trades opened since defined_at, their summaries and CLV per $ against DraftKings' and
    Pinnacle's close (descriptive; no since_defined: a DNB track isn't a cut of its parent),
    and `backcast` (backcast.block: the trades replayed from 14 Oct before defined_at)."""
    from soccer_stats import paper

    sec = paper.strategy_section(
        trades, track, tr.DNB_LABELS[track], rule, of=lambda _t: track, now=now
    )
    since = paper._utc(tr.DNB_FROM)
    mine = [t for t in trades if paper._opened_since(t, since)]
    theirs = [t for t in parent_trades if paper._opened_since(t, since)]
    sec.update(
        variant_of=rule["variant_of"],
        adds="dnb",
        filter=None,
        defined_at=tr.DNB_FROM,
        note=rule["note"],
        dnb_trades=len(trades),
        backcast=bc.block(
            trades,
            tr.DNB_FROM,
            backcast_from=bc.FROM,
            **({"sigma_from": sigma_from(rule)} if track == "lean_dnb" else {}),
        ),
        compare={
            "since": tr.DNB_FROM,
            "descriptive": True,
            "track": {"trades": len(mine), "summary": paper.portfolio_section(mine)["summary"]},
            "parent": {
                "trades": len(theirs),
                "summary": paper.portfolio_section(theirs)["summary"],
            },
            "clv_per_dollar": {
                f: {
                    "track": _clv_per_dollar(mine, f"{f}_staked"),
                    "parent": _clv_per_dollar(theirs, f),
                }
                for f in ("clv_dk", "clv_pinnacle")
            },
            "note": COMPARE_NOTE,
        },
    )
    return sec


def safe_section(track, trades, parent_trades, rule, now=None) -> dict:
    """section() for display only: a failure costs only this track."""
    try:
        return section(track, trades, parent_trades, rule, now)
    except Exception as exc:  # noqa: BLE001
        return {
            "key": track,
            "label": tr.DNB_LABELS[track],
            "variant_of": (rule or {}).get("variant_of"),
            "adds": "dnb",
            "filter": None,
            "defined_at": tr.DNB_FROM,
            "error": type(exc).__name__,
        }
