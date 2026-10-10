"""Live paper trading: log a $10 paper trade whenever a pick reaches a 12% edge.

Every scheduled build calls update_ledger, which:
1. opens a trade for each upcoming fixture whose best pick passes the trade rule
   (trades.best_pick under trades.PAPER_RULE via trades.paper_threshold: "fixed_raw" =
   12% on the model's own chance in every league; "learned" = the minimum edge learned
   from history on the blend, none learned = no new match trades), using
   DraftKings odds fetched within the last FRESH_HOURS and only before kickoff;
2. tracks the closing price of match trades: the last DraftKings price before kickoff
   in the odds log (odds_log.py; without a log, the last price seen by a build before
   kickoff), with closing line value against it and how many minutes before kickoff
   that price was quoted (GitHub throttles scheduled runs, so it can be hours);
3. settles trades once football-data has the result, adding closing line value against
   Pinnacle's close; voids a trade if kickoff moves by more than VOID_MOVED_HOURS or no
   result arrives within VOID_AFTER_DAYS.

The ledger is append-only JSON lines in paper_trades/<league>_<season>.jsonl on the
data-log branch. An "open" line holds the entry, which is never edited; later lines
are "update" events that may only set closing-price and settlement fields. Rebuilding
adds nothing new: a trade id (league, season, home, away) is opened at most once.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd

from soccer_stats import odds_log as ol
from soccer_stats import trades as tr
from soccer_stats.player_odds import PLAYER_BOOKMAKER_NAME

FRESH_HOURS = 3.0  # only open on odds fetched this recently
VOID_MOVED_HOURS = 48.0
VOID_AFTER_DAYS = 14
UPDATE_FIELDS = {
    "actual",
    "started",
    "close_odds",
    "close_prices",
    "close_fetched_at",
    "close_minutes_before",
    "clv_dk",
    "beat_close_dk",
    "clv_pinnacle",
    "status",
    "score",
    "profit",
    "settled_at",
}


def ledger_dir(log_dir: Path) -> Path:
    return Path(log_dir) / "paper_trades"


def load_ledger(log_dir: Path, league: str = "E0") -> dict[str, dict]:
    """Fold every ledger file into the current state of each trade, by id."""
    trades: dict[str, dict] = {}
    for path in sorted(ledger_dir(log_dir).glob(f"{league}_*.jsonl")):
        if path.name.startswith(f"{league}_corners_"):  # the corners ledger (its own files)
            continue
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            ev = json.loads(line)
            kind = ev.pop("type", "open")
            if kind == "open":
                trades.setdefault(ev["id"], ev)  # a duplicate open never overwrites
            elif ev.get("id") in trades:
                trades[ev["id"]].update({k: v for k, v in ev.items() if k in UPDATE_FIELDS})
    return trades


def append_events(log_dir: Path, events: list[dict], league: str = "E0") -> int:
    """Append events to their league's season ledger file (league and season from the
    trade id); returns how many were written."""
    if not events:
        return 0
    d = ledger_dir(log_dir)
    d.mkdir(parents=True, exist_ok=True)
    by_file: dict[Path, list[str]] = {}
    for ev in events:
        lg, season = ev["id"].split("|")[:2]
        by_file.setdefault(d / f"{lg or league}_{season}.jsonl", []).append(
            json.dumps(ev, separators=(",", ":"))
        )
    for path, lines in by_file.items():
        with open(path, "a") as f:
            f.write("\n".join(lines) + "\n")
    return len(events)


def model_ref(data: dict) -> dict:
    return {
        "commit": (os.environ.get("GITHUB_SHA") or "")[:7] or None,
        "xg_weight": data.get("xg_weight"),
        "matches_fit": data.get("matches_fit"),
    }


def _base_probs(card: dict) -> dict | None:
    b = card.get("p_base")
    if not b:
        return None
    return {**b, "under25": 1 - b["over25"] if b.get("over25") is not None else None}


def _update(trade: dict, now: pd.Timestamp, **fields) -> dict:
    trade.update(fields)
    return {"type": "update", "id": trade["id"], "at": now.isoformat(timespec="seconds"), **fields}


def _result(results: pd.DataFrame, trade: dict) -> dict | None:
    """The football-data result for a trade's match in its season, if played."""
    if results is None or results.empty:
        return None
    r = results[
        (results["home"] == trade["home"])
        & (results["away"] == trade["away"])
        & (results["season"].astype(str) == trade["season"])
    ]
    return r.iloc[0].to_dict() if not r.empty else None


def _minutes_before(kickoff, quoted) -> float | None:
    if not quoted:
        return None
    return round((pd.Timestamp(kickoff) - pd.Timestamp(quoted)) / pd.Timedelta(minutes=1), 1)


def _close_fields(t: dict, group: dict, quoted, kickoff) -> dict:
    """Closing fields for a match trade from its market group's prices at `quoted`."""
    clv = tr.clv(t["odds"], t["market"], group)
    return {
        "close_odds": group.get(t["market"]),
        "close_prices": group,
        "close_fetched_at": quoted,
        "close_minutes_before": _minutes_before(kickoff, quoted),
        "clv_dk": clv,
        "beat_close_dk": None if clv is None else bool(clv > 0),
    }


