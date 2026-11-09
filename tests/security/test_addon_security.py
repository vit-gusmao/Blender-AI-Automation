"""Segurança: scan de Python do addon (módulo puro, sem bpy) + níveis."""
from __future__ import annotations

from universal_blender_mcp.authorization import AuthLevel

# universal_mcp.security é stdlib-only — importável fora do Blender
from universal_mcp.security import (AddonSecurity,
                                    DESTRUCTIVE_OPERATIONS,
                                    MUTATING_OPERATIONS)


def _addon_paths():
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    if str(root / "blender_addon") not in sys.path:
        sys.path.insert(0, str(root / "blender_addon"))
    return root


def test_scan_blocks_dangerous_patterns():
    sec = AddonSecurity(strict_python=True)
    for code, expected in (
        ("import subprocess", "subprocess"),
        ("subprocess.run(['cmd'])", "subprocess"),
        ("os.system('del /f x')", "os.system"),
        ("data = __import__('os')", "__import__"),
        ("import socket\ns = socket.socket()", "socket.socket"),
        ("from shutil import rmtree", "shutil.rmtree"),
    ):
        findings = sec.scan_python_source(code)
        assert expected in findings, f"{code!r} não bloqueou {expected}"


def test_scan_allows_bpy_code():
    sec = AddonSecurity(strict_python=True)
    code = ("import bpy\n"
            "obj = bpy.data.objects['Cube']\n"
            "obj.location = (0, 0, 1)\n"
            "result = {'name': obj.name}\n")
    assert sec.scan_python_source(code) == []


def test_strict_mode_can_be_disabled_by_user():
    sec = AddonSecurity(strict_python=False)
    # strict desligado: padrões não são escaneados (política do usuário)
    assert AddonSecurity(strict_python=True).scan_python_source("import subprocess")
    # mas o objeto continua funcionando
    assert sec.check_operation("object_create") == (True, "")


def test_python_gate_respects_prefs():
    sec = AddonSecurity(allow_python=False)
    ok, reason = sec.check_operation("python_execute_blender")
    assert not ok and "Python" in reason
    assert sec.check_operation("scene_get_info")[0] is True


def test_path_restriction_mode(tmp_path):
    sec = AddonSecurity(restrict_paths=True,
                        allowed_paths=[str(tmp_path)])
    assert sec.path_allowed(str(tmp_path / "a.blend"))
    assert not sec.path_allowed(r"C:\Windows\x.blend")
    # sem restrição: tudo passa (allowlist do servidor ainda vale)
    assert AddonSecurity().path_allowed(r"C:\anywhere\x.blend")


def test_operation_risk_sets():
    assert "object_delete" in DESTRUCTIVE_OPERATIONS
    assert "python_execute_blender" in DESTRUCTIVE_OPERATIONS
    assert "object_create" in MUTATING_OPERATIONS
    assert "scene_get_info" not in MUTATING_OPERATIONS


def test_alias_scan_and_advanced_approval_gate():
    sec = AddonSecurity()
    assert sec.scan_python_source("import os as other\nother.system('x')")
    assert sec.scan_python_source("from os import system as call\ncall('x')")
    assert not sec.check_operation("python_execute_blender", {}, {})[0]
    assert sec.check_operation("python_execute_blender", {}, {
        "level": "advanced", "approved": True})[0]
    assert not sec.check_operation("scene_validate", {"fix": True}, {
        "level": "read_only"})[0]


def test_argument_aware_destructive_modes():
    from universal_mcp.security import is_destructive
    for op, args in (("mesh_apply_modifier", {"action": "APPLY"}),
                     ("bone_edit", {"delete": True}),
                     ("animation_action", {"delete": True}),
                     ("animation_fcurve", {"operation": "DELETE_KEYFRAME"}),
                     ("physics_configure", {"remove": True})):
        assert is_destructive(op, args)
    assert not is_destructive("addon_operator_invoke", {"mode": "LIST_ADDONS"})
