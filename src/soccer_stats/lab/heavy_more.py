"""More data for the 75%+ heavy-favourite idea (lead brief, 28 Oct): free counting only, no
key. How many favourites priced 75%+ before kickoff trailed at half-time, by competition and
season, beyond the sources `heavy_favourites` already counted, and what one in-play
snapshot per kickoff slot would cost. Nothing here reads how a match ended beyond the
half-time score.

Sources:
- domestic cups and the three UEFA club competitions (`CUPS`): ESPN scoreboards (half-time
  from the first linescore, else the goal clock), the favourite priced from ESPN's own
  pre-match moneyline where its scoreboard keeps one. Matches without one are counted
  apart with the cost of a pre-match historical snapshot per kickoff slot instead (an
  in-play snapshot carries only the in-play price, not the pre-match one);
- football-data's "extra" leagues (`EXTRA`): closing prices (Pinnacle's, else the
  average's) with the half-time score from ESPN, joined by date (one day either side), the
  90-minute score and the club names;
- the scoped leagues' newly priced earlier seasons (`EARLIER`, the archive probes of 28 Oct)
  through `heavy_favourites.season_scope`.
Fetching is polite (`PAUSE` between requests), cached for good in `data/raw/heavy_espn` and
resumable, with a time budget per run.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from soccer_stats import cups
from soccer_stats.cup_odds import HIST_COST
from soccer_stats.data import RAW_DIR, _fetch
from soccer_stats.lab import comebacks as cb
from soccer_stats.lab import heavy_favourites as hf

SUBDIR = "heavy_espn"  # under RAW_DIR; the Actions cache `heavy-espn-*` holds it
PAUSE = 0.5
RUN_SECONDS = 100 * 60
MIN_FAV = hf.MIN_FAV
# code: (name, ESPN slug, Odds API sport key to confirm with the free sports list)
CUPS = {
    "FAC": ("FA Cup", "eng.fa", "soccer_fa_cup"),
    "EFLC": ("League Cup", "eng.league_cup", "soccer_england_efl_cup"),
    "CDR": ("Copa del Rey", "esp.copa_del_rey", "soccer_spain_copa_del_rey"),
    "DFBP": ("DFB-Pokal", "ger.dfb_pokal", "soccer_germany_dfb_pokal"),
    "CI": ("Coppa Italia", "ita.coppa_italia", "soccer_italy_coppa_italia"),
    "CDF": ("Coupe de France", "fra.coupe_de_france", "soccer_france_coupe_de_france"),
    "UCL": ("Champions League", "uefa.champions", "soccer_uefa_champs_league"),
    "UEL": ("Europa League", "uefa.europa", "soccer_uefa_europa_league"),
    "UECL": ("Conference League", "uefa.europa.conf", "soccer_uefa_europa_conference_league"),
}
CUP_YEARS = (2021, 2022, 2023, 2024, 2025, 2026)  # July-June windows; 2026/27 to date
EXTRA = {
    "AUT": ("Austria", "aut.1", "soccer_austria_bundesliga"),
    "SWZ": ("Switzerland", "sui.1", "soccer_switzerland_superleague"),
    "DNK": ("Denmark", "den.1", "soccer_denmark_superliga"),
    "NOR": ("Norway", "nor.1", "soccer_norway_eliteserien"),
    "SWE": ("Sweden", "swe.1", "soccer_sweden_allsvenskan"),
    "POL": ("Poland", "pol.1", "soccer_poland_ekstraklasa"),
    "ROU": ("Romania", "rou.1", "soccer_romania_liga_1"),
    "FIN": ("Finland", "fin.1", "soccer_finland_veikkausliiga"),
    "IRL": ("Ireland", "irl.1", "soccer_league_of_ireland"),
    "RUS": ("Russia", "rus.1", "soccer_russia_premier_league"),
    "JPN": ("Japan", "jpn.1", "soccer_japan_j_league"),
    "CHN": ("China", "chn.1", "soccer_china_superleague"),
    "USA": ("USA", "usa.1", "soccer_usa_mls"),
    "MEX": ("Mexico", "mex.1", "soccer_mexico_ligamx"),
    "BRA": ("Brazil", "bra.1", "soccer_brazil_campeonato"),
    "ARG": ("Argentina", "arg.1", "soccer_argentina_primera_division"),
}
AMERICAS = ("USA", "MEX", "BRA", "ARG")  # evening kickoffs fall on the next UTC day
EXTRA_FROM = pd.Timestamp("2022-07-01")  # the in-play archive is priced from 2022/23
EARLIER = {"SP1": (2020, 2021), "I1": (2021,), "D2": (2021,), "SP2": (2021,)}


class Paced:
    """requests.get with a pause before each call and a time budget for the run."""

    def __init__(self, seconds: float, pause: float = PAUSE, get: Callable = requests.get):
        self.end, self.pause, self._get = time.monotonic() + seconds, pause, get
        self.calls = 0

    @property
    def out_of_time(self) -> bool:
        return time.monotonic() > self.end

    def __call__(self, url, params=None, timeout=None):
        if self.out_of_time:
            raise requests.ConnectionError("run budget spent")
        time.sleep(self.pause)
        self.calls += 1
        return self._get(url, params=params, timeout=timeout)


# ---------- ESPN parsing ----------


def american(x) -> float | None:
    """An American moneyline (number or "+150"/"-200" text) as decimal odds."""
    if x is None:
        return None
    try:
        v = float(str(x).replace("+", "").strip())
    except ValueError:
        return 2.0 if str(x).strip().upper() in ("EVEN", "EV") else None
    if v == 0:
        return None
    return 1 + v / 100 if v > 0 else 1 + 100 / -v


def _line(odds: dict, side: str):
    """One side's pre-match moneyline in either of ESPN's shapes."""
    team = odds.get(f"{side}TeamOdds") if side != "draw" else odds.get("drawOdds")
    if isinstance(team, dict) and team.get("moneyLine") is not None:
        return team["moneyLine"]
    ml = odds.get("moneyline")
    if isinstance(ml, dict):
        part = ml.get(side) or {}
        for when in ("open", "close"):  # pre-match: the opening line first
            v = (part.get(when) or {}).get("odds") if isinstance(part, dict) else None
            if v is not None:
                return v
    return None


def espn_prices(comp: dict) -> dict | None:
    """ESPN's pre-match 1X2 as decimal odds {home, draw, away}, when its scoreboard keeps one."""
    for o in comp.get("odds") or []:
        if not isinstance(o, dict):
            continue
        p = {s: american(_line(o, s)) for s in ("home", "away", "draw")}
        if all(v and v > 1 for v in p.values()):
            return p
    return None