def _log_close(t: dict, log: pd.DataFrame, now: pd.Timestamp) -> dict | None:
    """An update event when the odds log holds a later close than the trade has."""
    market = "h2h" if t["market"] in ol.MARKETS["h2h"] else "totals"
    r = ol.last_before(log, t["home"], t["away"], t["kickoff"], market)
    if r is None:
        return None
    quoted = r["fetched_at"].isoformat(timespec="seconds")
    group = {m: r["prices"].get(m) for m in tr.GROUPS[t["market"]]}
    if quoted == t.get("close_fetched_at") and group == t.get("close_prices"):
        return None
    if t.get("close_fetched_at") and pd.Timestamp(quoted) < pd.Timestamp(t["close_fetched_at"]):
        return None  # never step the close back in time
    return _update(t, now, **_close_fields(t, group, quoted, t["kickoff"]))


def update_ledger(
    ledger: dict[str, dict],
    fixtures: list[dict],
    odds_source: dict,
    results: pd.DataFrame,
    now: pd.Timestamp,
    league: str = "E0",
    ref: dict | None = None,
    apps: pd.DataFrame | None = None,
    players_on: bool = False,
    odds_log: pd.DataFrame | None = None,
    threshold: float | None = tr.PAPER_EDGE,
    rule: dict | None = None,
    lean: dict | None = None,
) -> tuple[list[dict], str | None]:
    """Open, update and settle paper trades. Mutates `ledger`; returns (new events, note).

    `fixtures` are the app's fixture cards (p, p_base, odds, kickoff, low_data, players,
    ...). Player trades open only when `players_on` (the player model passed its gate)
    and the trades.PLAYER_PAPER_TRADES switch is on; open ones settle on Understat's
    counts in `apps` either way. The note explains why nothing could be
    opened, if so. With `odds_log` (odds_log.load), every live match trade's close is
    the last logged DraftKings price before kickoff; without it, the price each build
    sees until kickoff. Match trades open at `threshold` (trades.paper_threshold); None
    opens no new match trades, while open ones still get their close and settle. `rule`
    (trades.paper_threshold's dict) picks the chance: p_source "model" trades on the
    card's raw `p`; otherwise p_bet (the blend) when set, else p. New trades record the
    rule name and p_source, and `strategy` "edge12".

    `lean` (trades.lean_rule) runs the second strategy beside it on the same gates (before
    kickoff, fresh DraftKings odds, not low_data, priced): trades.lean_pick on the card's
    blended `p_bet` at the rule's sigma, $10, one Lean trade per match (id "...|lean"),
    recording z, tier, sigma, p_bet and the raw model chance. Without a sigma, no Lean
    trades. Closes, CLV and settlement are the same for both.
    """
    rule = rule or {}
    raw = rule.get("p_source") == "model"
    events: list[dict] = []
    note = None
    fetched = odds_source.get("fetched_at")
    fresh = (
        odds_source.get("name") == "DraftKings"
        and fetched is not None
        and now - pd.Timestamp(fetched) <= pd.Timedelta(hours=FRESH_HOURS)
    )
    if odds_source.get("name") != "DraftKings":
        note = "DraftKings odds unavailable, so no new paper trades this update."
    elif not fresh:
        note = f"DraftKings odds are over {FRESH_HOURS:g} hours old, so no new trades this update."

    cards = {(c["home"], c["away"]): c for c in fixtures if c.get("kickoff")}

    # 1. Open.
    sigma = (lean or {}).get("sigma")
    if fresh and sigma:
        for c in cards.values():
            kickoff = pd.Timestamp(c["kickoff"])
            if kickoff <= now or c.get("low_data"):
                continue
            ev = _open_lean(ledger, c, kickoff, now, fetched, league, ref, lean)
            if ev:
                events.append(ev)
    if fresh and threshold is not None:
        for c in cards.values():
            kickoff = pd.Timestamp(c["kickoff"])
            if kickoff <= now or c.get("low_data"):
                continue
            # The raw model under fixed_raw; else p_bet (the blend) when set, as bestPick.
            probs = c["p"] if raw else (c.get("p_bet") or c["p"])
            pick = tr.best_pick(probs, c.get("odds") or {}, threshold)
            if not pick:
                continue
            tid = tr.trade_id(league, tr.season_label(kickoff), c["home"], c["away"])
            if tid in ledger:
                continue
            base = _base_probs(c)
            t = tr.new_trade(
                pick,
                source="live",
                league=league,
                home=c["home"],
                away=c["away"],
                kickoff=kickoff,
                opened_at=now,
                odds_fetched_at=fetched,
                threshold=threshold,
                model_p_base=base.get(pick["market"]) if base else None,
                news_applied=bool(c.get("news_applied")),
                model_ref={
                    **(ref or {}),
                    "probs": "blend" if (c.get("p_bet") and not raw) else "raw",
                },
                rule=rule.get("rule"),
                p_source="model" if raw else ("blend" if c.get("p_bet") else "model"),
                strategy="edge12",
            )
            # Until a later price arrives, the entry price is the last one seen.
            group = {m: c["odds"].get(m) for m in tr.GROUPS[t["market"]]}
            quoted = c.get("odds_updated") or fetched
            t.update(_close_fields(t, group, quoted, kickoff.isoformat()))
            ledger[tid] = t
            events.append({"type": "open", **t})

    if players_on and tr.PLAYER_PAPER_TRADES:
        events += _open_player_trades(ledger, cards, now, league, ref)

    # 2a. The close from the odds log, for every live match trade (settled ones too, so
    # trades opened before the log existed get their close once it covers them).
    if odds_log is not None:
        for t in ledger.values():
            if t["source"] != "live" or t.get("bet_type") == "player" or t["status"] == "void":
                continue
            ev = _log_close(t, odds_log, now)
            if ev:
                events.append(ev)

    # 2. Track the close and catch moved fixtures; 3. settle.
    for t in ledger.values():
        if t["status"] != "open" or t["source"] != "live":
            continue
        kickoff = pd.Timestamp(t["kickoff"])
        c = cards.get((t["home"], t["away"]))
        if c and abs(pd.Timestamp(c["kickoff"]) - kickoff) > pd.Timedelta(hours=VOID_MOVED_HOURS):
            events.append(
                _update(
                    t,
                    now,
                    **tr.settle(t, None, None, void=True),
                    settled_at=now.isoformat(timespec="seconds"),
                )
            )
            continue
        if t.get("bet_type") == "player":
            ev = _player_update(t, c, apps, now)
            if ev:
                events.append(ev)
            continue
        if kickoff > now:
            if odds_log is None and c and fresh and pd.Timestamp(fetched) < kickoff:
                group = {m: (c.get("odds") or {}).get(m) for m in tr.GROUPS[t["market"]]}
                if group.get(t["market"]) and group != t.get("close_prices"):
                    quoted = c.get("odds_updated") or fetched
                    events.append(_update(t, now, **_close_fields(t, group, quoted, kickoff)))
            continue
        res = _result(results, t)
        if res is not None:
            moved = (
                abs((pd.Timestamp(res["date"]) - kickoff.tz_convert(None).normalize()).days)
                > VOID_MOVED_HOURS / 24
            )
            pin = {m: res.get(f"close_{m}") for m in tr.MARKETS}
            events.append(
                _update(
                    t,
                    now,
                    **tr.settle(t, res["home_goals"], res["away_goals"], void=moved),
                    clv_pinnacle=None if moved else tr.clv(t["odds"], t["market"], pin),
                    settled_at=now.isoformat(timespec="seconds"),
                )
            )
        elif now - kickoff > pd.Timedelta(days=VOID_AFTER_DAYS):
            events.append(
                _update(
                    t,
                    now,
                    **tr.settle(t, None, None, void=True),
                    settled_at=now.isoformat(timespec="seconds"),
                )
            )
    return events, note


