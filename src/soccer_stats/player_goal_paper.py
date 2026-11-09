"""Anytime goalscorer: a live paper track for §16's rule (owner-approved 9 Nov 2026).

Descriptive only. It never feeds, changes or replaces §16's single judged read on 1 Feb 2027
(docs/player_props.md §16); it shows what the frozen rule does live. No API calls: it reads
only what publish already logs on data-log.

**Rule** (§16's frozen rule, no new parameters):

- Model B's chance (`player_goal_lab` candidate "B": GOAL_FACTORS, lineup known) for each
  starter ESPN confirmed by the entry snapshot (`player_goal_live`'s "entry", 20–75 min
  before kickoff). Features come only from matches that kicked off earlier.
- Main line "goalscorer": B (rounded to P_DECIMALS) × the best Yes price over JUDGED_BOOKS
  (the nine bookmakers, never the exchange) − 1 ≥ T_JUDGED (0.20), at that book.
- Side line "goalscorer_dk": the same rule at DraftKings' Yes price alone (`variant_of`
  "goalscorer", descriptive; DraftKings doesn't price the market in D1).
- $10 flat, one trade per player per match ever. Ids `<code>|<season>|<home>|<away>|goal|
  <player_id>`, the side line's ending "|dk".
- Close: the close snapshot (0–20 min before kickoff). CLV is entry odds / close − 1, as
  §16 measures it: best-to-best (`clv_best`, the main measure here) and same-book
  (`clv_same`).
- Settle: scorers from ESPN's scoreboard goal details (football-data has no scorers and
  Understat is down); own goals don't count. A player not in ESPN's latest logged XI is
  void, and so is a match with no result in 14 days.

**B exactly as the judged read computes it:** B refits per 28-day block from
`player_goals.FORWARD_START` (10 Oct) on the 730 days before the block's first kickoff,
over every earlier appearance (players with fewer than MIN_PREV_APPS get no chance). The
team xG and game state come from the match model refitted on `backtest.walk_forward`'s
weekly grid from 2022-07-01 (xG weight 0.7, goals only when any season lacks xG;
FIT_OPTIONS), as in `cli._goal_league_feats`.

Where live can't equal the judged read (sent to the lead 9 Nov):

1. Features while Understat is down: a league's match files end when Understat stopped, so
   B's features for a later match see nothing after that date. Each trade records
   `features_through`. The judged read recomputes with full files.
2. Starters: ESPN's XI at entry (team_news log), not Understat's after the match.
3. Names: ESPN's and the books' names map to Understat ids through each club's earlier
   appearances (`match_in_fixture`), not the match's own rosters.
4. A starter's position is his last starting position.

**Timing:** a match is decided once, on the first paper run after its entry snapshot is
logged (`goalscorer_checked.jsonl` records every decision, bets or not). Entries before
`defined_at` (the first paper run after the merge) are a backcast from 1 Nov, the live
log's first day (the documented exception to 14 Oct, like the line shop's 28 Oct), from
the logged entry snapshots only, never the bought history. A match decided after its
kickoff (a league whose match files came back later) is marked backcast too. Both carry
`backcast: true`, `backcast_from` "goalscorer_log".

Ledger: data-log `paper_trades/<code>_goalscorer_<season>.jsonl`, outside Moneyline's
record. A failure here costs only this track.
"""

from __future__ import annotations

import functools
import html
import json
import os
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pandas as pd

from soccer_stats import backcast as bc
from soccer_stats import trades as tr
from soccer_stats.player_data import match_in_fixture, norm
from soccer_stats.player_goal_backfill import JUDGED_BOOKS
from soccer_stats.player_goal_live import LEAGUES
from soccer_stats.player_goal_odds import MARKET
from soccer_stats.player_goal_priced import NOT_A_PLAYER, P_DECIMALS, T_JUDGED

