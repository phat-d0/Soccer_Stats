"""Corner Blend Lean: a paper track in Team corners (owner's decision of 24 Nov 2026, via the
lead; pre-registered in docs/totals.md "Corner Blend Lean: pre-registration", §2-§6 with
Amendment 1).

The development gate (§4) found c's range including 0 (c -0.178, -0.866 to +0.499; b 0.39):
model (f) adds nothing measurable beside Pinnacle's price, and the blend flattens that price
toward 50%. The owner built the track anyway, knowing that; nothing in the rule changed.

The rule (one implementation with the gate: lab/corner_lean.py's look_rows, eligible, fit,
the Amendment 1a schedule fit_at_times and the §3 choice side_pick):

* Rows: Pinnacle's first two-sided look quote per team half line (kickoff from 14 Oct), with
  Pinnacle's Shin-fair over and model (f)'s p_model_over as logged then.
* Fits: logit p_bet = a + b*logit(fair) + c*logit(model) on settled rows of SP1, D1, E1, E2
  and E3 only (E0, I1 and F1 are bake-off 2's locked test; they never enter a fit), a row
  entering only when its kickoff + 1 day is before the fit and its count is known; at least
  300 lines on 150 matches; the first fit when that minimum is met, then every 28 days from
  it. Fits are stored once (data-log paper_trades/corner_lean_fits.json) and never refitted:
  the backcast's at look times before `defined_at` (one pass, on the first run), the live
  ones at paper runs.
* Picks in all eight corner leagues: each look row priced by the latest fit made at or before
  its look; z per side on Pinnacle's raw prices; the larger if z >= 1 ("strong" at 2); one
  bet per match x team, the line with the largest z; driver "market" when z with c = 0 also
  reaches 1. $10. Each trade records its fit (fit_at, a, b, c, sigma, lines, matches).
* DraftKings line (DK_KEY, descriptive, never judged): the same p_bet and sigma against
  DraftKings' raw price at the same line in the same look call (one-sided lines when the side
  is quoted), its own largest z per match x team.
* Closes per PR #258 (paper.corner_close_update: a real close or `close_missing`), CLV vs
  Pinnacle's Shin-fair close at the same line; settled like the corner tracks.
* Judged once (§5, `judged`): the first 300 Pinnacle bets opened from `defined_at` (backcast
  never counts), each with a final close; or on 31 May 2027 on whatever is there (under 300 =
  inconclusive). Pass = the 95% range of mean CLV above 0 (matches resampled, 2,000 draws,
  seed 0). The verdict is stored once and never recomputed.

Backcast from 14 Oct on the logged look rows (descriptive). Its own ledger
(paper_trades/<code>_cornerlean_<season>.jsonl), outside the corner tracks' record and
totals. No API calls.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

from soccer_stats import backcast as bc
from soccer_stats import paper_lineshop as plsh
from soccer_stats import trades as tr
from soccer_stats.lab import corner_lean as lean

KEY = "corner_lean"
LABEL = "Corner Blend Lean vs Pinnacle"
DK_KEY = "corner_lean_dk"
DK_LABEL = "Corner Blend Lean at DraftKings' price (descriptive)"
BET_TYPE = "corner_lean"
ID_TAG = "cornerlean"
DK_SUFFIX = "dk"
FILE_TAG = "cornerlean"  # <code>_cornerlean_<season>.jsonl (never matches *_corners_*)
PIN, DK = "pinnacle", "draftkings"
STAKE = 10.0
BACKCAST_FROM = "corner_log"
DEFINED_FILE = "corner_lean_defined.json"
FITS_FILE = "corner_lean_fits.json"
VERDICT_FILE = "corner_lean_verdict.json"
JUDGE_BETS = 300
DEADLINE = "2027-05-31T00:00:00+00:00"
P_SOURCE = "corner_blend"  # sizing: the blend's chance of the bet side (model_p = p_bet)

RULE_NOTE = (
    "Pinnacle's team-corner price sharpened by model (f), as Blend Lean does for match odds: "
    "logit p = a + b x logit(Pinnacle's fair over) + c x logit(model (f)'s over), fitted on "
    "settled lines in La Liga, the Bundesliga and the three EFL leagues (the Premier League, "
    "Serie A and Ligue 1 stay out of every fit until their locked test in February), refitted "
    "every 28 days. At the look (about a day before kickoff) a side is bet when its chance "
    "beats Pinnacle's raw price by at least one sigma (z >= 1; strong at 2), one $10 bet per "
    "team per match. Judged once at 300 bets on CLV against Pinnacle's close (pre-registered "
    "in docs/totals.md, 'Corner Blend Lean')."
)
GATE_NOTE = (
    "Built knowing the development gate found no model signal: c's range includes 0 (c "
    "-0.18, -0.87 to +0.50), and b is 0.39, so the blend pulls Pinnacle's price toward 50% "
    "rather than sharpening it. Picks come from that flattening, not from model (f)."
)
DK_NOTE = (
    "The same chance and sigma against DraftKings' price at the same line in the same call, "
    "its own best line per team. Descriptive only: never judged, it places no bets of its own."
)
BACKCAST_NOTE = (
    "Backcast: before {defined} this line replays the rule from {started} on the look "
    "quotes logged at the time (backcast_from corner_log), with fits made on the dates "
    "the rule would have made them (a result counts only from kickoff + 1 day). There are "
    "no picks before the first fit. Descriptive only; the track is judged on bets opened "
    "from {defined}."
)


def rule(key: str = KEY) -> dict:
    """The track's rule dict (portfolio sections, sizing and the app's rule box)."""
    return {
        "key": key,
        "rule": "corner_lean",
        "min_z": lean.LEAN_Z,
        "strong_z": lean.STRONG_Z,
        "fit_leagues": list(lean.FIT_LEAGUES),
        "locked": list(lean.LOCKED),
        "refit_days": lean.REFIT.days,
        "minimum": {"lines": lean.MIN_LINES, "matches": lean.MIN_MATCHES},
        "book": PIN if key == KEY else DK,
        "stake": STAKE,
        "p_source": P_SOURCE,
        "clv_field": "clv_pinnacle",
        "source": "preregistered",
        "judge_bets": JUDGE_BETS if key == KEY else None,
        "deadline": DEADLINE if key == KEY else None,
        "note": RULE_NOTE if key == KEY else DK_NOTE,
        "gate_note": GATE_NOTE,
    }


# ---------- files ----------


def ledger_files(log_dir: Path, league: str) -> list[Path]:
    return sorted((Path(log_dir) / "paper_trades").glob(f"{league}_{FILE_TAG}_*.jsonl"))


def load_ledger(log_dir: Path, league: str) -> dict[str, dict]:
    """A league's corner Lean trades (both lines) by id."""
    from soccer_stats import paper

    return paper._fold(ledger_files(log_dir, league), paper.CORNER_UPDATE_FIELDS)