def _open_lean(ledger, c, kickoff, now, fetched, league, ref, lean) -> dict | None:
    """Open the Lean trade for one card (trades.lean_pick on its p_bet), unless the match
    already has one or nothing reaches 1 sigma; returns its open event."""
    pick = tr.lean_pick(c.get("p_bet"), c.get("odds"), lean["sigma"], lean.get("min_z", tr.LEAN_Z))
    if not pick:
        return None
    t = tr.new_trade(
        pick,
        source="live",
        league=league,
        home=c["home"],
        away=c["away"],
        kickoff=kickoff,
        opened_at=now,
        odds_fetched_at=fetched,
        threshold=lean.get("min_z", tr.LEAN_Z),
        news_applied=bool(c.get("news_applied")),
        model_ref={**(ref or {}), "probs": "blend"},
        rule=tr.LEAN_RULE,
        p_source="blend",
        strategy="lean",
    )
    if t["id"] in ledger:
        return None
    t.update(
        z=round(pick["z"], 3),
        tier=pick["tier"],
        sigma=round(pick["sigma"], 5),
        p_bet=round(pick["p_bet"], 4),
        p_model=round(float(c["p"][pick["market"]]), 4)
        if (c.get("p") or {}).get(pick["market"]) is not None
        else None,
    )
    group = {m: c["odds"].get(m) for m in tr.GROUPS[t["market"]]}
    quoted = c.get("odds_updated") or fetched
    t.update(_close_fields(t, group, quoted, kickoff.isoformat()))
    ledger[t["id"]] = t
    return {"type": "open", **t}


def _fresh(fetched: str | None, now: pd.Timestamp) -> bool:
    return fetched is not None and now - pd.Timestamp(fetched) <= pd.Timedelta(hours=FRESH_HOURS)


def _player_lines(c: dict, now: pd.Timestamp) -> pd.DataFrame:
    rows = [
        {
            "home": c["home"],
            "away": c["away"],
            "player": p["player"],
            "player_id": p["player_id"],
            "team": p["team"],
            "position": p.get("position"),
            **ln,
        }
        for p in c.get("players") or []
        for ln in p.get("lines", [])
        if _fresh(ln.get("fetched_at"), now)
    ]
    return pd.DataFrame(rows)


