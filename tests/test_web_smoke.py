"""Browser smoke test of the web app (tests/web/smoke.mjs) on the committed fixture.

Skips when node, Playwright or Chromium aren't available. CI runs smoke.mjs directly in
its own "web" job (.github/workflows/ci.yml), where a skip is a failure.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "web"
CHROMIUM = os.environ.get("CHROMIUM", "/opt/pw-browsers/chromium")


def test_fixture_is_complete():
    data = json.loads((FIXTURE / "data.json").read_text())
    for key in ("fixtures", "ratings", "record", "portfolio", "params", "teams"):
        assert key in data
    for f in (data["players_stats"], data["players_backtest"]):
        assert (FIXTURE / f).exists()
    pf = data["portfolio"]
    assert "live" not in pf and "backtest" not in pf  # the app reads `portfolios` only
    assert pf["player_model"]["priced"]["strategies"]
    pfs = {p["id"]: p for p in pf["portfolios"]}
    assert pfs["moneyline"]["live"]["trades"] and pfs["moneyline"]["backtest"]["trades"]
    assert pfs["goalscorer"]["live"]["summary"]["trades"] == 0  # the empty state
    assert pfs["player_shots"]["status"] == "retired" and pfs["player_shots"]["backtest"]
    assert pfs["moneyline"]["backtest"]["edge_threshold"]["min_edge"] is not None
    assert pfs["player_shots"]["backtest"]["edge_threshold"]["min_edge"] is None
    size = sum(p.stat().st_size for p in FIXTURE.glob("*.json"))
    assert size < 1_000_000  # keep the committed fixture small


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.skipif(not Path(CHROMIUM).exists(), reason="Chromium not available")
def test_web_smoke():
    out = subprocess.run(
        ["node", str(ROOT / "tests" / "web" / "smoke.mjs")],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if out.stdout.startswith("SKIP"):
        pytest.skip(out.stdout.strip())
    assert out.returncode == 0, out.stdout + out.stderr
