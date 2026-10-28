"""The paid-odds archive and the cache keep-alive (lead brief, 28 Oct; 0 credits, no key).

GitHub evicts a cache unused for 7 days (and the least recently used ones past 10 GB), and
the odds bought with credits live only in caches. Two safeguards (.github/workflows/caches.yml):

* Keep-alive: twice a week each cache is restored with its exact key prefix and paths (a
  restore counts as a use), its hit or miss and size logged.
* Archive: each paid cache (PAID) packed as a deterministic tar + zstd into the GitHub
  Release `paid-odds-archive` (assets up to 2 GB, split into parts above PART_BYTES), with a
  manifest (files, bytes, sha256, the source cache key, date) on data-log
  `archive/manifest.json`. An asset is replaced only when the cache's contents changed
  (`content_sha256`, from every file's path, size and sha256, so a repacked tar of the same
  files changes nothing).

On a miss (fewer files on disk than the manifest lists for a path), the composite action
.github/actions/paid-archive-restore fills the paid cache back in from the archive before
any step that could buy runs; a failed checksum stops the job.

This module holds the manifest and checksum code; it needs only the standard library, so
the workflows run it as a script before any install: `python3 src/soccer_stats/
cache_archive.py entry|merge|verify|missing ...`. Packing, uploads and downloads are shell.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

TAG = "paid-odds-archive"
MANIFEST_PATH = "archive/manifest.json"  # on data-log
PART_BYTES = 1_900_000_000  # a release asset may be up to 2 GB
CHUNK = 1 << 20
LIMIT_BYTES = 10 * 1024**3  # GitHub's cache limit per repository

# Caches holding data bought with credits: name -> key prefix and the exact paths saved
# (the order matters: a restore with other paths misses).
PAID = {
    "odds-history": {
        "prefix": "odds-history-",
        "paths": ["data/raw/odds_history"],
        "what": "DraftKings historical h2h snapshots (backfill.yml)",
    },
    "player-odds-history": {
        "prefix": "player-odds-history-",
        "paths": ["data/raw/player_odds_history"],
        "what": "FanDuel historical player lines (players.yml)",
    },
    "cup-odds-history": {
        "prefix": "cup-odds-history-",
        "paths": ["data/raw/cup_odds", "data/raw/cups/espn"],
        "what": "Champions League closes, Pinnacle and DraftKings (backfill.yml cup_odds)",
    },
    "comeback-odds": {
        "prefix": "comeback-odds-",
        "paths": ["data/raw/comeback_odds"],
        "what": "comeback phase 2 in-play snapshots, designs B and C (odds-check.yml)",
    },
    "archive-probe": {
        "prefix": "archive-probe-",
        "paths": ["data/raw/archive_probe"],
        "what": "in-play archive probes, one snapshot per league-season (odds-check.yml)",
    },
    "inplay-probe": {
        "prefix": "inplay-probe-",
        "paths": ["data/raw/inplay_probe"],
        "what": "in-play probe answers, live and historical (odds-check.yml inplay-probe)",
    },
}

# Caches a job holding ODDS_API_KEY saves that hold no bought data: prefix (fnmatch, as
# the workflow's key with ${{ inputs.league }} as *) -> why it is free. A cache saved in a
# key-holding job must be in PAID or here (tests/test_cache_archive.py), so a new fetcher
# can't save bought bodies into a cache nobody archives without someone saying so here.
FREE = {
    "raw-data-": "football-data, Understat and FPL downloads, plus the live odds answers "
    "whose parsed rows publish.yml logs on data-log (odds_log/)",
    "cup-espn-": "ESPN's UEFA cup calendars and results (free)",
    "cup-corners-": "ESPN cup match summaries for the cup corner research (free)",
    "espn-timelines-": "ESPN goal timelines (free)",
    "understat-*-": "Understat downloads for one league (free)",
    "comebacks-*-": "Understat match files for the comeback research (free)",
    "heavy-espn-": "ESPN cup and extra-league days for the 75%+ counting (free)",
}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def list_files(paths: list[str], root: Path = Path(".")) -> list[dict]:
    """Every file under `paths` (relative to `root`), sorted: {path, bytes, sha256}."""
    out = []
    for p in paths:
        base = Path(root) / p
        found = [base] if base.is_file() else sorted(x for x in base.rglob("*") if x.is_file())
        for f in found:
            out.append(
                {
                    "path": f.relative_to(root).as_posix(),
                    "bytes": f.stat().st_size,
                    "sha256": sha256_file(f),
                }
            )
    return sorted(out, key=lambda r: r["path"])


def content_sha256(files: list[dict]) -> str:
    """One digest of a cache's contents: every file's path, size and sha256, sorted. Two
    packs of the same files share it whatever the tar's own bytes."""
    h = hashlib.sha256()
    for r in sorted(files, key=lambda r: r["path"]):
        h.update(f"{r['path']}\t{r['bytes']}\t{r['sha256']}\n".encode())
    return h.hexdigest()