def _open_player_trades(ledger, cards, now, league, ref) -> list[dict]:
    """Player paper trades: the player rule (best line and side per player and market,
    at most MAX_PLAYER_TRADES per match, counting ones already open)."""
    events = []
    for c in cards.values():
        kickoff = pd.Timestamp(c["kickoff"])
        if kickoff <= now:
            continue
        lines = _player_lines(c, now)
        if lines.empty:
            continue
        season = tr.season_label(kickoff)
        lines["id"] = [
            tr.trade_id(league, season, c["home"], c["away"], p, m)
            for p, m in zip(lines["player"], lines["market"], strict=True)
        ]
        lines = lines[~lines["id"].isin(ledger)]
        have = sum(
            1
            for t in ledger.values()
            if t.get("bet_type") == "player"
            and (t["home"], t["away"], t["season"]) == (c["home"], c["away"], season)
        )
        picks = tr.player_picks(lines, tr.PAPER_EDGE, max(tr.MAX_PLAYER_TRADES - have, 0))
        for r in picks.to_dict("records"):
            t = tr.new_trade(
                {
                    "market": r["market"],
                    "odds": r["odds"],
                    "model_p": r["p"],
                    "edge": r["edge"],
                    "line": r["line"],
                    "side": r["side"],
                },
                source="live",
                league=league,
                home=c["home"],
                away=c["away"],
                kickoff=kickoff,
                opened_at=now,
                odds_fetched_at=r.get("fetched_at"),
                threshold=tr.PAPER_EDGE,
                model_ref=ref,
                player={"player": r["player"], "player_id": r["player_id"], "team": r["team"]},
            )
            t.update(
                close_odds=t["odds"],
                close_fetched_at=r.get("fetched_at"),
                clv_dk=(t["odds"] * r["implied"] - 1) if r.get("implied") else None,
            )
            t["position"] = r.get("position")
            t["implied"] = r.get("implied")
            t["bookmaker"] = PLAYER_BOOKMAKER_NAME
            ledger[t["id"]] = t
            events.append({"type": "open", **t})
    return events


def _player_update(t: dict, c: dict | None, apps: pd.DataFrame | None, now: pd.Timestamp):
    """Close tracking before kickoff; settlement on Understat's counts after it."""
    kickoff = pd.Timestamp(t["kickoff"])
    if kickoff > now:
        if not c:
            return None
        for p in c.get("players") or []:
            if p["player_id"] != t["player_id"]:
                continue
            for ln in p.get("lines", []):
                same = (ln["market"], ln["line"], ln["side"]) == (t["market"], t["line"], t["side"])
                if (
                    same
                    and _fresh(ln.get("fetched_at"), now)
                    and pd.Timestamp(ln["fetched_at"]) < kickoff
                ):
                    if ln["odds"] != t.get("close_odds"):
                        clv = t["odds"] * ln["implied"] - 1 if ln.get("implied") else None
                        return _update(
                            t,
                            now,
                            close_odds=ln["odds"],
                            close_fetched_at=ln["fetched_at"],
                            clv_dk=clv,
                        )
        return None
    if apps is not None and not apps.empty:
        m = apps[
            apps["team"].isin([t["home"], t["away"]])
            & apps["opponent"].isin([t["home"], t["away"]])
            & ((apps["kickoff"] - kickoff).abs() <= pd.Timedelta(days=2))
        ]
        if not m.empty:  # match data is in: he either played or didn't
            row = m[m["player_id"] == t["player_id"]]
            actual = (
                int(row["shots" if t["market"] == "player_shots" else "sot"].iloc[0])
                if len(row)
                else None
            )
            started = bool(row["started"].iloc[0]) if len(row) else None
            return _update(
                t,
                now,
                **tr.settle_player(t, actual, started),
                settled_at=now.isoformat(timespec="seconds"),
            )
    if now - kickoff > pd.Timedelta(days=VOID_AFTER_DAYS):
        return _update(
            t, now, **tr.settle_player(t, None, None), settled_at=now.isoformat(timespec="seconds")
        )
    return None


# ---------- team corners: the owner's live test (10 Oct 2026) ----------

CORNER_UPDATE_FIELDS = {
    "close_odds",
    "close_prices",
    "close_fetched_at",
    "close_minutes_before",
    "close_snapshot",
    "clv_pinnacle",
    "beat_close_pinnacle",
    "actual",
    "push",
    "status",
    "score",
    "profit",
    "settled_at",
}
KICKOFF_TOLERANCE = pd.Timedelta(hours=3)


def corner_ledger_files(log_dir: Path, league: str) -> list[Path]:
    return sorted(ledger_dir(log_dir).glob(f"{league}_corners_*.jsonl"))


def load_corner_ledger(log_dir: Path, league: str) -> dict[str, dict]:
    """The corners portfolio's trades in a league (paper_trades/<code>_corners_<season>
    .jsonl): "open" lines hold the entry, "update" lines only CORNER_UPDATE_FIELDS."""
    trades: dict[str, dict] = {}
    for path in corner_ledger_files(log_dir, league):
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            ev = json.loads(line)
            kind = ev.pop("type", "open")
            if kind == "open":
                trades.setdefault(ev["id"], ev)
            elif ev.get("id") in trades:
                trades[ev["id"]].update({k: v for k, v in ev.items() if k in CORNER_UPDATE_FIELDS})
    return trades


def append_corner_events(log_dir: Path, events: list[dict]) -> int:
    """Append corner events to paper_trades/<code>_corners_<season>.jsonl (league and
    season from the trade id)."""
    if not events:
        return 0
    d = ledger_dir(log_dir)
    d.mkdir(parents=True, exist_ok=True)
    by_file: dict[Path, list[str]] = {}
    for ev in events:
        lg, season = ev["id"].split("|")[:2]
        by_file.setdefault(d / f"{lg}_corners_{season}.jsonl", []).append(
            json.dumps(ev, separators=(",", ":"))
        )
    for path, lines in by_file.items():
        with open(path, "a") as f:
            f.write("\n".join(lines) + "\n")
    return len(events)