KEY = "goalscorer"
LABEL = "Anytime goalscorer, model B at 20%"
DK_KEY = "goalscorer_dk"
DK_LABEL = "Anytime goalscorer at DraftKings' price (descriptive)"
DK_BOOK = "draftkings"
ID_SUFFIX = {KEY: "", DK_KEY: "|dk"}
STAKE = 10.0
BACKCAST_START = "2026-11-01T00:00:00+00:00"  # the live goalscorer log's first day
BACKCAST_FROM = "goalscorer_log"
DEFINED_FILE = "goalscorer_defined.json"
CHECKED_FILE = "goalscorer_checked.jsonl"
FIRST_YEAR = 2021  # cli._goal_league_feats: Understat appearances from 2021/22
MM_START = pd.Timestamp("2022-07-01")  # the match model's walk-forward grid
MM_REFIT = pd.Timedelta(days=7)
MM_LOOKBACK = pd.Timedelta(days=730)
MM_MIN_TEAM = 6
B_REFIT = pd.Timedelta(days=28)
SETTLE_AFTER = pd.Timedelta(minutes=105)
VOID_AFTER = pd.Timedelta(days=14)
BOOK_NAMES = {
    "draftkings": "DraftKings",
    "fanduel": "FanDuel",
    "betmgm": "BetMGM",
    "betrivers": "BetRivers",
    "williamhill_us": "Caesars",
    "bovada": "Bovada",
    "espnbet": "ESPN BET",
    "onexbet": "1xBet",
    "williamhill": "William Hill",
}
RULE_NOTE = (
    "Model B's chance for each starter ESPN confirmed by the entry snapshot (20 to 75 "
    "minutes before kickoff), from earlier matches only. A $10 paper trade opens when B x the "
    "best 'Yes' price among the nine bookmakers - 1 reaches 20%, at that book; one per player "
    "per match. This is §16's frozen rule run live, for display only: the test's single "
    "judged read is §16's, on 1 Feb 2027, and nothing here changes it."
)
DK_NOTE = (
    "The same rule at DraftKings' 'Yes' price alone, the book the owner bets at. Its own "
    "entries, never judged; DraftKings doesn't price the market in the Bundesliga."
)
FEATURES_NOTE = (
    "Understat has been down since 2 Nov, so B's features for later matches end at the last "
    "match file held (features_through on each trade). The judged read recomputes them when "
    "Understat returns, so a live trade can differ from the judged rule's bet."
)
BACKCAST_NOTE = (
    "Backcast: before {defined} this line replays the rule from {started} on the entry "
    "prices the live goalscorer log recorded and ESPN's lineups at the time "
    "(backcast_from goalscorer_log). It starts on 1 Nov, when that log began, not 14 Oct. "
    "A match decided after its kickoff is a backcast too. Descriptive only."
)
UPDATE_FIELDS = {
    "close_odds",
    "close_book",
    "close_best",
    "close_same",
    "close_fetched_at",
    "close_minutes_before",
    "clv_best",
    "clv_same",
    "beat_close",
    "status",
    "score",
    "scored",
    "started",
    "profit",
    "settled_at",
    "result_source",
    "void_reason",
}


def rule(key: str = KEY) -> dict:
    """The track's rule dict (sections, sizing and the app's rule box)."""
    return {
        "key": key,
        "rule": "goalscorer_b",
        "threshold": T_JUDGED,
        "books": [DK_BOOK] if key == DK_KEY else list(JUDGED_BOOKS),
        "stake": STAKE,
        "p_source": "model_b",
        "clv_field": "clv_best",
        "source": "preregistered",
        "judged": "§16, 1 Feb 2027 (this track is descriptive)",
        "note": RULE_NOTE if key == KEY else DK_NOTE,
    }


def _utc(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tz is None else t.tz_convert("UTC")


def _iso(x) -> str:
    return _utc(x).isoformat(timespec="seconds")


def _r(v, n=4):
    return None if v is None or not np.isfinite(v) else round(float(v), n)


# ---------- files ----------


def _ledger_dir(log_dir: Path) -> Path:
    return Path(log_dir) / "paper_trades"


def ledger_files(log_dir: Path, league: str) -> list[Path]:
    return sorted(_ledger_dir(log_dir).glob(f"{league}_goalscorer_*.jsonl"))


def load_ledger(log_dir: Path, league: str) -> dict[str, dict]:
    """A league's goalscorer trades (both lines) by id."""
    from soccer_stats import paper

    return paper._fold(ledger_files(log_dir, league), UPDATE_FIELDS)


def append_events(log_dir: Path, events: list[dict]) -> int:
    """Append events to <code>_goalscorer_<season>.jsonl (league and season from the id)."""
    if not events:
        return 0
    d = _ledger_dir(log_dir)
    d.mkdir(parents=True, exist_ok=True)
    by_file: dict[Path, list[str]] = {}
    for ev in events:
        lg, season = ev["id"].split("|")[:2]
        by_file.setdefault(d / f"{lg}_goalscorer_{season}.jsonl", []).append(
            json.dumps(ev, separators=(",", ":"))
        )
    for path, lines in by_file.items():
        with open(path, "a") as f:
            f.write("\n".join(lines) + "\n")
    return len(events)


def load_checked(log_dir: Path) -> dict[str, dict]:
    """Every decided match by its key (league|event id)."""
    path = _ledger_dir(log_dir) / CHECKED_FILE
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            out.setdefault(f"{r['league']}|{r['event_id']}", r)
    return out


def append_checked(log_dir: Path, rows: list[dict]) -> None:
    if not rows:
        return
    d = _ledger_dir(log_dir)
    d.mkdir(parents=True, exist_ok=True)
    with open(d / CHECKED_FILE, "a") as f:
        f.write("\n".join(json.dumps(r, separators=(",", ":")) for r in rows) + "\n")


def defined_at(log_dir: Path | None, now: pd.Timestamp, write: bool = True) -> str:
    """The track's live start: read from paper_trades/goalscorer_defined.json, written on
    the first call with a ledger directory and `write` (the first default-branch paper run
    after the merge with a live goalscorer log); otherwise `now`, nothing written."""
    stamp = _iso(now)
    if log_dir is None:
        return stamp
    path = _ledger_dir(log_dir) / DEFINED_FILE
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
        "note": "The first paper run of the goalscorer track (its live start).",
    }
    path.write_text(json.dumps(rec, indent=1) + "\n")
    return stamp