def count_by_path(paths: list[str], root: Path = Path(".")) -> dict[str, int]:
    """Files under each path (0 when it doesn't exist)."""
    out = {}
    for p in paths:
        base = Path(root) / p
        if base.is_file():
            out[p] = 1
        elif base.exists():
            out[p] = sum(1 for x in base.rglob("*") if x.is_file())
        else:
            out[p] = 0
    return out


def missing(entry_: dict | None, root: Path = Path(".")) -> dict[str, tuple[int, int]]:
    """The paths of an archived cache with fewer files on disk than the archive holds:
    {path: (on disk, archived)}; {} when complete or nothing is archived."""
    if not entry_:
        return {}
    have = count_by_path(entry_["paths"], root)
    want = entry_.get("by_path") or {}
    return {p: (have.get(p, 0), n) for p, n in want.items() if have.get(p, 0) < n}


def entry(
    name: str,
    key: str | None,
    paths: list[str],
    parts: list[Path],
    root: Path = Path("."),
    created: str | None = None,
    files: list[dict] | None = None,
) -> dict:
    """One cache's manifest entry from its restored files and its packed parts (in order):
    {name, key, paths, files, bytes, by_path, content_sha256, archive: {bytes, sha256 (of
    the whole archive), parts: [{asset, bytes, sha256}]}, created}."""
    files = list_files(paths, root) if files is None else files
    whole = hashlib.sha256()
    part_rows = []
    for p in parts:
        part_rows.append(
            {"asset": Path(p).name, "bytes": Path(p).stat().st_size, "sha256": sha256_file(p)}
        )
        with open(p, "rb") as f:
            while chunk := f.read(CHUNK):
                whole.update(chunk)
    by_path = {
        p: sum(1 for r in files if r["path"] == p or r["path"].startswith(p + "/")) for p in paths
    }
    return {
        "name": name,
        "key": key,
        "paths": list(paths),
        "files": len(files),
        "bytes": sum(r["bytes"] for r in files),
        "by_path": by_path,
        "content_sha256": content_sha256(files),
        "archive": {
            "bytes": sum(r["bytes"] for r in part_rows),
            "sha256": whole.hexdigest(),
            "parts": part_rows,
        },
        "created": created,
    }


def changed(previous: dict | None, new: dict) -> bool:
    """Whether a cache's archive must be (re)uploaded: no earlier entry, or its contents
    differ (content_sha256). A cache with no files is never uploaded over an archive."""
    if not new.get("files"):
        return False
    return not previous or previous.get("content_sha256") != new.get("content_sha256")


def merge(manifest: dict | None, entries: list[dict], now: str) -> tuple[dict, list[str]]:
    """The manifest with `entries` folded in: an entry replaces its cache's only when
    changed(); an unchanged one keeps the earlier entry (its assets stay) with `checked`
    set to `now`; caches missing from `entries` (a cache miss) keep theirs. Returns the
    manifest and the names that changed."""
    old = dict((manifest or {}).get("caches") or {})
    caches = dict(old)
    updated = []
    for e in entries:
        prev = old.get(e["name"])
        if changed(prev, e):
            caches[e["name"]] = {**e, "checked": now}
            updated.append(e["name"])
        elif prev:
            caches[e["name"]] = {**prev, "checked": now}
    out = {
        "tag": TAG,
        "updated": now if updated else (manifest or {}).get("updated", now),
        "checked": now,
        "caches": dict(sorted(caches.items())),
        "note": "Paid odds caches packed as tar + zstd release assets on the "
        f"`{TAG}` tag; content_sha256 covers every file's path, size and sha256, "
        "archive.sha256 the whole archive, parts[].sha256 each asset.",
    }
    return out, updated


def verify(entry_: dict, folder: Path) -> list[str]:
    """Problems with the downloaded assets of one manifest entry in `folder` (missing part,
    wrong size or sha256, wrong whole-archive sha256); [] when all match."""
    problems = []
    whole = hashlib.sha256()
    for part in entry_["archive"]["parts"]:
        p = Path(folder) / part["asset"]
        if not p.exists():
            problems.append(f"{part['asset']}: missing")
            continue
        if p.stat().st_size != part["bytes"]:
            problems.append(f"{part['asset']}: {p.stat().st_size} bytes, expected {part['bytes']}")
        if sha256_file(p) != part["sha256"]:
            problems.append(f"{part['asset']}: sha256 mismatch")
        with open(p, "rb") as f:
            while chunk := f.read(CHUNK):
                whole.update(chunk)
    if not problems and whole.hexdigest() != entry_["archive"]["sha256"]:
        problems.append(f"{entry_['name']}: whole-archive sha256 mismatch")
    return problems