def _corner_close(t: dict, q: dict) -> dict:
    """Close fields for a corner trade from a Pinnacle quote at its line: the price of
    its side, both prices, when we downloaded it and CLV vs the de-margined close."""
    clv = tr.corner_clv(t["odds"], t["side"], q["over"], q["under"])
    got = q["downloaded_at"]
    return {
        "close_odds": q[t["side"]],
        "close_prices": {"over": q["over"], "under": q["under"]},
        "close_fetched_at": got,
        "close_minutes_before": _minutes_before(t["kickoff"], got),
        "close_snapshot": q.get("snapshot"),
        "clv_pinnacle": clv,
        "beat_close_pinnacle": None if clv is None else bool(clv > 0),
    }


def _corner_result(results: pd.DataFrame | None, t: dict) -> dict | None:
    if results is None or results.empty:
        return None
    r = results[
        (results["home"] == t["home"])
        & (results["away"] == t["away"])
        & (results["season"].astype(str) == t["season"])
    ]
    if "league" in r and t.get("league"):
        r = r[r["league"] == t["league"]]
    return r.iloc[0].to_dict() if not r.empty else None


def update_corner_ledger(
    ledger: dict[str, dict],
    cards: list[dict],
    rows: list[dict],
    results: pd.DataFrame | None,
    now: pd.Timestamp,
    league: str,
    ref: dict | None = None,
) -> list[dict]:
    """Open, close and settle corner paper trades in one league. Mutates `ledger`.

    `rows` are the logged Pinnacle team-corner rows (corners_live.load_rows); `cards`
    the build's fixture cards with the model (f) (`corners.home/away.cdf`).
    1. Open: for each team line's newest quote downloaded before kickoff, within
       FRESH_HOURS of now, on a card whose kickoff is still ahead: the better of over and
       under (corners_live.pick at trades.CORNERS_EDGE), one trade per match, team and
       line, ever.
    2. Close: the newest quote at the trade's team line before kickoff (the close
       snapshot when it was taken), with CLV vs Pinnacle's de-margined close.
    3. Settle on football-data's corner counts (HC/AC); a whole line that lands exactly is
       a push (void, stake back); a moved match or no result after VOID_AFTER_DAYS: void.
    P/L and CLV only: nothing here scores the model against the league average.
    """
    from soccer_stats import corners_live as cl

    rule = tr.corners_rule()
    events: list[dict] = []
    quotes = cl.newest_quotes([r for r in rows if (r.get("league") or league) == league])
    cards_by = {(c["home"], c["away"]): c for c in cards if c.get("kickoff")}

    # 1. Open.
    for (_, h, a, k, side, line), q in sorted(quotes.items(), key=lambda kv: str(kv[0])):
        c = cards_by.get((h, a))
        if c is None:
            continue
        kickoff = cl._ts(c["kickoff"])
        if abs(kickoff - k) > KICKOFF_TOLERANCE or kickoff <= now:
            continue
        got = cl._ts(q["downloaded_at"])
        if got > now or now - got > pd.Timedelta(hours=FRESH_HOURS):
            continue
        model = (c.get("corners") or {}).get(side) or {}
        p = cl.pick(model.get("cdf"), line, q["over"], q["under"], rule["threshold"])
        if not p:
            continue
        t = tr.new_corner_trade(
            p,
            league=league,
            home=h,
            away=a,
            team_side=side,
            line=line,
            kickoff=kickoff,
            opened_at=now,
            odds_fetched_at=q["downloaded_at"],
            snapshot=q.get("snapshot"),
            ref=ref,
        )
        if t["id"] in ledger:
            continue
        t["model_mean"] = model.get("mean")
        t.update(_corner_close(t, q))  # until a later quote, the entry price is the close
        ledger[t["id"]] = t
        events.append({"type": "open", **t})

    # 2. Close, and 3. settle.
    for t in ledger.values():
        if t.get("source") != "live" or t.get("status") == "void":
            continue
        k = cl._ts(t["kickoff"])
        q = None
        for (_, h, a, kq, side, line), r in quotes.items():
            if (h, a, side, line) == (t["home"], t["away"], t["team_side"], float(t["line"])):
                if abs(kq - k) <= KICKOFF_TOLERANCE:
                    if q is None or cl._ts(r["downloaded_at"]) > cl._ts(q["downloaded_at"]):
                        q = r
        if q is not None and cl._ts(q["downloaded_at"]) < k:
            newer = not t.get("close_fetched_at") or cl._ts(q["downloaded_at"]) > cl._ts(
                t["close_fetched_at"]
            )
            if newer:
                events.append(_update(t, now, **_corner_close(t, q)))
        if t["status"] != "open" or k > now:
            continue
        res = _corner_result(results, t)
        if res is not None and not (
            pd.isna(res.get("home_corners")) or pd.isna(res.get("away_corners"))
        ):
            moved = (
                abs((pd.Timestamp(res["date"]) - k.tz_convert(None).normalize()).days)
                > VOID_MOVED_HOURS / 24
            )
            count = res["home_corners" if t["team_side"] == "home" else "away_corners"]
            fields = tr.settle_corner(t, None if moved else count)
            events.append(
                _update(
                    t,
                    now,
                    **fields,
                    score=f"{int(res['home_corners'])}-{int(res['away_corners'])} corners",
                    settled_at=now.isoformat(timespec="seconds"),
                )
            )
        elif now - k > pd.Timedelta(days=VOID_AFTER_DAYS):
            events.append(
                _update(
                    t,
                    now,
                    **tr.settle_corner(t, None),
                    settled_at=now.isoformat(timespec="seconds"),
                )
            )
    return events


