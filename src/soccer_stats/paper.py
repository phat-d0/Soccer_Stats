"""Live paper trading: log a $10 paper trade whenever a pick reaches a 12% edge.

Every scheduled build calls update_ledger, which:
1. opens a trade for each upcoming fixture whose best pick passes the trade rule
   (trades.best_pick at PAPER_EDGE), using DraftKings odds fetched within the last
   FRESH_HOURS and only before kickoff;
2. tracks the closing price of open trades until kickoff (the last DraftKings price
   seen before kickoff), with closing line value against it;
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
    "clv_dk",
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
    """Append events to their season's ledger file; returns how many were written."""
    if not events:
        return 0
    d = ledger_dir(log_dir)
    d.mkdir(parents=True, exist_ok=True)
    by_file: dict[Path, list[str]] = {}
    for ev in events:
        season = ev["id"].split("|")[1]
        by_file.setdefault(d / f"{league}_{season}.jsonl", []).append(
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
) -> tuple[list[dict], str | None]:
    """Open, update and settle paper trades. Mutates `ledger`; returns (new events, note).

    `fixtures` are the app's fixture cards (p, p_base, odds, kickoff, low_data, players,
    ...). Player trades open only when `players_on` (the player model passed its gate)
    and settle on Understat's counts in `apps`. The note explains why nothing could be
    opened, if so.
    """
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
    if fresh:
        for c in cards.values():
            kickoff = pd.Timestamp(c["kickoff"])
            if kickoff <= now or c.get("low_data"):
                continue
            # p_bet (the model blended with the price) when publish set it; as bestPick.
            pick = tr.best_pick(c.get("p_bet") or c["p"], c.get("odds") or {}, tr.PAPER_EDGE)
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
                threshold=tr.PAPER_EDGE,
                model_p_base=base.get(pick["market"]) if base else None,
                news_applied=bool(c.get("news_applied")),
                model_ref={**(ref or {}), "probs": "blend" if c.get("p_bet") else "raw"},
            )
            # Until a later price arrives, the entry price is the last one seen.
            group = {m: c["odds"].get(m) for m in tr.GROUPS[t["market"]]}
            t.update(
                close_odds=t["odds"],
                close_prices=group,
                close_fetched_at=fetched,
                clv_dk=tr.clv(t["odds"], t["market"], group),
            )
            ledger[tid] = t
            events.append({"type": "open", **t})

    if players_on:
        events += _open_player_trades(ledger, cards, now, league, ref)

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
            if c and fresh and pd.Timestamp(fetched) < kickoff:
                group = {m: (c.get("odds") or {}).get(m) for m in tr.GROUPS[t["market"]]}
                price = group.get(t["market"])
                if price and group != t.get("close_prices"):
                    events.append(
                        _update(
                            t,
                            now,
                            close_odds=price,
                            close_prices=group,
                            close_fetched_at=fetched,
                            clv_dk=tr.clv(t["odds"], t["market"], group),
                        )
                    )
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
) -> int:
    """Update the ledger in `log_dir` and fill data["portfolio"]; returns events written.

    Fails safe: if the ledger can't be read, nothing is opened and the tab says why.
    """
    now = now or pd.Timestamp.now(tz="UTC")
    portfolio = data.setdefault("portfolio", {})
    live = {"trades": [], "summary": {"trades": 0}, "error": None, "note": None}
    written = 0
    if log_dir is None or not Path(log_dir).is_dir():
        live["error"] = "The paper-trade ledger is unavailable, so nothing was opened this update."
    else:
        try:
            ledger = load_ledger(log_dir, league)
        except Exception as exc:  # corrupt or unreadable: never write blind
            live["error"] = (
                f"The paper-trade ledger couldn't be read ({type(exc).__name__}), "
                "so nothing was opened this update."
            )
        else:
            events, note = update_ledger(
                ledger,
                data.get("fixtures", []),
                data.get("odds_source") or {},
                results,
                now,
                league,
                model_ref(data),
                apps=apps,
                players_on=bool(
                    ((data.get("players_status") or {}).get("gate") or {}).get("passed")
                ),
            )
            written = append_events(log_dir, events, league)
            live.update(portfolio_section(list(ledger.values())), note=note)
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
    return written
