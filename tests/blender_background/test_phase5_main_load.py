import os
from pathlib import Path
import subprocess

import pytest
from tests.conftest import find_blender

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(find_blender() is None, reason="Blender 3.6 unavailable")
@pytest.mark.parametrize("scenario", [
    "project_open", "asset_import", "operator_load", "python_load", "library_roundtrip",
    pytest.param("review_fixture", id="review_fixture_1"),
    pytest.param("review_fixture", id="review_fixture_2"),
    pytest.param("review_fixture", id="review_fixture_3"),
])
def test_main_replacement_does_not_restore_freed_selection(tmp_path, scenario):
    env = dict(os.environ)
    env["PYTHONPATH"] = "blender_addon;mcp_server/src"
    script = "phase5_fix1.py" if scenario == "review_fixture" else "phase5_fix2_load.py"
    out = subprocess.run([str(find_blender()), "--background", "--factory-startup",
                          "--python", str(Path(__file__).with_name(script)), "--", scenario],
                         env=env, cwd=str(ROOT), capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, f"Blender exit={out.returncode}\n{out.stdout[-6000:]}\n{out.stderr[-3000:]}"
    assert ("FIX1 SUMMARY {\"failed\": []}" if scenario == "review_fixture" else "FIX2 PASS " + scenario) in out.stdout