CORNERS_RESEARCH_FILE = Path(__file__).resolve().parent / "lab" / "markets_research.json"


def corners_research(path: Path = CORNERS_RESEARCH_FILE) -> dict | None:
    """The corners portfolio's "backtest": the research record (no historical corner
    prices exist, so there is no priced backtest and no money). Corners bake-off 2's
    development table, round 11's 2024/25 holdout and round 12's 2025/26 recalibration,
    from the committed lab/markets_research.json. None if the file is unreadable."""
    try:
        c = json.loads(Path(path).read_text())["corners"]
    except Exception:  # noqa: BLE001
        return None
    return {
        "kind": "research",
        "title": "Research record (no historical corner prices, so no priced backtest)",
        "note": "Football-data has no corner prices, so this strategy can't be replayed for "
        "money. These are the model tests behind it; the locked February test (2026/27 "
        "matches to 31 Jan 2027, opened no earlier than 3 Feb 2027) decides whether the "
        "model is right.",
        "verdict": c.get("verdict"),
        "development": c.get("development"),
        "holdout": c.get("holdout"),
        "recalibration": c.get("recalibration"),
        "source": "lab/markets_research.json",
    }


def portfolio_section(trades: list[dict]) -> dict:
    """Trades (newest first) plus their report, for data.json."""
    df = pd.DataFrame(trades)
    rep = tr.report(df) if not df.empty else {"summary": tr.summarize(df), "breakdowns": {}}
    by_type = {}
    if not df.empty:
        bt = df.get("bet_type", pd.Series("match", index=df.index)).fillna("match")
        for kind in ("match", "player"):
            sub = df[bt == kind]
            if not sub.empty:
                by_type[kind] = tr.report(sub)
    return {
        "trades": sorted(trades, key=lambda t: t["kickoff"], reverse=True),
        **rep,
        "by_bet_type": by_type,
    }


def by_league(trades: list[dict]) -> dict | None:
    """Summary and breakdowns per competition (each trade's football-data `league`, E0 when
    missing), for the app's competition filter; None with one league or none. No trade lists:
    the app filters the section's own trades."""
    groups: dict[str, list[dict]] = {}
    for t in trades:
        groups.setdefault(t.get("league") or "E0", []).append(t)
    if len(groups) < 2:
        return None
    out = {}
    for lg, sub in sorted(groups.items()):
        sec = portfolio_section(sub)
        out[lg] = {"summary": sec["summary"], "breakdowns": sec["breakdowns"]}
    return out


def strategy_section(trades: list[dict], key: str, label: str, rule: dict | None) -> dict:
    """One match strategy's live record (Moneyline -> live -> strategies): its rule, summary,
    breakdowns, per-league view and its trades' ids, newest first (the trades themselves
    are in the section's combined `trades`, so data.json carries each trade once)."""
    mine = [t for t in trades if tr.strategy_of(t) == key]
    sec = portfolio_section(mine)
    return {
        "key": key,
        "label": label,
        "rule": rule,
        "summary": sec["summary"],
        "breakdowns": sec["breakdowns"],
        "by_league": by_league(mine),
        "trade_ids": [t["id"] for t in sec["trades"]],
    }


def lean_overview(leans: dict[str, dict]) -> dict:
    """The Lean rule across leagues for the strategy list: the shared fields, sigma per
    league and one note (the rule's own when any league has a fit, else why not)."""
    first = next(iter(leans.values()), tr.lean_rule(None, "E0"))
    live = [r for r in leans.values() if r.get("sigma")]
    return {
        **{k: first[k] for k in ("key", "rule", "min_z", "strong_z", "p_source", "stake")},
        "sigma": {lg: r.get("sigma") for lg, r in leans.items()},
        "source": "match_blends" if live else "none",
        "note": (live[0] if live else first)["note"],
    }


def match_strategies(leans: dict[str, dict], rule: dict) -> list[dict]:
    """Moneyline's strategies in tr.STRATEGY_ORDER (Lean first): key, label, rule."""
    edge_label = (
        tr.STRATEGY_LABELS["edge12"]
        if rule.get("rule") == "fixed_raw"
        else "Learned minimum edge (blend)"
    )
    rules = {"lean": lean_overview(leans), "edge12": rule}
    labels = {**tr.STRATEGY_LABELS, "edge12": edge_label}
    return [{"key": k, "label": labels[k], "rule": rules[k]} for k in tr.STRATEGY_ORDER]