def append_events(log_dir: Path, events: list[dict]) -> int:
    """Append events to <code>_cornerlean_<season>.jsonl (league and season from the id)."""
    if not events:
        return 0
    d = Path(log_dir) / "paper_trades"
    d.mkdir(parents=True, exist_ok=True)
    by_file: dict[Path, list[str]] = {}
    for ev in events:
        lg, season = ev["id"].split("|")[:2]
        by_file.setdefault(d / f"{lg}_{FILE_TAG}_{season}.jsonl", []).append(
            json.dumps(ev, separators=(",", ":"), default=float)
        )
    for path, lines in by_file.items():
        with open(path, "a") as f:
            f.write("\n".join(lines) + "\n")
    return len(events)


def defined_at(log_dir: Path | None, now: pd.Timestamp, write: bool = True) -> str:
    """When the track went live: paper_trades/corner_lean_defined.json, written on the first
    call with a ledger directory (the first default-branch paper run after the merge)."""
    stamp = pd.Timestamp(now).isoformat(timespec="seconds")
    if log_dir is None:
        return stamp
    path = Path(log_dir) / "paper_trades" / DEFINED_FILE
    if path.exists():
        try:
            return json.loads(path.read_text())["defined_at"]
        except (ValueError, KeyError):
            pass
    if not write:
        return stamp
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "defined_at": stamp,
        "commit": (os.environ.get("GITHUB_SHA") or "")[:7] or None,
        "note": "The first paper run of the corner Blend Lean track (its live start).",
    }
    path.write_text(json.dumps(rec, indent=1) + "\n")
    return stamp


