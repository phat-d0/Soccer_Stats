"""The paid-odds archive's manifest and checksums, and the cache keep-alive's coverage."""

import fnmatch
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from soccer_stats import cache_archive as ca

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
FALLBACK = "./.github/actions/paid-archive-restore"


def tree(root: Path, files: dict[str, bytes]) -> None:
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


FILES = {
    "data/raw/cup_odds/a.json": b'{"a": 1}',
    "data/raw/cup_odds/sub/b.json": b'{"b": 2}',
    "data/raw/cups/espn/day.json": b"[]",
}
PATHS = ["data/raw/cup_odds", "data/raw/cups/espn"]


def pack(root: Path, out: Path, n_parts: int = 1) -> list[Path]:
    """Stand-in parts (the workflow packs tar + zstd): the files' bytes, split."""
    blob = b"".join((root / k).read_bytes() for k in sorted(FILES))
    size = -(-len(blob) // n_parts)
    parts = []
    for i in range(n_parts):
        p = out / f"cup-odds-history.tar.zst.part{i:02d}"
        p.write_bytes(blob[i * size : (i + 1) * size])
        parts.append(p)
    return parts


def test_content_digest_covers_names_sizes_and_bytes_not_order(tmp_path):
    tree(tmp_path, FILES)
    files = ca.list_files(PATHS, tmp_path)
    assert [f["path"] for f in files] == sorted(FILES)
    assert files[0]["sha256"] == hashlib.sha256(FILES[files[0]["path"]]).hexdigest()
    d = ca.content_sha256(files)
    assert ca.content_sha256(list(reversed(files))) == d
    (tmp_path / "data/raw/cup_odds/a.json").write_bytes(b'{"a": 9}')
    assert ca.content_sha256(ca.list_files(PATHS, tmp_path)) != d


def test_entry_records_files_parts_and_whole_archive(tmp_path):
    tree(tmp_path, FILES)
    parts = pack(tmp_path, tmp_path, n_parts=2)
    e = ca.entry("cup-odds-history", "cup-odds-history-123", PATHS, parts, tmp_path, "2026-10-28")
    assert e["files"] == 3 and e["bytes"] == sum(len(v) for v in FILES.values())
    assert e["by_path"] == {"data/raw/cup_odds": 2, "data/raw/cups/espn": 1}
    assert [p["asset"] for p in e["archive"]["parts"]] == [p.name for p in parts]
    whole = hashlib.sha256(b"".join(p.read_bytes() for p in parts)).hexdigest()
    assert e["archive"]["sha256"] == whole and e["key"] == "cup-odds-history-123"
    assert ca.verify(e, tmp_path) == []


def test_verify_catches_a_bad_or_missing_part(tmp_path):
    tree(tmp_path, FILES)
    parts = pack(tmp_path, tmp_path, n_parts=2)
    e = ca.entry("cup-odds-history", "k", PATHS, parts, tmp_path)
    parts[1].write_bytes(parts[1].read_bytes()[:-1] + b"X")
    assert any("sha256 mismatch" in p for p in ca.verify(e, tmp_path))
    parts[0].unlink()
    assert any("missing" in p for p in ca.verify(e, tmp_path))


def test_an_asset_is_replaced_only_when_the_contents_changed(tmp_path):
    tree(tmp_path, FILES)
    e1 = ca.entry("cup-odds-history", "k1", PATHS, pack(tmp_path, tmp_path), tmp_path, "d1")
    assert ca.changed(None, e1)
    m1, up = ca.merge(None, [e1], "t1")
    assert up == ["cup-odds-history"] and m1["caches"]["cup-odds-history"]["key"] == "k1"
    # The same files under a newer key: unchanged, the old entry (and asset) stay.
    e2 = ca.entry("cup-odds-history", "k2", PATHS, pack(tmp_path, tmp_path), tmp_path, "d2")
    assert not ca.changed(m1["caches"]["cup-odds-history"], e2)
    m2, up = ca.merge(m1, [e2], "t2")
    assert up == [] and m2["caches"]["cup-odds-history"]["key"] == "k1"
    assert m2["caches"]["cup-odds-history"]["checked"] == "t2" and m2["updated"] == "t1"
    # A new file: changed.
    tree(tmp_path, {"data/raw/cup_odds/c.json": b"{}"})
    e3 = ca.entry("cup-odds-history", "k3", PATHS, pack(tmp_path, tmp_path), tmp_path, "d3")
    m3, up = ca.merge(m2, [e3], "t3")
    assert up == ["cup-odds-history"] and m3["caches"]["cup-odds-history"]["files"] == 4


def test_a_missed_or_empty_cache_never_replaces_its_archive(tmp_path):
    tree(tmp_path, FILES)
    e = ca.entry("cup-odds-history", "k", PATHS, pack(tmp_path, tmp_path), tmp_path)
    m, _ = ca.merge(None, [e], "t1")
    m2, up = ca.merge(m, [], "t2")  # the cache missed: no entry this run
    assert up == [] and m2["caches"]["cup-odds-history"] == m["caches"]["cup-odds-history"]
    empty = {**e, "files": 0, "content_sha256": "x"}
    assert not ca.changed(m["caches"]["cup-odds-history"], empty)


def test_missing_reports_paths_with_fewer_files_than_the_archive(tmp_path):
    tree(tmp_path, FILES)
    e = ca.entry("cup-odds-history", "k", PATHS, pack(tmp_path, tmp_path), tmp_path)
    assert ca.missing(e, tmp_path) == {}
    shutil.rmtree(tmp_path / "data/raw/cup_odds")
    assert ca.missing(e, tmp_path) == {"data/raw/cup_odds": (0, 2)}
    assert ca.missing(None, tmp_path) == {}


def test_command_line_entry_merge_verify_missing(tmp_path, monkeypatch, capsys):
    tree(tmp_path, FILES)
    monkeypatch.chdir(tmp_path)
    parts = pack(tmp_path, tmp_path)
    args = ["entry", "--name", "cup-odds-history", "--key", "k", "--paths", *PATHS]
    args += ["--parts", *map(str, parts), "--now", "t", "--out", "entries/e.json"]
    (tmp_path / "entries").mkdir()
    assert ca.main(args) == 0
    assert json.loads((tmp_path / "entries/e.json").read_text())["changed"] is True
    assert ca.main(["merge", "--entries", "entries", "--now", "t", "--out", "m.json"]) == 0
    m = json.loads((tmp_path / "m.json").read_text())
    assert "changed" not in m["caches"]["cup-odds-history"] and m["tag"] == ca.TAG
    assert ca.main(["verify", "--manifest", "m.json", "--dir", "."]) == 0
    assert (
        ca.main(
            [
                "missing",
                "--manifest",
                "m.json",
                "--name",
                "cup-odds-history",
                "--parts-out",
                "p.txt",
            ]
        )
        == 0
    )
    assert (tmp_path / "p.txt").read_text() == ""
    shutil.rmtree(tmp_path / "data/raw/cups/espn")
    ca.main(
        ["missing", "--manifest", "m.json", "--name", "cup-odds-history", "--parts-out", "p.txt"]
    )
    assert (tmp_path / "p.txt").read_text() == parts[0].name + "\n"
    parts[0].write_bytes(b"corrupt")
    assert ca.main(["verify", "--manifest", "m.json", "--dir", "."]) == 1
    assert "sha256 mismatch" in capsys.readouterr().out


@pytest.mark.skipif(not shutil.which("zstd"), reason="zstd not installed")
def test_a_real_pack_restores_only_the_missing_files(tmp_path, monkeypatch):
    src = tmp_path / "src"
    tree(src, FILES)
    tar = [
        "tar",
        "--sort=name",
        "--mtime=2026-01-01 00:00:00Z",
        "--owner=0",
        "--group=0",
        "--numeric-owner",
    ]
    blob = subprocess.run(
        [*tar, "-cf", "-", *PATHS], cwd=src, check=True, capture_output=True
    ).stdout
    z = subprocess.run(["zstd", "-q", "-c"], input=blob, check=True, capture_output=True).stdout
    again = subprocess.run(
        [*tar, "-cf", "-", *PATHS], cwd=src, check=True, capture_output=True
    ).stdout
    assert again == blob  # deterministic: the same files pack to the same bytes
    dst = tmp_path / "dst"
    tree(dst, {"data/raw/cups/espn/day.json": b"[1]"})  # newer than the archive: kept
    subprocess.run(
        ["tar", "--zstd", "-x", "--skip-old-files", "-f", "-"], cwd=dst, input=z, check=True
    )
    assert (dst / "data/raw/cup_odds/sub/b.json").read_bytes() == FILES[
        "data/raw/cup_odds/sub/b.json"
    ]
    assert (dst / "data/raw/cups/espn/day.json").read_bytes() == b"[1]"


# ---------- the workflows ----------


def _steps():
    """(workflow, job, index, step) for every step outside caches.yml."""
    for f in sorted(WORKFLOWS.glob("*.yml")):
        if f.name == "caches.yml":
            continue
        for jn, job in (yaml.safe_load(f.read_text())["jobs"] or {}).items():
            for i, s in enumerate(job.get("steps") or []):
                yield f.name, jn, i, s, job["steps"]


def _paths(v) -> list[str]:
    return [p.strip() for p in str(v).split("\n") if p.strip()]


def _prefix(w: dict) -> str:
    """The key prefix a cache step restores or saves under ({league} for a dynamic one)."""
    key = w.get("restore-keys") or re.sub(r"(\$\{\{ github\.run_id \}\}|none)$", "", w["key"])
    return key.replace("${{ inputs.league }}", "*")


def _keepalive() -> list[dict]:
    wf = yaml.safe_load((WORKFLOWS / "caches.yml").read_text())
    return wf["jobs"]["keepalive"]["strategy"]["matrix"]["include"]


def test_every_cache_the_workflows_use_is_kept_alive_with_its_exact_paths():
    kept = _keepalive()
    seen = 0
    for wf, job, _i, s, _all in _steps():
        if "actions/cache" not in s.get("uses", ""):
            continue
        w = s["with"]
        if w["key"].startswith("playwright-"):
            continue  # CI's browser cache: every CI run uses it
        prefix = _prefix(w)
        matches = [e for e in kept if fnmatch.fnmatchcase(e["prefix"], prefix)]
        assert matches, f"{wf} {job}: cache {prefix} isn't in caches.yml's keep-alive"
        for e in matches:
            assert _paths(e["path"]) == _paths(w["path"]), f"{prefix}: paths differ"
        seen += 1
    assert seen > 20


def test_the_paid_caches_match_the_archive_registry():
    kept = {e["name"]: e for e in _keepalive()}
    for name, spec in ca.PAID.items():
        e = kept[name]
        assert e["paid"] is True and e["prefix"] == spec["prefix"]
        assert _paths(e["path"]) == spec["paths"]
    assert {n for n, e in kept.items() if e["paid"]} == set(ca.PAID)
    wf = yaml.safe_load((WORKFLOWS / "caches.yml").read_text())
    test = {
        e["name"]: _paths(e["path"])
        for e in wf["jobs"]["fallback-test"]["strategy"]["matrix"]["include"]
    }
    assert test == {n: s["paths"] for n, s in ca.PAID.items()}
    assert not any("ODDS_API_KEY" in json.dumps(j) for j in wf["jobs"].values())


def test_every_paid_restore_is_followed_by_the_archive_fallback():
    paid = {s["prefix"]: n for n, s in ca.PAID.items()}
    found = 0
    for wf, job, i, s, steps in _steps():
        if "actions/cache" not in s.get("uses", "") or "/save@" in s["uses"]:
            continue
        prefix = _prefix(s["with"])
        if prefix not in paid:
            continue
        nxt = steps[i + 1]
        assert nxt.get("uses") == FALLBACK, f"{wf} {job}: no fallback after the {prefix} restore"
        assert nxt["with"]["name"] == paid[prefix]
        assert nxt.get("if") == s.get("if"), f"{wf} {job}: the fallback's condition differs"
        found += 1
    assert found == 9