def portfolios_section(
    live: dict,
    live_trades: list[dict],
    backtests: dict[str, dict | None],
    match_rule: dict | None = None,
    sections: dict[str, dict] | None = None,
    strategies: list[dict] | None = None,
) -> list[dict]:
    """One entry per trades.PORTFOLIOS: id, name, status, note, and its own live and
    backtest sections (portfolio_section over that portfolio's trades only).

    `live` carries the ledger's error and note, shared by every portfolio; `backtests`
    maps a portfolio id to its backtest file's contents (or None). `match_rule`
    (trades.paper_threshold) goes on Moneyline's live section as `rule`, and its note
    (why no new trades, or the fallback) becomes that section's note when there's no other.
    `sections` overrides a portfolio's live `error`, `note` and adds its `rule` (corners:
    its own ledger and rule). A backtest of `kind` "research" (corners: the research
    record, no prices) passes through unchanged. `strategies` (match_strategies) gives
    Moneyline's live section `strategies`: one strategy_section each, in that order; its
    combined trades and summary stay for older app code.
    """
    out = []
    for p in tr.PORTFOLIOS:
        mine = [t for t in live_trades if tr.portfolio_of(t) == p["id"]]
        bt = backtests.get(p["id"])
        research = bt is not None and bt.get("kind") == "research"
        if bt is not None and "summary" not in bt and not research:  # trades-only file
            trades = [t for t in bt.get("trades") or [] if tr.portfolio_of(t) == p["id"]]
            bt = {
                "generated_at": bt.get("generated_at"),
                "seasons": bt.get("seasons"),
                "edge_threshold": bt.get("edge_threshold"),  # research lab: minimum edge
                **portfolio_section(trades),
            }
        if bt is not None and not research and (lg := by_league(bt.get("trades") or [])):
            bt = {**bt, "by_league": lg}
        section = {**portfolio_section(mine), "error": live.get("error"), "note": live.get("note")}
        if lg := by_league(mine):
            section["by_league"] = lg
        if sections and p["id"] in sections:
            section.update(sections[p["id"]])
        if p["id"] == "moneyline" and match_rule is not None:
            section["rule"] = match_rule
            section["note"] = live.get("note") or match_rule.get("note")
        if p["id"] == "moneyline" and strategies:
            section["strategies"] = [
                strategy_section(mine, st["key"], st["label"], st["rule"]) for st in strategies
            ]
        out.append(
            {
                **{k: p[k] for k in ("id", "name", "status", "note")},
                "live": section,
                "backtest": bt,
            }
        )
    return out


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except ValueError:
        return None


def _add_league_levels(bt: dict | None, codes: list[str], rules: dict, log_dir, primary: str):
    """Moneyline backtest -> edge_threshold.by_league: each league's own learned level (its
    <code>_dk.json edge_threshold), or min_edge None with the rule's note when it has none,
    so the app flags exactly what the paper rule would trade. Only with 2+ leagues."""
    et = (bt or {}).get("edge_threshold")
    if not isinstance(et, dict) or len(codes) < 2:
        return
    by = {}
    for lg in codes:
        own = et if lg == primary else (league_backtest(log_dir, lg) or {}).get("edge_threshold")
        by[lg] = own if isinstance(own, dict) else {"min_edge": None, "note": rules[lg]["note"]}
    bt["edge_threshold"] = {**et, "by_league": by}


def leagues_in_play(data: dict, primary: str = "E0") -> list[str]:
    """The primary league plus every league with fixtures in this build (data.json
    `leagues`, or the fixtures' own `league`), primary first."""
    codes = [primary]
    listed = [lg["code"] for lg in data.get("leagues") or [] if lg.get("fixtures")]
    tagged = [f.get("league") for f in data.get("fixtures") or [] if f.get("league")]
    for c in listed + tagged:
        if c not in codes:
            codes.append(c)
    return codes


LAB_LEVELS = Path(__file__).resolve().parent / "lab" / "min_edge.json"


def league_backtest(log_dir: Path, league: str) -> dict | None:
    """The file a league's minimum edge comes from: backtest/<code>_dk.json on data-log;
    for a league other than E0 without one, the research lab's committed levels
    (lab/min_edge.json -> leagues[code], an edge_threshold dict) wrapped as
    {"edge_threshold": ...}. None when neither has the league."""
    dk = _read_json(backtest_path(log_dir, league))
    if dk is not None or league == "E0":
        return dk
    et = ((_read_json(LAB_LEVELS) or {}).get("leagues") or {}).get(league)
    return {"edge_threshold": et} if isinstance(et, dict) else None


def backtest_path(log_dir: Path, league: str = "E0") -> Path:
    """Where backtest-dk saves the app's backtest section (on the data-log branch)."""
    return Path(log_dir) / "backtest" / f"{league}_dk.json"