def half_time(comp: dict, h: dict, a: dict) -> list[int] | None:
    """Half-time score from the first linescore, else from goals before minute 46."""
    vals = []
    for c in (h, a):
        ls = c.get("linescores")
        v = ls[0].get("value") if isinstance(ls, list) and ls and isinstance(ls[0], dict) else None
        vals.append(cups._goals(v))
    if None not in vals:
        return vals
    ids = (str((h.get("team") or {}).get("id", "")), str((a.get("team") or {}).get("id", "")))
    if not all(ids):
        return None
    ht = [0, 0]
    for d in comp.get("details") or []:
        if not isinstance(d, dict) or not d.get("scoringPlay") or d.get("shootout"):
            continue
        m = cups._minute((d.get("clock") or {}).get("displayValue"))
        if m is None:
            return None
        if m <= 45:
            tid = str((d.get("team") or {}).get("id", ""))
            if tid in ids:
                ht[ids.index(tid)] += 1
    return ht


def events(body: dict | None) -> list[dict]:
    """Completed events with kickoff, names, 90-minute score, half-time score and ESPN's
    pre-match prices (None when its scoreboard has none)."""
    parsed = {e["id"]: e for e in cups.parse_events(body)}
    out = []
    for e in (body or {}).get("events") or []:
        if not isinstance(e, dict) or str(e.get("id")) not in parsed:
            continue
        p = parsed[str(e["id"])]
        if not p["completed"] or p["score90"] is None:
            continue
        comp = (e.get("competitions") or [{}])[0] or {}
        sides = {c.get("homeAway"): c for c in comp.get("competitors") or [] if isinstance(c, dict)}
        out.append(
            {
                "id": p["id"],
                "kickoff": p["kickoff"],
                "home": p["home"],
                "away": p["away"],
                "score90": p["score90"],
                "ht": half_time(comp, sides.get("home") or {}, sides.get("away") or {}),
                "prices": espn_prices(comp),
            }
        )
    return out