def load_fits(log_dir: Path | None) -> dict:
    """{fits: [...], backcast_done: bool} from paper_trades/corner_lean_fits.json."""
    if log_dir is not None:
        path = Path(log_dir) / "paper_trades" / FITS_FILE
        if path.exists():
            try:
                rec = json.loads(path.read_text())
                return {"fits": list(rec.get("fits") or []), "backcast_done": True}
            except ValueError:
                pass
    return {"fits": [], "backcast_done": False}


def save_fits(log_dir: Path | None, fits: list[dict]) -> None:
    if log_dir is None:
        return
    path = Path(log_dir) / "paper_trades" / FITS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "note": "Corner Blend Lean fits (docs/totals.md §2, Amendment 1): made once, never "
        "refitted. source backcast = at a look time before defined_at, live = at a paper run.",
        "fit_leagues": list(lean.FIT_LEAGUES),
        "fits": fits,
    }
    path.write_text(json.dumps(rec, indent=1, default=float) + "\n")


# ---------- rows and fits ----------


def _utc(x) -> pd.Timestamp:
    return lean._ts(x)


def look_frame(rows: list[dict], results: list[pd.DataFrame | None]) -> pd.DataFrame:
    """lab/corner_lean.look_rows over every league, with the corner count attached for the
    fit leagues' rows only (the locked leagues' outcomes are never read here)."""
    df = lean.look_rows(rows)
    frames = [
        r[r["league"].isin(lean.FIT_LEAGUES) & ~r["league"].isin(lean.LOCKED)]
        for r in results
        if r is not None and not r.empty and "league" in r
    ]
    df = lean.attach_counts(df, *frames)  # each frame on its own (never concatenated)
    unlocked = df["league"].isin(lean.FIT_LEAGUES) & ~df["league"].isin(lean.LOCKED)
    df.loc[~unlocked, ["count", "won"]] = np.nan
    return df.sort_values("look_at", kind="stable").reset_index(drop=True)


def update_fits(
    df: pd.DataFrame, store: dict, defined: pd.Timestamp, now: pd.Timestamp
) -> tuple[list[dict], int]:
    """Stored fits extended by Amendment 1a: once, the backcast's at the look times before
    `defined` (from 14 Oct); then, live, at `now`. Returns (fits, new)."""
    fits = list(store["fits"])
    n0 = len(fits)
    if df.empty:  # no look rows logged: nothing to fit on
        return fits, 0
    if not store["backcast_done"] and not df.empty:
        times = [t for t in df["look_at"].unique() if lean.START <= _utc(t) < defined]
        for f in lean.fit_at_times(df, times, fits)[len(fits) :]:
            fits.append({**f, "source": "backcast"})
    if now >= defined:
        for f in lean.fit_at_times(df, [now], fits)[len(fits) :]:
            fits.append({**f, "source": "live"})
    return fits, len(fits) - n0


def fit_for(fits: list[dict], at: pd.Timestamp) -> dict | None:
    """The latest fit made at or before `at`."""
    live = [f for f in fits if _utc(f["fit_at"]) <= at]
    return live[-1] if live else None


# ---------- picks and trades ----------