def run(
    data: dict,
    log_dir: Path | None,
    results: pd.DataFrame,
    league: str = "E0",
    now: pd.Timestamp | None = None,
    apps: pd.DataFrame | None = None,
    corner_results: pd.DataFrame | None = None,
) -> int:
    """Update the ledger in `log_dir` and fill data["portfolio"]; returns events written.

    Fails safe: if the ledger can't be read, nothing is opened and the tab says why.
    The corners portfolio runs on its own ledger files, the logged Pinnacle team-corner
    rows in `log_dir`/odds_log and `corner_results` (corners_live.corner_results); a
    failure there costs only the corners section.
    """
    now = now or pd.Timestamp.now(tz="UTC")
    portfolio = data.setdefault("portfolio", {})
    live = {"trades": [], "summary": {"trades": 0}, "error": None, "note": None}
    written = 0
    live_trades: list[dict] = []
    corner_trades: list[dict] = []
    corner_section: dict = {"rule": tr.corners_rule(), "note": tr.CORNERS_NOTE, "error": None}
    backtests: dict[str, dict | None] = {"corners": corners_research()}
    codes = leagues_in_play(data, league)
    files = {lg: league_backtest(log_dir, lg) if log_dir is not None else None for lg in codes}
    rules = {lg: tr.paper_threshold(files[lg], lg) for lg in codes}
    # The app's flags (edge_threshold.by_league) keep showing the learned levels and their
    # notes whatever PAPER_RULE is; the paper rule itself is in portfolio.rules.
    learned = {lg: tr.paper_threshold(files[lg], lg, rule="learned") for lg in codes}
    # The second match strategy (Blend Lean): each league's sigma from data.json's
    # match_blends (publish.add_lean); none = no Lean trades in that league.
    leans = {lg: tr.lean_rule(data.get("match_blends"), lg) for lg in codes}
    for lg in codes:
        rules[lg] = {**rules[lg], "sigma": leans[lg]["sigma"], "lean": leans[lg]}
    rule = rules[league]
    portfolio["strategy_order"] = list(tr.STRATEGY_ORDER)
    portfolio.setdefault("rule", {}).update(
        threshold=rule["threshold"],
        threshold_source=rule["source"],
        threshold_note=rule["note"],
        rule=rule.get("rule"),
        p_source=rule.get("p_source"),
        note=rule["note"],
    )
    portfolio["rules"] = rules  # per league: the minimum edge each one trades at
    if log_dir is None or not Path(log_dir).is_dir():
        live["error"] = "The paper-trade ledger is unavailable, so nothing was opened this update."
    else:
        try:
            ledger: dict[str, dict] = {}
            for lg in codes:
                ledger.update(load_ledger(log_dir, lg))
        except Exception as exc:  # corrupt or unreadable: never write blind
            live["error"] = (
                f"The paper-trade ledger couldn't be read ({type(exc).__name__}), "
                "so nothing was opened this update."
            )
        else:
            players_on = bool(((data.get("players_status") or {}).get("gate") or {}).get("passed"))
            events: list[dict] = []
            note = None
            for lg in codes:
                # Each league runs the rule on its own trades, fixtures, odds and results.
                sub = {k: t for k, t in ledger.items() if k.split("|")[0] == lg}
                cards = [c for c in data.get("fixtures", []) if (c.get("league") or league) == lg]
                res = results
                if res is not None and "league" in res and not res.empty:
                    res = res[res["league"] == lg]
                log = ol.load(log_dir, lg) if ol.log_dir(log_dir).is_dir() else None
                ev, n = update_ledger(
                    sub,
                    cards,
                    ol.source_for(data, lg, league),
                    res,
                    now,
                    lg,
                    model_ref(data),
                    apps=apps if lg == league else None,
                    players_on=players_on and lg == league,
                    odds_log=log,
                    threshold=rules[lg]["threshold"],
                    rule=rules[lg],
                    lean=leans[lg],
                )
                ledger.update(sub)
                events += ev
                if lg == league:
                    note = n
            written = append_events(log_dir, events, league)
            live_trades = list(ledger.values())
            live.update(portfolio_section(live_trades), note=note)
            try:  # the corners portfolio: its own ledger, rows and results
                n, corner_trades = _run_corners(data, log_dir, codes, league, now, corner_results)
                written += n
            except Exception as exc:  # noqa: BLE001  never costs the match ledger
                corner_section["error"] = (
                    f"The corners ledger couldn't be updated ({type(exc).__name__}), "
                    "so nothing was opened this update."
                )
        for p in tr.PORTFOLIOS:
            found = _read_json(Path(log_dir) / "backtest" / f"{league}_{p['backtest']}.json")
            if found is not None or p["id"] not in backtests:
                backtests[p["id"]] = found
        _add_league_levels(backtests.get("moneyline"), codes, learned, log_dir, league)
        bt = backtest_path(log_dir, league)
        if bt.exists():
            try:
                portfolio["backtest"] = json.loads(bt.read_text())
            except ValueError:
                portfolio["backtest"] = None
        pm = Path(log_dir) / "backtest" / f"{league}_players.json"
        if pm.exists():
            try:
                players = json.loads(pm.read_text())
            except ValueError:
                players = {}
            ptrades = players.pop("trades", [])
            portfolio["player_model"] = players
            if ptrades:  # priced player backtest trades join the match ones
                b = portfolio.get("backtest") or {}
                combined = (b.get("trades") or []) + ptrades
                merged = portfolio_section(combined)
                portfolio["backtest"] = {**b, **merged}
    portfolio["live"] = live
    # One portfolio per strategy (the app reads these); live, backtest and player_model
    # above stay for one release.
    if live.get("error") and not corner_section["error"]:
        corner_section["error"] = live["error"]
    portfolio["portfolios"] = portfolios_section(
        live,
        live_trades + corner_trades,
        backtests,
        rule,
        {"corners": corner_section},
        match_strategies(leans, rule),
    )
    return written


def _run_corners(data, log_dir, codes, primary, now, results) -> tuple[int, list[dict]]:
    """Update the corners ledger in every corner league in play; returns (events written,
    every corner trade)."""
    from soccer_stats import corners_live as cl

    rows = cl.load_rows(ol.log_dir(log_dir))
    ref = model_ref(data)
    events: list[dict] = []
    trades: list[dict] = []
    for lg in [c for c in codes if c in cl.LEAGUES]:
        ledger = load_corner_ledger(log_dir, lg)
        cards = [c for c in data.get("fixtures", []) if (c.get("league") or primary) == lg]
        res = results
        if res is not None and "league" in res and not res.empty:
            res = res[res["league"] == lg]
        lrows = [r for r in rows if r.get("league") == lg]
        events += update_corner_ledger(ledger, cards, lrows, res, now, lg, ref)
        trades += list(ledger.values())
    return append_corner_events(log_dir, events), trades