def favourite(prices: dict | None) -> tuple[str, float] | None:
    if not prices:
        return None
    f = cb._fair(np.array([prices["home"], prices["draw"], prices["away"]], float))
    return ("home", float(f[0])) if f[0] >= f[2] else ("away", float(f[2]))


def trails(ht: list[int] | None, side: str) -> bool:
    if not ht:
        return False
    return (ht[0] < ht[1]) if side == "home" else (ht[1] < ht[0])


def busiest(kickoffs) -> str | None:
    """The kickoff most matches share (ties: the earliest), for an archive probe."""
    ks = pd.Series([k for k in kickoffs if pd.notna(k)])
    if ks.empty:
        return None
    counts = ks.value_counts()
    return f"{counts[counts == counts.max()].index.min():%Y-%m-%dT%H:%MZ}"


# ---------- cups ----------


def window(year: int, today: pd.Timestamp) -> tuple[pd.Timestamp, pd.Timestamp]:
    start = pd.Timestamp(f"{year}-07-01", tz="UTC")
    end = min(pd.Timestamp(f"{year + 1}-07-01", tz="UTC"), today.normalize())
    return start, end


def cup_season(code: str, year: int, raw_dir: Path, get: Callable, stats: dict,
               today: pd.Timestamp) -> dict:  # fmt: skip
    name, slug, sport = CUPS[code]
    start, end = window(year, today)
    if end <= start:
        return {"cup": code, "season": hf.season_name(year), "error": "not started"}
    cal = cups.season_calendar(slug, start, end, Path(raw_dir) / SUBDIR, get, stats=stats,
                               today=today)  # fmt: skip
    evs = []
    for path in sorted((Path(raw_dir) / SUBDIR / cups.CACHE / "espn").glob(f"{slug}_*.json")):
        if path.name.endswith(".recent.json"):
            continue
        day = path.stem.rsplit("_", 1)[-1]
        if start.strftime("%Y%m%d") <= day < end.strftime("%Y%m%d"):
            evs += events(json.loads(path.read_text()))
    evs = list({e["id"]: e for e in evs}.values())
    priced = [(e, favourite(e["prices"])) for e in evs if e["prices"]]
    heavy = [(e, f) for e, f in priced if f[1] >= MIN_FAV]
    trail = [e for e, f in heavy if trails(e["ht"], f[0])]
    unpriced = [e for e in evs if not e["prices"]]
    decided = [e for e in unpriced if e["ht"] and e["ht"][0] != e["ht"][1]]
    slots = {e["kickoff"] for e in trail}
    pre_slots = {e["kickoff"] for e in decided}
    return {
        "cup": code,
        "name": name,
        "season": hf.season_name(year),
        "sport": sport,
        "matches": len(evs),
        "no_half_time": sum(e["ht"] is None for e in evs),
        "with_espn_odds": len(priced),
        "heavy": len(heavy),
        "trailing": len(trail),
        "slots": len(slots),
        "credits": len(slots) * HIST_COST,
        "unpriced_matches": len(unpriced),
        "unpriced_not_level_at_ht": len(decided),
        "pre_match_slots": len(pre_slots),
        "pre_match_credits": len(pre_slots) * HIST_COST,
        "requests": cal.get("requests"),
        "probe_kickoff": busiest(e["kickoff"] for e in evs),
        "trailing_list": [
            f"{e['kickoff']:%Y-%m-%d} {e['home']} {e['ht'][0]}-{e['ht'][1]} {e['away']}"
            for e in trail
        ],
    }


# ---------- football-data's extra leagues ----------


def read_extra(code: str, raw_dir: Path) -> pd.DataFrame:
    """One extra league's played matches from mid-2022 with closing prices."""
    path = _fetch(cups.FD_EXTRA_URL.format(code=code), Path(raw_dir) / f"{code}.csv", 12,
                  stale_ok=True)  # fmt: skip
    raw = pd.read_csv(path, encoding="latin-1", on_bad_lines="skip")
    raw = raw.dropna(subset=["Home", "Away", "HG", "AG"]).reset_index(drop=True)
    df = pd.DataFrame(
        {
            "season": raw["Season"].astype(str),
            "date": pd.to_datetime(raw["Date"], dayfirst=True, format="mixed", errors="coerce"),
            "home": raw["Home"],
            "away": raw["Away"],
            "fthg": pd.to_numeric(raw["HG"], errors="coerce"),
            "ftag": pd.to_numeric(raw["AG"], errors="coerce"),
        }
    )
    for col, src in (("ph", "PSCH"), ("pd", "PSCD"), ("pa", "PSCA"), ("ah", "AvgCH"),
                     ("ad", "AvgCD"), ("aa", "AvgCA")):  # fmt: skip
        df[col] = cb._num(raw, src)
    return df[df["date"] >= EXTRA_FROM].reset_index(drop=True)


