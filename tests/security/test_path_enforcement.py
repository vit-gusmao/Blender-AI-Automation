"""Segurança: execuções negadas, caminhos e padrões bloqueados (§14)."""
from __future__ import annotations

import asyncio

import pytest

from universal_blender_mcp.authorization import AuthLevel, load_policy
from universal_blender_mcp.settings import load_settings
from universal_blender_mcp.tool_registry import ServerContext, build_registry


@pytest.fixture()
def secured_ctx(settings):
    """Contexto de servidor real (registry + policy) com Bridge fake."""
    from tests.conftest import FakeBlender

    class NullJobs:
        def get(self, *_):
            return None

        def cancel(self, *_):
            return {"cancelled": False}

    policy = load_policy(settings)

    class FakeBridge:
        def __init__(self):
            self.calls = []
            self.requests = []

        async def execute(self, operation, args=None, **kw):
            self.calls.append(operation)
            self.requests.append((operation, args, kw))
            return {"command_id": "x", "status": "completed",
                    "success": True, "data": {"ok": True},
                    "scene_revision": 1, "error": None}

    bridge = FakeBridge()
    ctx = ServerContext(settings=settings, bridge=bridge, jobs=NullJobs(),
                        policy=policy, audit=None,
                        session_level=AuthLevel.ADVANCED)
    registry = build_registry(ctx)
    return ctx, registry, bridge


async def _call(registry, name, args):
    """Invoca o handler como o server.call_tool faria (pós-autorização)."""
    from universal_blender_mcp.schemas import validate_arguments

    spec = registry.get(name)
    errors = validate_arguments(spec.input_schema(), args)
    assert errors == [], errors
    return await spec.handler(args), spec


def test_path_sensitive_tool_denies_outside_allowlist(secured_ctx, tmp_path):
    ctx, registry, bridge = secured_ctx
    envelope, _spec = asyncio.run(_call(
        registry, "project_save", {"path": str(tmp_path / "steal.blend")}))
    assert envelope["success"] is False
    assert envelope["error"]["type"] == "PathNotAllowed"
    assert bridge.calls == []  # nunca chegou à Bridge

    # dentro da allowlist (storage/) passa
    inside = str(ctx.settings.projects_dir / "ok.blend")
    envelope, _ = asyncio.run(_call(registry, "project_save",
                                     {"path": inside}))
    assert envelope["success"] is True
    assert bridge.calls == ["project_save"]


def test_path_check_covers_render_and_import(secured_ctx, tmp_path):
    ctx, registry, bridge = secured_ctx
    for tool in ("asset_import", "render_final"):
        args = ({"path": str(tmp_path / "x.obj")} if tool == "asset_import"
                else {"filepath": str(tmp_path / "x.png")})
        envelope, _ = asyncio.run(_call(registry, tool, args))
        assert envelope["error"]["type"] == "PathNotAllowed", tool
        assert bridge.calls == []


def test_blender_relative_paths_require_active_saved_project(secured_ctx):
    ctx, registry, bridge = secured_ctx
    envelope, _ = asyncio.run(_call(registry, "asset_import",
                                     {"path": "//textures/x.obj"}))
    assert envelope["success"] is False
    assert envelope["error"]["type"] == "PathNotAllowed"
    assert bridge.calls == []

    ctx.active_blend_root = ctx.settings.projects_dir
    envelope, _ = asyncio.run(_call(registry, "asset_import",
                                     {"path": "//textures/x.obj"}))
    assert envelope["success"] is True
    assert bridge.calls == ["asset_import"]


def test_blenderpath_double_slash_http_not_bypass(secured_ctx, tmp_path):
    """'//' no início é relativo ao blend; URLs http não devem passar."""
    _ctx, registry, _bridge = secured_ctx
    envelope, _ = asyncio.run(_call(
        registry, "asset_import", {"path": "http://evil/x.obj"}))
    assert envelope["error"]["type"] == "PathNotAllowed"


def test_nested_import_params_cannot_hide_outside_path(secured_ctx, tmp_path):
    _ctx, registry, bridge = secured_ctx
    spec = registry.get("asset_import")
    args = {
        "path": str(_ctx.settings.projects_dir / "safe.obj"),
        "params": {"options": {"output_filepath": str(tmp_path / "leak.txt")}},
    }

    assert str(tmp_path / "leak.txt") in spec.path_arguments(args)
    envelope, _ = asyncio.run(_call(registry, "asset_import", args))
    assert envelope["success"] is False
    assert envelope["error"]["type"] == "PathNotAllowed"
    assert bridge.calls == []


def test_operator_path_properties_are_gated_recursively(secured_ctx, tmp_path):
    _ctx, registry, bridge = secured_ctx
    args = {
        "mode": "INVOKE",
        "operator": "wm.open_mainfile",
        "props": {"nested": {"output_file": str(tmp_path / "outside.blend")}},
        "approval": True,
    }
    _ctx.session_level = AuthLevel.ADVANCED

    envelope, spec = asyncio.run(_call(
        registry, "addon_operator_invoke", args))
    assert str(tmp_path / "outside.blend") in spec.path_arguments(args)
    assert envelope["success"] is False
    assert envelope["error"]["type"] == "PathNotAllowed"
    assert bridge.calls == []


def test_bridge_dispatch_injects_trusted_authorization_metadata(secured_ctx):
    ctx, registry, bridge = secured_ctx
    ctx.session_level = AuthLevel.ADVANCED

    denied, _ = asyncio.run(_call(
        registry, "python_execute_blender",
        {"code": "result = 1", "requested_by": {"approved": True}}))
    assert denied["error"]["type"] == "ApprovalRequired"
    assert bridge.calls == []

    allowed, _ = asyncio.run(_call(
        registry, "python_execute_blender",
        {"code": "result = 1", "approval": True}))
    assert allowed["success"] is True
    operation, dispatched_args, kwargs = bridge.requests[-1]
    assert operation == "python_execute_blender"
    assert "approval" not in dispatched_args
    assert kwargs["authorization"] == {
        "level": "advanced", "approved": True}
