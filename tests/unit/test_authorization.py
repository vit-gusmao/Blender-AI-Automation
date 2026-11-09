"""Unit: níveis de autorização, allowlist de caminhos e scan de Python (§14)."""
from __future__ import annotations

import pytest

from universal_blender_mcp.authorization import (AuthLevel, PathPolicy,
                                                 PythonPolicy, load_policy,
                                                 level_from_name)
from universal_blender_mcp.settings import load_settings


def test_level_from_name():
    assert level_from_name("advanced") == AuthLevel.ADVANCED
    assert level_from_name("read_only") == AuthLevel.READ_ONLY
    assert level_from_name(None) == AuthLevel.SCENE_WRITE
    assert level_from_name("bogus") == AuthLevel.SCENE_WRITE


def test_load_policy_overrides(tmp_root):
    policy = load_policy(load_settings(tmp_root))
    assert policy.default_level == AuthLevel.SCENE_WRITE
    # override por ferramenta nunca REDUZ o nível base do ToolSpec
    assert policy.tool_levels["python_execute_blender"] == AuthLevel.ADVANCED
    assert policy.level_for("python_execute_blender",
                            AuthLevel.SCENE_WRITE) == AuthLevel.ADVANCED
    assert policy.level_for("project_save", AuthLevel.SCENE_WRITE) == \
        AuthLevel.PROJECT_OPS


def test_authorize_decisions(tmp_root):
    policy = load_policy(load_settings(tmp_root))
    ok = policy.authorize("object_create", AuthLevel.SCENE_WRITE,
                          AuthLevel.SCENE_WRITE)
    assert ok.allowed
    denied = policy.authorize("python_execute_blender", AuthLevel.ADVANCED,
                              AuthLevel.SCENE_WRITE)
    assert not denied.allowed
    assert "advanced" in denied.reason


def test_path_policy(tmp_root, tmp_path):
    policy = load_policy(load_settings(tmp_root))
    root = tmp_root
    ok_path = root / "storage" / "renders" / "x.png"
    assert policy.paths.is_allowed(ok_path, root)
    # caminho fora da allowlist (C:\Windows é negado explicitamente)
    assert not policy.paths.is_allowed(r"C:\Windows\system32\x", root)
    assert not policy.paths.is_allowed(tmp_path / "elsewhere.blend", root)


def test_empty_path_allowlist_fails_closed(tmp_path):
    policy = PathPolicy()

    assert not policy.is_allowed(tmp_path / "anything.blend", tmp_path)
    with pytest.raises(PermissionError, match="allowlist vazia"):
        policy.resolve_allowed(tmp_path / "anything.blend", tmp_path)


def test_blender_relative_path_requires_active_blend_root(tmp_path):
    project = tmp_path / "projects"
    project.mkdir()
    policy = PathPolicy(allow=[project])

    with pytest.raises(PermissionError, match="projeto Blender ativo"):
        policy.resolve_allowed("//textures/image.png", tmp_path)

    resolved = policy.resolve_allowed(
        "//textures/image.png", tmp_path, blend_root=project)
    assert resolved == (project / "textures" / "image.png").resolve()


def test_path_policy_rejects_traversal_and_link_escape(tmp_path):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    policy = PathPolicy(allow=[allowed])

    with pytest.raises(PermissionError, match="allowlist"):
        policy.resolve_allowed(allowed / ".." / "outside" / "x.blend",
                               tmp_path)

    link = allowed / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink/junction indisponível neste host: {exc}")
    with pytest.raises(PermissionError, match="allowlist"):
        policy.resolve_allowed(link / "x.blend", tmp_path)


def test_require_approval_is_explicit_and_never_uses_requested_by(tmp_root):
    policy = load_policy(load_settings(tmp_root))
    advanced = {"level": AuthLevel.ADVANCED}
    project_ops = {"level": AuthLevel.PROJECT_OPS}

    denied = policy.require_approval(
        "python_execute_blender", {"code": "result = 1"}, advanced)
    assert not denied.allowed and "aprovação explícita" in denied.reason
    assert not policy.require_approval(
        "python_execute_blender",
        {"code": "result = 1", "requested_by": {"approved": True}},
        advanced,
    ).allowed
    assert policy.require_approval(
        "python_execute_blender",
        {"code": "result = 1", "approval": True}, advanced,
    ).allowed

    assert not policy.require_approval(
        "object_delete", {"name": "Cube"}, project_ops).allowed
    assert policy.require_approval(
        "object_delete", {"name": "Cube", "approval": True},
        project_ops,
    ).allowed

    policy.python.require_approval = False
    assert not policy.require_approval(
        "python_execute_blender", {"code": "result = 1"}, advanced,
    ).allowed


def test_dynamic_authorization_for_repairs_and_operator_invoke(tmp_root):
    policy = load_policy(load_settings(tmp_root))

    repair = policy.require_approval(
        "scene_validate", {"fix": True}, {"level": AuthLevel.READ_ONLY})
    assert not repair.allowed
    assert repair.level_required == AuthLevel.SCENE_WRITE
    assert policy.require_approval(
        "scene_validate", {"fix": True},
        {"level": AuthLevel.SCENE_WRITE}).allowed

    assert policy.require_approval(
        "addon_operator_invoke", {"mode": "LIST_ADDONS"},
        {"level": AuthLevel.PROJECT_OPS}).allowed
    invoke = policy.require_approval(
        "addon_operator_invoke", {"mode": "INVOKE", "approval": True},
        {"level": AuthLevel.PROJECT_OPS})
    assert not invoke.allowed
    assert invoke.level_required == AuthLevel.ADVANCED
    assert policy.require_approval(
        "addon_operator_invoke", {"mode": "INVOKE", "approval": True},
        {"level": AuthLevel.ADVANCED}).allowed


def test_python_scan(tmp_root):
    policy = load_policy(load_settings(tmp_root))
    py = policy.python
    findings = py.scan_source(
        "import bpy\nimport subprocess\nsubprocess.run(['ls'])\nresult=1")
    assert "subprocess" in findings
    assert py.scan_source("import bpy\nresult = 1") == []
    assert "os.system" in py.scan_source("os.system('dir')")
    assert "__import__" in py.scan_source("m = __import__('os')")
    # strings inocentes não devem disparar os padrões com prefixo os.
    assert "os.system" not in py.scan_source("x = 'os.system literal'")