def load_team_news(log_dir: Path, league: str) -> pd.DataFrame:
    """data-log's ESPN team-news rows for one league (one per match, team and change)."""
    rows = []
    for path in sorted((Path(log_dir) / "team_news").glob(f"{league}_*.jsonl")):
        rows += [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    return pd.DataFrame(rows)


# ---------- the logged snapshots ----------


def snapshots(rows: pd.DataFrame | None, league: str, kind: str) -> dict[str, dict]:
    """Per event: the first answered snapshot of `kind` ("entry" or "close") with prices:
    {event_id, home, away, kickoff, fetched_at, prices: DataFrame(book, player, yes)}.
    A retried snapshot keeps its first fetch with rows."""
    if rows is None or rows.empty or "league" not in rows:
        return {}
    x = rows[(rows["league"] == league) & (rows["snapshot"] == kind)]
    x = x[~x["player"].astype(str).str.lower().str.strip().isin(NOT_A_PLAYER)]
    yes = pd.to_numeric(x["yes"], errors="coerce")
    x = x.assign(yes=yes)[yes > 1]
    out = {}
    for eid, g in x.groupby("event_id"):
        first = min(g["fetched_at"], key=_utc)
        g = g[g["fetched_at"] == first]
        r = g.iloc[0]
        out[str(eid)] = {
            "event_id": str(eid),
            "home": r["home"],
            "away": r["away"],
            "kickoff": _utc(r["kickoff"]),
            "fetched_at": _utc(first),
            "prices": g[["book", "player", "yes"]].reset_index(drop=True),
        }
    return out


def best_prices(prices: pd.DataFrame, books=JUDGED_BOOKS) -> dict[str, tuple[float, str]]:
    """Player name -> (best Yes, book) over `books`; ties go to the book listed first."""
    order = {b: i for i, b in enumerate(books)}
    x = prices[prices["book"].isin(order)].copy()
    if x.empty:
        return {}
    x["_o"] = x["book"].map(order)
    x = x.sort_values(["player", "yes", "_o"], ascending=[True, False, True])
    first = x.drop_duplicates("player")
    return {
        p: (float(y), b)
        for p, y, b in zip(first["player"], first["yes"], first["book"], strict=True)
    }


def lineup_at(news: pd.DataFrame | None, home: str, away: str, kickoff, at=None) -> dict | None:
    """ESPN's confirmed XI for both sides from the team-news log: the latest confirmed row
    per side fetched at or before `at` (None = any time). None unless both sides are
    confirmed. {home: [names], away: [names], fetched_at}."""
    if news is None or news.empty or "confirmed" not in news:
        return None
    k = _utc(kickoff)
    x = news[(news["home"] == home) & (news["away"] == away)]
    if x.empty:
        return None
    x = x[pd.to_datetime(x["kickoff"], utc=True).sub(k).abs() <= pd.Timedelta(hours=3)]
    x = x[x["confirmed"].astype(bool)]
    if at is not None:
        x = x[pd.to_datetime(x["fetched_at"], utc=True) <= _utc(at)]
    out = {}
    for side in ("home", "away"):
        s = x[x["side"] == side]
        if s.empty:
            return None
        last = s.loc[pd.to_datetime(s["fetched_at"], utc=True).idxmax()]
        if not last["starters"]:
            return None
        out[side] = list(last["starters"])
        out[f"{side}_at"] = str(last["fetched_at"])
    return out


# ---------- model B (as the judged read computes it) ----------


def mm_window(date) -> pd.Timestamp:
    """The start of backtest.walk_forward's weekly window holding `date` (grid from
    MM_START)."""
    d = pd.Timestamp(date)
    d = d.tz_convert(None) if d.tz is not None else d
    return MM_START + MM_REFIT * int((d.normalize() - MM_START) // MM_REFIT)


def b_block(kickoff) -> pd.Timestamp:
    """The judged read's 28-day block start for a kickoff (blocks from FORWARD_START)."""
    from soccer_stats.player_goals import FORWARD_START

    k = _utc(kickoff)
    return FORWARD_START + B_REFIT * int((k - FORWARD_START) // B_REFIT)


def match_model(matches: pd.DataFrame, err, home: str, away: str, date) -> dict:
    """{home: (team_xg, game_state), away: (...)} for one fixture from the match model refit
    at its walk-forward window (as backtest.walk_forward would predict it); NaNs when a team
    has fewer than MM_MIN_TEAM matches in the window (walk_forward skips those)."""
    from soccer_stats.markets import match_odds
    from soccer_stats.models import DixonColes
    from soccer_stats.models.player_goals import FIT_OPTIONS

    lo = mm_window(date)
    m = matches.sort_values("date").reset_index(drop=True)  # walk_forward's own order
    train = m[(m["date"] < lo) & (m["date"] >= lo - MM_LOOKBACK)]
    counts = pd.concat([train["home"], train["away"]]).value_counts()
    if min(counts.get(home, 0), counts.get(away, 0)) < MM_MIN_TEAM:
        return {home: (np.nan, np.nan), away: (np.nan, np.nan)}
    model = DixonColes(xg_weight=0.7 if not err else 0.0, options=FIT_OPTIONS).fit(train, as_of=lo)
    eh, ea = model.expected_goals(home, away)
    p = match_odds(model.score_matrix(home, away))
    gs = float(p[0] - p[2])  # [home, draw, away], as walk_forward's p_home - p_away
    return {home: (float(eh), gs), away: (float(ea), -gs)}


def history_info(matches: pd.DataFrame, err, apps: pd.DataFrame, since) -> pd.DataFrame:
    """pb.match_info over backtest.walk_forward from the weekly window holding `since` (each
    window's fit depends only on its own start, so these equal the judged read's from
    2022-07-01)."""
    from soccer_stats import backtest
    from soccer_stats import player_backtest as pb
    from soccer_stats.models import DixonColes
    from soccer_stats.models.player_goals import FIT_OPTIONS

    start = max(mm_window(since), MM_START)
    preds = backtest.walk_forward(
        matches,
        start=start,
        model_factory=functools.partial(
            DixonColes, xg_weight=0.7 if not err else 0.0, options=FIT_OPTIONS
        ),
    )
    return pb.match_info(preds, apps)


def _position(hist: pd.DataFrame, pid: str) -> str:
    """His last starting position, else his last non-substitute one, else MID."""
    mine = hist[hist["player_id"] == pid]
    for x in (mine[mine["started"].astype(bool)], mine[mine["position"] != "SUB"]):
        if len(x):
            return str(x["position"].iloc[-1])
    return "MID"


def starter_chances(
    apps: pd.DataFrame,
    info: pd.DataFrame,
    target: dict,
    starters: dict[str, list[str]],
) -> tuple[dict[str, float], dict]:
    """B's chance per starter id for one match, from appearances that kicked off before it.

    `apps`: Understat appearances (player_data.load_appearances); `info`: match_info rows
    for earlier matches (match_id, team, team_xg, game_state); `target`: {home, away,
    kickoff, season, match_model: {team: (team_xg, game_state)}}; `starters`: team -> player
    ids. Returns ({player_id: chance}, detail)."""
    from soccer_stats.factors import build_features
    from soccer_stats.models.player_goals import GOAL_FACTORS, GoalscorerModel
    from soccer_stats.player_goal_lab import LOOKBACK_DAYS, MIN_PREV_APPS
    from soccer_stats.player_goals import goal_features

    k = _utc(target["kickoff"])
    hist = apps[pd.to_datetime(apps["kickoff"], utc=True) < k].copy()
    mid = f"live|{target['home']}|{target['away']}"
    rows, mi = [], []
    for team, opp, home in (
        (target["home"], target["away"], True),
        (target["away"], target["home"], False),
    ):
        xg, gs = target["match_model"].get(team, (np.nan, np.nan))
        mi.append({"match_id": mid, "team": team, "team_xg": xg, "game_state": gs})
        for pid in starters.get(team, []):
            names = hist.loc[hist["player_id"] == pid, "player"]
            rows.append(
                {
                    "match_id": mid,
                    "season": target["season"],
                    "kickoff": k,
                    "team": team,
                    "opponent": opp,
                    "home": home,
                    "player_id": pid,
                    "player": names.iloc[-1] if len(names) else "",
                    "position": _position(hist, pid),
                    "minutes": 0,
                    "started": True,
                    "shots": 0,
                    "sot": 0,
                    "goals": 0,
                    "xg": 0.0,
                    "penalties": 0,
                    "pen_xg": 0.0,
                    "sp_xg": 0.0,
                    "team_shots": np.nan,
                    "opp_shots": np.nan,
                    "_placeholder": True,
                }
            )
    detail = {
        "features_through": _iso(hist["kickoff"].max()) if len(hist) else None,
        "block_start": _iso(b_block(k)),
    }
    if not rows or hist.empty:
        return {}, {**detail, "why": "no starters or no history"}
    allr = pd.concat([hist.assign(_placeholder=False), pd.DataFrame(rows)], ignore_index=True)
    info_all = pd.concat([info, pd.DataFrame(mi)], ignore_index=True)
    info_all = info_all.astype({"team_xg": float, "game_state": float})
    feats = goal_features(build_features(allr, info_all))
    live = feats[feats["_placeholder"].astype(bool)]
    past = feats[~feats["_placeholder"].astype(bool)]
    bs = b_block(k)
    t = pd.to_datetime(past["kickoff"], utc=True)
    first = min([*t[t >= bs], k])  # the block's first kickoff, as the judged read's test rows
    train = past[(t < bs) & (t >= first - pd.Timedelta(days=LOOKBACK_DAYS))]
    ok = live[live["prev_apps"] >= MIN_PREV_APPS]
    detail.update(train_rows=len(train), starters=len(live), eligible=len(ok))
    if len(train) < 200 or train["goals"].sum() < 10 or ok.empty:
        return {}, {**detail, "why": "too little training data or no eligible starter"}
    p = GoalscorerModel(list(GOAL_FACTORS)).fit(train).prob_score(ok, lineup_known=True)
    return {str(pid): float(v) for pid, v in zip(ok["player_id"], p, strict=True)}, detail


class BSource:
    """Loads a league's Understat appearances, football-data matches and walk-forward match
    info once per run (only when a match needs deciding) and gives B's chances."""

    def __init__(self, league: str, now: pd.Timestamp):
        self.league, self.now = league, now
        self._loaded = False
        self.apps = self.matches = self.err = None
        self.info: pd.DataFrame | None = None
        self.missing = 0  # played matches whose Understat file this run doesn't hold
        self.seasons_missing: list[str] = []  # league-seasons with no Understat league file

    def load(self) -> bool:
        if self._loaded:
            return self.apps is not None and not self.apps.empty
        self._loaded = True
        from soccer_stats.data import current_season, load_matches
        from soccer_stats.player_data import load_appearances
        from soccer_stats.xg import with_xg

        years = range(FIRST_YEAR, current_season() + 1)
        frames = []
        for y in years:  # one season at a time: a missing league file costs only its season
            try:
                a, miss = load_appearances(self.league, [y], max_new=0)
            except Exception as exc:  # noqa: BLE001  e.g. Understat's maintenance page
                self.seasons_missing.append(f"{y}/{(y + 1) % 100:02d} ({type(exc).__name__})")
                continue
            self.missing += miss
            if not a.empty:
                frames.append(a)
        apps = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        if not apps.empty:
            apps = apps.sort_values(["kickoff", "match_id", "team"]).reset_index(drop=True)
        season = f"{current_season() % 100:02d}{(current_season() + 1) % 100:02d}"
        if apps.empty or not (apps["season"].astype(str) == season).any():
            return False  # no match files for this season (Understat cache missing)
        self.apps = apps
        self.matches, self.err = with_xg(load_matches([self.league], years))
        return True

    def history(self, since) -> pd.DataFrame:
        if self.info is None:
            self.info = history_info(self.matches, self.err, self.apps, since)
        return self.info

    def match_model(self, home: str, away: str, kickoff) -> dict:
        return match_model(self.matches, self.err, home, away, kickoff)

    def rosters(self, team: str, kickoff) -> dict[str, str]:
        """Understat id -> name of everyone who played for `team` this season or last,
        before `kickoff`."""
        a = self.apps
        k = _utc(kickoff)
        season = tr.season_label(k)
        prev = f"{int(season[:2]) - 1:02d}{season[:2]}"
        x = a[(a["team"] == team) & a["season"].astype(str).isin([season, prev])]
        x = x[pd.to_datetime(x["kickoff"], utc=True) < k]
        return dict(x.drop_duplicates("player_id", keep="last")[["player_id", "player"]].values)


# ---------- deciding a match ----------


def _new_trade(key, league, entry, pid, name, team, p, odds, book, detail, lineup, defined, now):
    k = entry["kickoff"]
    pick = {"market": MARKET, "odds": odds, "model_p": p, "edge": round(p, P_DECIMALS) * odds - 1}
    t = tr.new_trade(
        pick,
        source="live",
        league=league,
        home=entry["home"],
        away=entry["away"],
        kickoff=k,
        opened_at=entry["fetched_at"],
        odds_fetched_at=_iso(entry["fetched_at"]),
        threshold=T_JUDGED,
        model_ref={"model": "B", **{x: detail.get(x) for x in ("block_start", "train_rows")}},
        stake=STAKE,
        rule="goalscorer_b",
        p_source="model_b",
        strategy=key,
    )
    t.update(
        id=tr.trade_id(league, t["season"], entry["home"], entry["away"], "goal", pid)
        + ID_SUFFIX[key],
        bet_type="goalscorer",
        portfolio="goalscorer",
        side="yes",
        player=name["understat"],
        player_id=pid,
        team=team,
        espn_name=name["espn"],
        odds_name=name["odds"],
        book=book,
        book_name=BOOK_NAMES.get(book, book),
        event_id=entry["event_id"],
        p_B=_r(p, P_DECIMALS),
        features_through=detail.get("features_through"),
        lineup_fetched_at={s: lineup.get(f"{s}_at") for s in ("home", "away")},
        decided_at=_iso(now),
    )
    if key == DK_KEY:
        t.update(variant_of=KEY, descriptive=True)
    if entry["fetched_at"] < defined or now >= k:
        bc.mark(t)
        t["backcast_from"] = BACKCAST_FROM
    return t


def decide(
    entry: dict, lineup: dict, source: BSource, league: str, defined, now
) -> tuple[list[dict], dict]:
    """The trades the rule opens on one match at its entry snapshot, and the decision record
    (written to goalscorer_checked.jsonl whether or not anything opened)."""
    home, away, k = entry["home"], entry["away"], entry["kickoff"]
    rec = {"league": league, "event_id": entry["event_id"], "home": home, "away": away,
           "kickoff": _iso(k), "entry_at": _iso(entry["fetched_at"]),
           "decided_at": _iso(now)}  # fmt: skip
    rosters = {home: source.rosters(home, k), away: source.rosters(away, k)}
    ids: dict[str, list[str]] = {}
    espn_of: dict[str, str] = {}
    for side, team in (("home", home), ("away", away)):
        found, _ = match_in_fixture(lineup[side], {team: rosters[team]})
        ids[team] = [pid for pid, _t in found.values()]
        espn_of.update({pid: n for n, (pid, _t) in found.items()})
    names = entry["prices"]["player"].unique()
    odds_found, _ = match_in_fixture(names, rosters)
    odds_of = {pid: n for n, (pid, _t) in odds_found.items()}
    target = {
        "home": home,
        "away": away,
        "kickoff": k,
        "season": tr.season_label(k),
        "match_model": source.match_model(home, away, k),
    }
    info = source.history(b_block(k) - pd.Timedelta(days=760))
    chances, detail = starter_chances(source.apps, info, target, ids)
    best = best_prices(entry["prices"])
    dk = best_prices(entry["prices"], (DK_BOOK,))
    out = []
    for team, pids in ids.items():
        for pid in pids:
            p, oname = chances.get(pid), odds_of.get(pid)
            if p is None or oname is None:
                continue
            name = {
                "understat": html.unescape(rosters[team].get(pid) or ""),  # "O&#039;Nien"
                "espn": espn_of.get(pid),
                "odds": oname,
            }
            for key, prices in ((KEY, best), (DK_KEY, dk)):
                if oname not in prices:
                    continue
                odds, book = prices[oname]
                if round(p, P_DECIMALS) * odds - 1 >= T_JUDGED:
                    out.append(
                        _new_trade(
                            key,
                            league,
                            entry,
                            pid,
                            name,
                            team,
                            p,
                            odds,
                            book,
                            detail,
                            lineup,
                            defined,
                            now,
                        )  # fmt: skip
                    )
    rec.update(
        starters=sum(len(v) for v in lineup.values() if isinstance(v, list)),
        starters_matched=sum(map(len, ids.values())),
        priced_names=len(names),
        priced_matched=len(odds_of),
        chances=len(chances),
        bets={
            KEY: sum(t["strategy"] == KEY for t in out),
            DK_KEY: sum(t["strategy"] == DK_KEY for t in out),
        },  # fmt: skip
        **{x: detail.get(x) for x in ("features_through", "block_start", "train_rows", "why")},
        match_files_missing=getattr(source, "missing", None),
        seasons_missing=getattr(source, "seasons_missing", None),
    )
    return out, rec


# ---------- close and settlement ----------


def close_fields(t: dict, close: dict | None) -> dict:
    """The close snapshot's prices for a trade: best over the line's books and the entry
    book's own; CLV = entry odds / close − 1 (§16)."""
    if close is None or t.get("close_fetched_at"):
        return {}
    books = (DK_BOOK,) if t.get("strategy") == DK_KEY else JUDGED_BOOKS
    best = best_prices(close["prices"], books)
    name = t.get("odds_name")
    pr = close["prices"]
    same = pr[(pr["player"] == name) & (pr["book"] == t["book"])]["yes"]
    cb = best.get(name)
    cs = float(same.iloc[0]) if len(same) else None
    clv_b = t["odds"] / cb[0] - 1 if cb else None
    return {
        "close_odds": cb[0] if cb else None,
        "close_book": cb[1] if cb else None,
        "close_best": cb[0] if cb else None,
        "close_same": cs,
        "close_fetched_at": _iso(close["fetched_at"]),
        "close_minutes_before": round(
            (close["kickoff"] - close["fetched_at"]).total_seconds() / 60, 1
        ),
        "clv_best": _r(clv_b),
        "clv_same": _r(t["odds"] / cs - 1) if cs else None,
        "beat_close": None if clv_b is None else bool(clv_b > 0),
    }


def goal_events(raw_event: dict) -> list[dict]:
    """ESPN scoreboard goal details: [{name, own_goal, team_id}] (shootouts left out)."""
    comp = ((raw_event.get("competitions") or [{}])[0]) or {}
    out = []
    for d in comp.get("details") or []:
        if not isinstance(d, dict) or not d.get("scoringPlay") or d.get("shootout"):
            continue
        who = [a for a in d.get("athletesInvolved") or [] if isinstance(a, dict)]
        out.append(
            {
                "name": (who[0].get("displayName") or who[0].get("fullName")) if who else None,
                "own_goal": bool(d.get("ownGoal")),
                "team_id": str((d.get("team") or {}).get("id", "")),
            }
        )
    return out


def espn_match(trade: dict, raw_dir=None, get=None, budget=None, now=None) -> dict | None:
    """The trade's final ESPN event, from espn_results' scoreboard (cached once final):
    {score90, goals: goal_events} or None (not final, not found, or the goal details don't
    add up to the score)."""
    import requests

    from soccer_stats import espn_results as er
    from soccer_stats.data import RAW_DIR

    slug = er.DOMESTIC.get(trade["league"])
    if slug is None:
        return None
    k = _utc(trade["kickoff"])
    budget = budget or er.Budget(seconds=30)
    raw = {}
    parsed = []
    for day in er._days(k):
        body = er.scoreboard(slug, day, raw_dir or RAW_DIR, get or requests.get, budget, now)
        for e in er._raw_events(body):
            raw[str(e.get("id"))] = e
        parsed += er.parse(body)
    match = {"home": trade["home"], "away": trade["away"], "kickoff": k}
    how, e = er.match_events(match, parsed, {trade["home"], trade["away"]})
    if how != "matched" or not e["final"]:
        return None
    goals = goal_events(raw.get(str(e["id"]), {}))
    if len(goals) != int(e["score90"][0]) + int(e["score90"][1]):
        return None  # the details don't account for the score: don't guess
    return {"score90": e["score90"], "goals": goals, "event_id": e["id"]}


def settle_fields(t: dict, result: dict, lineup: dict | None) -> dict:
    """Won if one of the match's goals (own goals left out) is his; void if he isn't in
    ESPN's latest logged XI."""
    side = "home" if t["team"] == t["home"] else "away"
    xi = {norm(n) for n in (lineup or {}).get(side) or []}
    score = f"{int(result['score90'][0])}-{int(result['score90'][1])}"
    if norm(t.get("espn_name") or "") not in xi:
        return {"status": "void", "profit": 0.0, "started": False, "score": score,
                "void_reason": "not in ESPN's starting XI"}  # fmt: skip
    mine = norm(t.get("espn_name") or "")
    scored = sum(1 for g in result["goals"] if not g["own_goal"] and norm(g["name"] or "") == mine)
    won = scored > 0
    return {
        "status": "won" if won else "lost",
        "profit": round(t["stake"] * (t["odds"] - 1), 2) if won else -t["stake"],
        "started": True,
        "scored": scored,
        "score": score,
        "result_source": "espn",
    }


def update_trades(
    ledger: dict[str, dict],
    closes: dict[str, dict],
    news: pd.DataFrame | None,
    now: pd.Timestamp,
    result_fn: Callable[[dict], dict | None],
) -> list[dict]:
    """Close (the close snapshot) and settle (ESPN) every trade; returns update events."""
    from soccer_stats import paper

    events = []
    results: dict[str, dict | None] = {}
    for t in ledger.values():
        f = close_fields(t, closes.get(t.get("event_id")))
        if f:
            events.append(paper._update(t, now, **f))
        if t["status"] != "open":
            continue
        k = _utc(t["kickoff"])
        if now < k + SETTLE_AFTER:
            continue
        mkey = f"{t['home']}|{t['away']}|{t['kickoff']}"
        if mkey not in results:
            results[mkey] = result_fn(t)
        res = results[mkey]
        if res is not None:
            lineup = lineup_at(news, t["home"], t["away"], t["kickoff"])
            events.append(
                paper._update(t, now, **settle_fields(t, res, lineup), settled_at=_iso(now))
            )
        elif now - k > VOID_AFTER:
            events.append(
                paper._update(
                    t,
                    now,
                    status="void",
                    profit=0.0,
                    settled_at=_iso(now),
                    void_reason="no result in 14 days",
                )  # fmt: skip
            )
    return events


# ---------- one league ----------


def update_league(
    ledger: dict[str, dict],
    checked: dict[str, dict],
    live_rows: pd.DataFrame | None,
    news: pd.DataFrame | None,
    league: str,
    defined,
    now: pd.Timestamp,
    source: BSource | None = None,
    result_fn: Callable[[dict], dict | None] | None = None,
) -> tuple[list[dict], list[dict], dict]:
    """Decide every match with a logged entry snapshot not yet decided (from BACKCAST_START,
    entry at or before `now`), then close and settle. Mutates `ledger` and `checked`;
    returns (ledger events, new decision records, counts)."""
    source = source or BSource(league, now)
    result_fn = result_fn or (lambda t: espn_match(t, now=now))
    entries = snapshots(live_rows, league, "entry")
    closes = snapshots(live_rows, league, "close")
    lo, defined = _utc(BACKCAST_START), _utc(defined)
    events, recs = [], []
    counts = {"pending": 0, "decided": 0, "no_lineup": 0, "no_data": 0}
    for eid, e in sorted(entries.items(), key=lambda x: x[1]["fetched_at"]):
        if e["kickoff"] < lo or e["fetched_at"] > now or f"{league}|{eid}" in checked:
            continue
        counts["pending"] += 1
        lineup = lineup_at(news, e["home"], e["away"], e["kickoff"], at=e["fetched_at"])
        if lineup is None:
            rec = {"league": league, "event_id": eid, "home": e["home"], "away": e["away"],
                   "kickoff": _iso(e["kickoff"]), "entry_at": _iso(e["fetched_at"]),
                   "decided_at": _iso(now), "why": "no confirmed ESPN lineup at entry",
                   "bets": {KEY: 0, DK_KEY: 0}}  # fmt: skip
            counts["no_lineup"] += 1
        else:
            if not source.load():
                counts["no_data"] += 1
                continue  # no Understat match files: decided when they come back
            new, rec = decide(e, lineup, source, league, defined, now)
            for t in new:
                if tr.holds(ledger, t["id"]):
                    continue
                ledger[t["id"]] = t
                events.append({"type": "open", **t})
            counts["decided"] += 1
        checked[f"{league}|{eid}"] = rec
        recs.append(rec)
    events += update_trades(ledger, closes, news, now, result_fn)
    return events, recs, counts


# ---------- sections for data.json ----------


def _clv(trades: list[dict], field: str) -> dict:
    vals = [t[field] for t in trades if t.get(field) is not None and t.get("status") != "void"]
    return {"trades": len(vals), "mean": float(np.mean(vals)) if vals else None}


def sections(trades: list[dict], defined: str, now: pd.Timestamp | None = None) -> list[dict]:
    """The main line (key "goalscorer") and the DraftKings line (variant_of "goalscorer",
    adds "book") for Anytime goalscorer -> live -> strategies: paper.strategy_section plus
    defined_at, note, features_note, clv (best-to-best and same-book means), the backcast
    block, goalscorer_trades, and on the DraftKings line a descriptive compare."""
    from soccer_stats import paper

    note = BACKCAST_NOTE.format(
        defined=_utc(defined).strftime("%-d %b"), started=_utc(BACKCAST_START).strftime("%-d %b")
    )
    out = []
    for key, label in ((KEY, LABEL), (DK_KEY, DK_LABEL)):
        mine = [t for t in trades if t.get("strategy") == key]
        r = rule(key)
        sec = paper.strategy_section(mine, key, label, r, of=lambda t: t.get("strategy"), now=now)
        sec.update(
            defined_at=defined,
            note=r["note"],
            features_note=FEATURES_NOTE,
            descriptive=True,
            clv={"best": _clv(mine, "clv_best"), "same_book": _clv(mine, "clv_same")},
            goalscorer_trades=len(mine),
            backcast=bc.block(
                mine,
                defined,
                note=note,
                started=BACKCAST_START,
                backcast_from=BACKCAST_FROM,
                exception="starts 1 Nov, when the live goalscorer log began (not 14 Oct)",
            ),
        )
        if key == DK_KEY:
            main = [t for t in trades if t.get("strategy") == KEY]
            sec.update(
                variant_of=KEY,
                adds="book",
                filter=None,
                compare={
                    "descriptive": True,
                    "track": {
                        "trades": len(mine),
                        "summary": paper.portfolio_section(mine)["summary"],
                        "clv_best": _clv(mine, "clv_best"),
                    },
                    "parent": {
                        "trades": len(main),
                        "summary": paper.portfolio_section(main)["summary"],
                        "clv_best": _clv(main, "clv_best"),
                    },
                    "note": "Descriptive: DraftKings' line bets only where its own price clears "
                    "20%, so it holds fewer, different bets from the main line.",
                },  # fmt: skip
            )
        out.append(sec)
    return out


def safe_sections(trades: list[dict], defined: str, now=None) -> list[dict]:
    """sections() for display only: a failure costs only this track."""
    try:
        return sections(trades, defined, now)
    except Exception as exc:  # noqa: BLE001
        return [{"key": KEY, "label": LABEL, "defined_at": defined, "error": type(exc).__name__}]


def run(log_dir: Path, now: pd.Timestamp, leagues=LEAGUES, sources=None, result_fn=None) -> dict:
    """Every goalscorer league on data-log: decide, close, settle, write. Returns {trades,
    defined_at, events, counts}."""
    from soccer_stats import player_goal_live as gl

    rows = gl.load_rows(log_dir)
    defined = defined_at(log_dir, now, write=not rows.empty)
    checked = load_checked(log_dir)
    events, recs, trades = [], [], []
    counts = {}
    for lg in leagues:
        ledger = load_ledger(log_dir, lg)
        src = (sources or {}).get(lg)
        try:  # one league's failure costs only that league this run
            ev, rc, c = update_league(
                ledger, checked, rows, load_team_news(log_dir, lg), lg, defined, now, src, result_fn
            )
        except Exception as exc:  # noqa: BLE001
            print(f"Goalscorer paper track: {lg} not updated ({type(exc).__name__}: {exc})"[:300])
            ev, rc, c = [], [], {"error": type(exc).__name__}
        events += ev
        recs += rc
        counts[lg] = c
        trades += list(ledger.values())
    n = append_events(log_dir, events)
    append_checked(log_dir, recs)
    return {"trades": trades, "defined_at": defined, "events": n, "counts": counts,
            "opened": [e for e in events if e.get("type") == "open"]}  # fmt: skip


def summary_line(res: dict) -> str:
    """The publish log's line."""
    parts = []
    for key, name in ((KEY, "best book"), (DK_KEY, "DraftKings")):
        mine = [t for t in res["trades"] if t.get("strategy") == key]
        new = [e for e in res["opened"] if e.get("strategy") == key]
        parts.append(
            f"{name} {len(mine)} trades ({sum(map(bc.is_backcast, mine))} backcast), "
            f"{sum(not e.get('backcast') for e in new)} opened this update"
        )
    waits = sum(c.get("no_data", 0) for c in res["counts"].values())
    return (
        "Goalscorer paper track: "
        + "; ".join(parts)
        + f"; defined_at {res['defined_at']}"
        + (f"; {waits} matches wait for Understat match files" if waits else "")
    )