def _dk_index(rows: list[dict]) -> dict[tuple, dict]:
    """DraftKings' look quotes by (league, home, away, kickoff, side, line, downloaded_at)."""
    out = {}
    for r in rows:
        if (r.get("book") or PIN) != DK or r.get("snapshot") != "look":
            continue
        try:
            key = (
                r.get("league"),
                r["home"],
                r["away"],
                _utc(r["kickoff"]),
                r["side"],
                float(r["line"]),
                _utc(r["downloaded_at"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
        out[key] = r
    return out


def _fresh(r: dict) -> bool:
    try:
        got = _utc(r["downloaded_at"])
        return got - _utc(r.get("fetched_at") or got) <= lean.FRESH
    except (KeyError, TypeError, ValueError):
        return False


def candidates(df: pd.DataFrame, rows: list[dict], fits: list[dict]) -> dict[str, list[dict]]:
    """Every qualifying line per track: {KEY: [...], DK_KEY: [...]}, each {row, fit, pick,
    p, pm, model_side, fair_side}. A look row is priced by the latest fit made at or before
    its look (no fit, no pick); Pinnacle's quote fresh (3 h) and before kickoff."""
    dk = _dk_index(rows)
    out: dict[str, list[dict]] = {KEY: [], DK_KEY: []}
    for row in df.to_dict("records"):
        if row["look_at"] < lean.START or row["look_at"] >= row["kickoff"]:
            continue
        if row["age"] > lean.FRESH:
            continue
        f = fit_for(fits, row["look_at"])
        if f is None:
            continue
        p, pm = lean.chances(row, f)
        got = lean.side_pick(p, pm, f["sigma"], row["over"], row["under"])
        if got:
            out[KEY].append({"row": row, "fit": f, "pick": got, "quote": None})
        q = dk.get(
            (row["league"], row["home"], row["away"], row["kickoff"], row["side"], row["line"],
             row["look_at"])
        )  # fmt: skip
        if q is not None and _fresh(q):
            got = lean.side_pick(p, pm, f["sigma"], q.get("over"), q.get("under"))
            if got:
                out[DK_KEY].append({"row": row, "fit": f, "pick": got, "quote": q})
    return out


def best_per_team(cands: list[dict]) -> list[dict]:
    """One per match x team: the line with the largest z (ties: the lower line)."""
    best: dict[tuple, dict] = {}
    for c in sorted(cands, key=lambda c: (c["row"]["look_at"], c["row"]["line"])):
        k = (c["row"]["match"], c["row"]["side"])
        if k not in best or c["pick"]["z"] > best[k]["pick"]["z"]:
            best[k] = c
    return sorted(best.values(), key=lambda c: (c["row"]["look_at"], c["row"]["match"]))


def _r(v, n=5):
    return None if v is None else round(float(v), n)


def new_trade(c: dict, key: str, defined: pd.Timestamp) -> dict:
    """A trade: tr.new_corner_trade's shape, its own id (one per match x team per line of
    the track), bet type and rule, plus how it qualified and its fit."""
    row, f, got = c["row"], c["fit"], c["pick"]
    side, odds = got["bet"], got["odds"]
    look = row["look_at"]
    q = c.get("quote")
    t = tr.new_corner_trade(
        {
            "side": side,
            "odds": odds,
            "model_p": got["p_bet"],
            "edge": got["p_bet"] * odds - 1,
            "p_push": 0.0,
        },  # fmt: skip
        league=row["league"],
        home=row["home"],
        away=row["away"],
        team_side=row["side"],
        line=row["line"],
        kickoff=row["kickoff"],
        opened_at=look,
        odds_fetched_at=look.isoformat(),
        snapshot="look",
        book=PIN,
    )
    p_model = row["model"] if side == "over" else 1 - row["model"]
    fair = row["fair"] if side == "over" else 1 - row["fair"]
    t.update(
        id=tr.trade_id(
            row["league"],
            t["season"],
            row["home"],
            row["away"],
            ID_TAG,
            row["side"],
            *((DK_SUFFIX,) if key == DK_KEY else ()),
        ),  # fmt: skip
        bet_type=BET_TYPE,
        portfolio="corners",
        strategy=key,
        bookmaker=PIN if key == KEY else DK,
        threshold=None,
        rule="corner_lean",
        p_source=P_SOURCE,
        p_bet=_r(got["p_bet"]),
        p_model=_r(p_model),
        p_fair=_r(fair),
        z=_r(got["z"], 4),
        z_market=_r(got["z_market"], 4),
        tier="strong" if got["strong"] else "lean",
        driver=got["driver"],
        sigma=_r(f["sigma"], 6),
        fit_at=f["fit_at"],
        fit_a=_r(f["a"], 6),
        fit_b=_r(f["b"], 6),
        fit_c=_r(f["c"], 6),
        fit_lines=f.get("lines"),
        fit_matches=f.get("matches"),
        fit_source=f.get("source"),
        pinnacle_entry_prices={"over": row["over"], "under": row["under"]},
        pinnacle_fair_over=_r(row["fair"]),
        look_fetched_at=(row["look_at"] - row["age"]).isoformat(),
        close_missing=None,
    )
    if q is not None:
        t["entry_prices"] = {"over": q.get("over"), "under": q.get("under")}
        t.update(variant_of=KEY, descriptive=True)
    # A look entry is never its own close: it waits for a later quote at its line.
    from soccer_stats import paper

    t["close_missing"] = paper.NO_CLOSE_AT_LINE
    if look < defined:
        bc.mark(t)
        t["backcast_from"] = BACKCAST_FROM
    return t


def open_trades(
    ledger: dict[str, dict],
    df: pd.DataFrame,
    rows: list[dict],
    fits: list[dict],
    league: str,
    defined: pd.Timestamp,
    now: pd.Timestamp,
) -> list[dict]:
    """Open each match x team's trade per track. A look before `defined` is backcast; a live
    look opens only while its kickoff is ahead. Mutates `ledger`; returns open events."""
    mine = df[df["league"] == league] if not df.empty else df
    if mine.empty or not fits:
        return []
    cands = candidates(mine[mine["look_at"] <= now], rows, fits)
    events = []
    for key in (KEY, DK_KEY):
        for c in best_per_team(cands[key]):
            row = c["row"]
            if row["look_at"] >= defined and row["kickoff"] <= now:
                continue
            t = new_trade(c, key, defined)
            if tr.holds(ledger, t["id"]):
                continue
            ledger[t["id"]] = t
            events.append({"type": "open", **t})
    return events


def update_ledger(
    ledger: dict[str, dict],
    df: pd.DataFrame,
    rows: list[dict],
    fits: list[dict],
    results: pd.DataFrame | None,
    now: pd.Timestamp,
    league: str,
    defined: pd.Timestamp,
    espn: pd.DataFrame | None = None,
) -> list[dict]:
    """Open, close (a real close at the trade's line, paper.corner_close_update) and settle
    (paper.corner_settle_update) one league's corner Lean trades. Mutates `ledger`."""
    from soccer_stats import corners_live as cl
    from soccer_stats import paper

    events = paper.duplicate_sweep(ledger, now)
    events += open_trades(ledger, df, rows, fits, league, defined, now)
    mine = [r for r in rows if (r.get("league") or league) == league]
    quotes = {b: cl.newest_quotes(mine, book=b) for b in (PIN, DK)}
    for t in ledger.values():
        if t.get("status") != "open":
            ev = paper._corner_check(t, results, now)
            if ev:
                events.append(ev)
        if t.get("status") == "void":
            continue
        book = DK if t.get("strategy") == DK_KEY else PIN
        ev = paper.corner_close_update(t, quotes, now, book)
        if ev:
            events.append(ev)
        ev = paper.corner_settle_update(t, results, espn, now)
        if ev:
            events.append(ev)
    return events


# ---------- the judged read (§5) ----------


def judged(
    trades: list[dict],
    defined: str,
    now: pd.Timestamp | None = None,
    log_dir: Path | None = None,
    verdict_file: str = VERDICT_FILE,
    target: int = JUDGE_BETS,
    deadline: str = DEADLINE,
) -> dict:
    """The pre-registered read, once: the first `target` bets opened from defined_at (not
    backcast, voids out) once every one has a final close (paper_lineshop._final), or on
    `deadline` the bets with a final close by then (fewer than `target` = inconclusive).
    Pass = the 95% range of mean CLV vs Pinnacle's close above 0 (matches resampled, 2,000
    draws, seed 0; a bet without a close counts as missing). Stored in paper_trades/
    `verdict_file` with the trade ids and never recomputed (`frozen`)."""
    now = now or pd.Timestamp.now(tz="UTC")
    out = {
        "since": defined,
        "target": target,
        "deadline": deadline,
        "verdict": None,
        "note": (
            f"Read once, when the first {target} bets opened from the start all have a final "
            f"close, or on {_utc(deadline).strftime('%-d %b %Y')} with whatever is there (under "
            f"{target}: inconclusive): the 95% range of mean CLV against Pinnacle's close must "
            "sit above 0. Until then only the count is shown."
        ),
    }
    path = Path(log_dir) / "paper_trades" / verdict_file if log_dir is not None else None
    if path is not None and path.exists():
        try:
            stored = json.loads(path.read_text())
            return {**out, **{k: v for k, v in stored.items() if k != "trades"}, "frozen": True}
        except ValueError:
            pass
    pool = plsh._judged_pool(trades, defined)
    final = [t for t in pool if plsh._final(t, now)]
    out.update(bets=len(pool), final=len(final))
    first = pool[:target]
    if len(first) == target and all(plsh._final(t, now) for t in first):
        res = plsh._verdict(first)
    elif now >= _utc(deadline):
        first = final[:target]
        res = plsh._verdict(first) if first else {"verdict": "fail", "clv": None, "missing": 0}
        if len(first) < target:
            res["verdict"] = "inconclusive"
    else:
        return out
    res.update(read=len(first), read_at=now.isoformat(timespec="seconds"))
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        rec = {**res, "since": defined, "trades": [
            {"id": t["id"], "clv_pinnacle": t.get("clv_pinnacle")} for t in first]}  # fmt: skip
        path.write_text(json.dumps(rec, indent=1) + "\n")
    return {**out, **res, "frozen": path is not None}


# ---------- sections for data.json ----------


def _fits_view(fits: list[dict]) -> list[dict]:
    keys = ("fit_at", "source", "a", "b", "c", "sigma", "lines", "matches")
    return [{k: f.get(k) for k in keys} for f in fits]


def sections(
    trades: list[dict],
    defined: str,
    fits: list[dict],
    now: pd.Timestamp | None = None,
    read: dict | None = None,
) -> list[dict]:
    """The track's two sections for Team corners -> live -> strategies: the Pinnacle line
    (KEY, judged) and the DraftKings line (DK_KEY, variant_of KEY, adds "book",
    descriptive). paper.strategy_section plus defined_at, note, gate_note, by_driver, fits,
    backcast (from 14 Oct), corner_lean_trades, and `judged` (main) or `compare` (DK)."""
    from soccer_stats import paper

    note = BACKCAST_NOTE.format(
        defined=_utc(defined).strftime("%-d %b"), started=_utc(bc.START).strftime("%-d %b")
    )
    out = []
    for key, label in ((KEY, LABEL), (DK_KEY, DK_LABEL)):
        mine = [t for t in trades if t.get("strategy") == key]
        r = rule(key)
        sec = paper.strategy_section(mine, key, label, r, of=lambda t: t.get("strategy"), now=now)
        sec.update(
            defined_at=defined,
            note=r["note"],
            gate_note=GATE_NOTE,
            by_driver=paper.by_driver(mine),
            driver_note=tr.DRIVER_NOTE,
            fits=_fits_view(fits),
            corner_lean_trades=len(mine),
            backcast=bc.block(mine, defined, note=note, backcast_from=BACKCAST_FROM),
        )
        if key == KEY:
            sec["judged"] = read if read is not None else judged(mine, defined, now)
        else:
            since = _utc(defined)
            main = [t for t in trades if t.get("strategy") == KEY]
            a, b = plsh._opened_from(mine, since), plsh._opened_from(main, since)
            sec.update(
                variant_of=KEY,
                adds="book",
                filter=None,
                descriptive=True,
                compare={
                    "since": defined,
                    "descriptive": True,
                    "track": {
                        "trades": len(a),
                        "summary": paper.portfolio_section(a)["summary"],
                        "clv_pinnacle": plsh._clv_mean(a),
                    },
                    "parent": {
                        "trades": len(b),
                        "summary": paper.portfolio_section(b)["summary"],
                        "clv_pinnacle": plsh._clv_mean(b),
                    },
                    "note": "Descriptive: DraftKings' line picks its own best line per team "
                    "at its own price, so this compares the two records over the same period, "
                    "not the same bets.",
                },
            )
        out.append(sec)
    return out


def safe_sections(trades, defined, fits, now=None, read=None) -> list[dict]:
    """sections() for display only: a failure costs only this track."""
    try:
        return sections(trades, defined, fits, now, read)
    except Exception as exc:  # noqa: BLE001
        return [{"key": KEY, "label": LABEL, "defined_at": defined, "error": type(exc).__name__}]


# ---------- the run ----------


def run(
    log_dir: Path,
    codes: list[str],
    now: pd.Timestamp,
    results: pd.DataFrame | None,
    espn: pd.DataFrame | None = None,
) -> dict:
    """Fits, opens, closes and settles the track in every corner league in play; writes
    the ledger, the fits file and (once) defined_at. Returns {trades, defined_at, fits,
    judged, events, new_fits}."""
    from soccer_stats import corners_live as cl
    from soccer_stats import odds_log as ol
    from soccer_stats import paper

    rows = cl.load_rows(ol.log_dir(log_dir))
    defined = defined_at(log_dir, now, write=bool(rows))
    d = _utc(defined)
    df = look_frame(rows, [results, espn])
    store = load_fits(log_dir)
    fits, new = update_fits(df, store, d, now)
    if new or (not store["backcast_done"] and not df.empty):
        save_fits(log_dir, fits)
    events: list[dict] = []
    trades: list[dict] = []
    for lg in [c for c in codes if c in cl.LEAGUES]:
        ledger = load_ledger(log_dir, lg)
        res = paper._league_rows(results, lg)
        events += update_ledger(
            ledger, df, rows, fits, res, now, lg, d, paper._league_rows(espn, lg)
        )
        trades += list(ledger.values())
    n = append_events(log_dir, events)
    main = [t for t in trades if t.get("strategy") == KEY]
    read = judged(main, defined, now, log_dir if rows else None)
    return {
        "trades": trades,
        "defined_at": defined,
        "fits": fits,
        "judged": read,
        "events": n,
        "opened": [e for e in events if e.get("type") == "open"],
        "new_fits": new,
    }


def summary_line(res: dict) -> str:
    parts = []
    for key, name in ((KEY, "Pinnacle"), (DK_KEY, "DraftKings")):
        mine = [t for t in res["trades"] if t.get("strategy") == key]
        new = [e for e in res["opened"] if e.get("strategy") == key]
        live = sum(1 for e in new if not e.get("backcast"))
        parts.append(
            f"{name} {len(mine)} trades ({sum(map(bc.is_backcast, mine))} backcast), "
            f"{live} opened this update"
            + (f", {len(new) - live} backcast" if len(new) > live else "")
        )
    fits = res["fits"]
    last = fits[-1] if fits else None
    fit_txt = (
        f"{len(fits)} fits (latest {last['fit_at'][:16]}, b {last['b']:.3f}, c {last['c']:+.3f})"
        if last
        else "no fit yet"
    )
    r = res["judged"]
    read = r["verdict"] if r.get("verdict") else f"{r.get('final', 0)} of {JUDGE_BETS} final"
    return (
        "Corner Blend Lean: " + "; ".join(parts) + f"; {fit_txt}"
        + (f", {res['new_fits']} new" if res["new_fits"] else "")
        + f"; defined_at {res['defined_at']}; judged read: {read}"
    )  # fmt: skip