def _tokens(name: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", cups.norm(str(name))) if len(t) > 2}


def same_club(a: str, b: str) -> bool:
    ta, tb = _tokens(a), _tokens(b)
    return bool(ta & tb) or cups.norm(str(a)) == cups.norm(str(b))


def join_extra(fd: pd.DataFrame, evs: list[dict]) -> tuple[pd.DataFrame, dict]:
    """Attach ESPN's half-time score and kickoff to football-data's matches: date within a
    day, the 90-minute score equal, and both club names sharing a word."""
    by_day: dict = {}
    for e in evs:
        by_day.setdefault(e["kickoff"].date(), []).append(e)
    ht, ko, counts = [], [], {"matches": len(fd), "joined": 0, "no_event": 0}
    for r in fd.itertuples():
        hit = None
        if pd.notna(r.date):
            for d in (r.date.date(), (r.date + pd.Timedelta(days=1)).date(),
                      (r.date - pd.Timedelta(days=1)).date()):  # fmt: skip
                for e in by_day.get(d, []):
                    if e["score90"] == [r.fthg, r.ftag] and same_club(r.home, e["home"]) and \
                            same_club(r.away, e["away"]):  # fmt: skip
                        hit = e
                        break
                if hit:
                    break
        ht.append(hit["ht"] if hit else None)
        ko.append(hit["kickoff"] if hit else pd.NaT)
        counts["joined" if hit else "no_event"] += 1
    out = fd.copy()
    out["ht"], out["kickoff"] = ht, ko
    return out, counts


def extra_days(fd: pd.DataFrame, code: str) -> list[str]:
    days = set()
    for d in fd["date"].dropna():
        days.add(f"{d:%Y%m%d}")
        if code in AMERICAS:
            days.add(f"{d + pd.Timedelta(days=1):%Y%m%d}")
    return sorted(days)


def extra_league(code: str, raw_dir: Path, get: Callable, stats: dict,
                 today: pd.Timestamp) -> list[dict]:  # fmt: skip
    country, slug, sport = EXTRA[code]
    fd = read_extra(code, raw_dir)
    evs = []
    for day in extra_days(fd, code):
        if stats.get("out_of_time"):
            break
        try:
            body = cups.scoreboard_day(slug, day, Path(raw_dir) / SUBDIR, get, stats, today)
        except requests.ConnectionError:
            stats["out_of_time"] = True
            break
        evs += events(body)
    joined, counts = join_extra(fd, list({e["id"]: e for e in evs}.values()))
    fav = hf.fair_chances(joined)
    rows = []
    for season, g in fav.groupby("season"):
        heavy = g[g["p_fav"] >= MIN_FAV]
        trail = heavy[[trails(h, "home" if fh else "away") for h, fh in
                       zip(heavy["ht"], heavy["fav_home"], strict=True)]]  # fmt: skip
        timed = trail[trail["kickoff"].notna()]
        slots = int(timed["kickoff"].nunique())
        rows.append(
            {
                "league": code,
                "country": country,
                "season": season,
                "sport": sport,
                "matches": int(len(g)),
                "joined": int(g["ht"].notna().sum()),
                "heavy": int(len(heavy)),
                "heavy_with_ht": int(heavy["ht"].notna().sum()),
                "trailing": int(len(trail)),
                "slots": slots,
                "credits": slots * HIST_COST,
                "probe_kickoff": busiest(g["kickoff"]),
            }
        )
    for r in rows:
        r["join_counts"] = counts
    return rows


# ---------- the run ----------


def earlier() -> list[dict]:
    rows = []
    for lg, years in EARLIER.items():
        for y in years:
            try:
                r = hf.season_scope(lg, y)
            except Exception as exc:  # noqa: BLE001 - a missing file is reported, not fatal
                r = {"league": lg, "season": hf.season_name(y), "error": str(exc)[:120]}
            r.pop("snapshot_times", None)
            rows.append(r)
    return rows


def run(raw_dir: Path = RAW_DIR, seconds: float = RUN_SECONDS, get: Callable = requests.get,
        today: pd.Timestamp | None = None, cups_only: bool = False) -> dict:  # fmt: skip
    today = today or pd.Timestamp.now(tz="UTC")
    paced = Paced(seconds, get=get)
    stats: dict = {}
    out: dict = {"cups": [], "extra": [], "earlier": earlier()}
    for code in CUPS:
        for y in CUP_YEARS:
            if paced.out_of_time:
                out["cups"].append({"cup": code, "season": hf.season_name(y), "error": "time"})
                continue
            try:
                out["cups"].append(cup_season(code, y, raw_dir, paced, stats, today))
            except Exception as exc:  # noqa: BLE001 - one cup failing is reported, not fatal
                out["cups"].append({"cup": code, "season": hf.season_name(y),
                                    "error": str(exc)[:120]})  # fmt: skip
    if not cups_only:
        for code in EXTRA:
            try:
                out["extra"] += extra_league(code, raw_dir, paced, stats, today)
            except Exception as exc:  # noqa: BLE001
                out["extra"].append({"league": code, "error": str(exc)[:120]})
    out["requests"], out["failures"] = paced.calls, stats.get("failures", 0)
    out["out_of_time"] = paced.out_of_time
    return out


def totals(res: dict) -> dict:
    def s(rows, k):
        return sum(r.get(k, 0) for r in rows if "error" not in r)

    c, x, e = res["cups"], res["extra"], res["earlier"]
    return {
        "cups": {k: s(c, k) for k in ("matches", "with_espn_odds", "heavy", "trailing", "slots",
                                      "credits", "pre_match_slots", "pre_match_credits")},
        "extra": {k: s(x, k) for k in ("matches", "joined", "heavy", "trailing", "slots",
                                       "credits")},
        "earlier": {k: s(e, k) for k in ("heavy", "trailing", "slots", "credits")},
    }  # fmt: skip


def report(res: dict, tot: dict) -> str:
    lines = [f"== More data for the 75%+ idea: {res['requests']} ESPN requests, "
             f"{res['failures']} failed, out of time: {res['out_of_time']} =="]  # fmt: skip
    for r in res["cups"]:
        if "error" in r:
            lines.append(f"  cup {r['cup']} {r['season']}: {r['error']}")
            continue
        lines.append(
            f"  cup {r['cup']:5s} {r['season']}: {r['matches']} matches ({r['no_half_time']} "
            f"no HT), ESPN odds {r['with_espn_odds']}, heavy {r['heavy']}, trailing "
            f"{r['trailing']}, slots {r['slots']} ({r['credits']} cr); unpriced "
            f"{r['unpriced_matches']}, not level at HT {r['unpriced_not_level_at_ht']} in "
            f"{r['pre_match_slots']} slots ({r['pre_match_credits']} cr pre-match)"
        )
    for r in res["extra"]:
        if "error" in r:
            lines.append(f"  extra {r['league']}: {r['error']}")
            continue
        lines.append(
            f"  extra {r['league']:4s} {r['season']}: {r['matches']} matches, ESPN HT "
            f"{r['joined']}, heavy {r['heavy']} ({r['heavy_with_ht']} with HT), trailing "
            f"{r['trailing']}, slots {r['slots']} ({r['credits']} cr)"
        )
    for r in res["earlier"]:
        if "error" in r:
            lines.append(f"  earlier {r['league']} {r['season']}: {r['error']}")
            continue
        lines.append(f"  earlier {r['league']} {r['season']}: heavy {r['heavy']}, trailing "
                     f"{r['trailing']}, slots {r['slots']} ({r['credits']} cr)")  # fmt: skip
    lines.append("Totals: " + json.dumps(tot))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--minutes", type=float, default=RUN_SECONDS / 60, help="run time budget")
    p.add_argument("--json", default="")
    args = p.parse_args(argv)
    res = run(seconds=args.minutes * 60)
    tot = totals(res)
    print(report(res, tot))
    print("HEAVY_MORE_JSON " + json.dumps({**res, "totals": tot}, default=str))
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps({**res, "totals": tot}, indent=1, default=str))


if __name__ == "__main__":
    main()