def short(n: float) -> str:
    """Bytes in words: 1.2 GB, 34.5 MB, 812 KB."""
    for unit, size in (("GB", 1024**3), ("MB", 1024**2), ("KB", 1024)):
        if n >= size:
            return f"{n / size:.1f} {unit}"
    return f"{int(n)} B"


def read_json(path) -> dict | None:
    p = Path(path) if path else None
    if not p or not p.exists() or not p.stat().st_size:
        return None
    try:
        return json.loads(p.read_text())
    except ValueError:
        return None


# ---------- command line (stdlib only: run as a script in the workflows) ----------


def _cmd_entry(a) -> int:
    prev = ((read_json(a.manifest) or {}).get("caches") or {}).get(a.name)
    e = entry(a.name, a.key or None, a.paths, [Path(p) for p in a.parts], created=a.now)
    e["changed"] = changed(prev, e)
    Path(a.out).write_text(json.dumps(e, indent=1))
    state = "changed: upload" if e["changed"] else "unchanged: keep the archived asset"
    print(
        f"{a.name}: {e['files']} files, {short(e['bytes'])} on disk, archive "
        f"{short(e['archive']['bytes'])} in {len(e['archive']['parts'])} part(s); {state}"
    )
    return 0


def _cmd_merge(a) -> int:
    entries = [read_json(p) for p in sorted(Path(a.entries).glob("*.json"))]
    entries = [{k: v for k, v in e.items() if k != "changed"} for e in entries if e]
    out, updated = merge(read_json(a.manifest), entries, a.now)
    Path(a.out).write_text(json.dumps(out, indent=1) + "\n")
    print(f"Manifest: {len(out['caches'])} caches; changed: {', '.join(updated) or 'none'}")
    for name, e in out["caches"].items():
        print(
            f"  {name}: {e['files']} files, {short(e['bytes'])}, archive "
            f"{short(e['archive']['bytes'])}, key {e.get('key')}, created {e.get('created')}"
        )
    return 0


def _cmd_verify(a) -> int:
    caches = (read_json(a.manifest) or {}).get("caches") or {}
    names = a.names or list(caches)
    bad = 0
    for n in names:
        if n not in caches:
            print(f"{n}: not in the manifest")
            bad += 1
            continue
        problems = verify(caches[n], Path(a.dir))
        print(f"{n}: " + ("OK, every sha256 matches" if not problems else "; ".join(problems)))
        bad += bool(problems)
    return 1 if bad else 0


def _cmd_missing(a) -> int:
    e = ((read_json(a.manifest) or {}).get("caches") or {}).get(a.name)
    if not e:
        print(f"{a.name}: nothing archived yet")
        return 0
    gaps = missing(e)
    for p, (have, want) in gaps.items():
        print(f"{a.name}: {p} has {have} of the archive's {want} files")
    if not gaps:
        print(f"{a.name}: complete ({e['files']} archived files all on disk)")
    if a.parts_out:
        rows = e["archive"]["parts"] if gaps else []
        Path(a.parts_out).write_text("".join(r["asset"] + "\n" for r in rows))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="cache_archive")
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("entry", help="manifest entry for one packed cache")
    e.add_argument("--name", required=True)
    e.add_argument("--key", default="")
    e.add_argument("--paths", nargs="+", required=True)
    e.add_argument("--parts", nargs="+", required=True)
    e.add_argument("--manifest", help="the current manifest (decides changed)")
    e.add_argument("--now", required=True)
    e.add_argument("--out", required=True)
    e.set_defaults(func=_cmd_entry)
    m = sub.add_parser("merge", help="fold entries into the manifest")
    m.add_argument("--manifest")
    m.add_argument("--entries", required=True)
    m.add_argument("--now", required=True)
    m.add_argument("--out", required=True)
    m.set_defaults(func=_cmd_merge)
    v = sub.add_parser("verify", help="check downloaded assets against the manifest")
    v.add_argument("--manifest", required=True)
    v.add_argument("--dir", required=True)
    v.add_argument("--names", nargs="*")
    v.set_defaults(func=_cmd_verify)
    g = sub.add_parser("missing", help="which archived files aren't on disk")
    g.add_argument("--manifest", required=True)
    g.add_argument("--name", required=True)
    g.add_argument("--parts-out", help="write the assets to download when anything is missing")
    g.set_defaults(func=_cmd_missing)
    a = ap.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
