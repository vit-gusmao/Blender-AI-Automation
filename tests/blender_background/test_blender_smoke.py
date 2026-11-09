r"""Wrapper pytest: roda o smoke test dentro do Blender 3.6 real.

Usa BLENDER_EXE (padrão: C:\Blender\blender 3.6\blender.exe). Pula
limpamente quando o Blender não é encontrado.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.conftest import find_blender

SMOKE = Path(__file__).parent / "blender_smoke.py"

pytestmark = pytest.mark.skipif(
    find_blender() is None,
    reason="Blender 3.6 não encontrado (defina BLENDER_EXE)",
)


def test_blender_36_background_smoke(tmp_path):
    blender = find_blender()
    report_path = tmp_path / "smoke_report.json"
    state_root = tmp_path / "state"
    cmd = [
        str(blender), "--background", "--factory-startup",
        "--python", str(SMOKE), "--",
        f"--report={report_path}",
        f"--state-root={state_root}",
    ]
    print("executando:", " ".join(cmd))
    proc = subprocess.run(  # noqa: S603 — blender local do usuário
        cmd, capture_output=True, text=True, timeout=600, cwd=str(tmp_path))
    print(proc.stdout[-4000:])
    if proc.stderr:
        print("STDERR:", proc.stderr[-2000:])

    assert report_path.exists(), (
        f"relatório não gerado (exit={proc.returncode}); "
        f"stdout tail acima")
    report = json.loads(report_path.read_text("utf-8"))
    assert report["blender"] == "3.6.0", report["blender"]
    assert report["python"].startswith("3.10.")
    assert report["background"] is True
    assert Path(report["state_root"]).resolve() == state_root.resolve()
    jobs_db = Path(report["jobs_db"])
    assert jobs_db == state_root / "storage" / "jobs.db"
    assert jobs_db.is_file() and jobs_db.stat().st_size > 0
    assert Path(report["artifacts_root"]) == state_root / "artifacts"
    expected_checks = [
        "addon registration via /register",
        "blender_ping",
        "blender_get_version",
        "scene_get_info read-only",
        "scene_list_objects read-only",
        "object_create cube exact",
        "object_create idempotent no scene mutation",
        "object_transform exact",
        "material_create exact",
        "material_assign exact",
        "mesh_validate exact",
        "mesh_edit SUBDIVIDE_EDGES exact",
        "object_get_info exact",
        "python_execute_blender (permitido)",
        "python_execute_blender (padrão bloqueado)",
        "scene_validate read-only exact",
        "mesh_boolean DIFFERENCE enum exact",
        "render_preview (Cycles 120x90)",
        "asset_export OBJ",
        "project_save",
        "project_checkpoint exact",
        "jobs persisted in isolated SQLite",
    ]
    assert [check["name"] for check in report["checks"]] == expected_checks
    failed = [c for c in report["checks"] if not c["ok"]]
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]
    assert failed == [], json.dumps(failed, indent=1)
    assert report["passed"] == len(expected_checks)
    assert report["failed"] == 0
    assert all(check["detail"] is not False for check in report["checks"])
    print(f"smoke OK: {report['passed']} verificações dentro do Blender "
          f"{report['blender']}")


def test_blender_reports_python_310():
    blender = find_blender()
    out = subprocess.run(  # noqa: S603
        [str(blender), "--background", "--factory-startup",
         "--python-expr",
         "import sys,bpy;print('VER', bpy.app.version_string, "
         "sys.version.split()[0])"],
        capture_output=True, text=True, timeout=120)
    combined = out.stdout + out.stderr
    assert out.returncode == 0, combined[-500:]
    assert "VER 3.6.0 3.10." in combined, combined[-500:]
