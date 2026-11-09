import subprocess
from pathlib import Path
import pytest
from tests.conftest import find_blender


@pytest.mark.skipif(find_blender() is None, reason="Blender 3.6 unavailable")
@pytest.mark.parametrize("script", ["phase5_regressions.py", "phase5_fix1.py"])
def test_phase5_blender_regressions(script):
    out = subprocess.run([str(find_blender()), "--background", "--factory-startup",
                          "--python", str(Path(__file__).with_name(script))],
                         capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stdout[-5000:] + out.stderr[-5000:]
