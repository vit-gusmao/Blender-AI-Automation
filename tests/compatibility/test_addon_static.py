"""Compatibilidade: addon Python 3.10 puro + contrato dispatcher ↔ ferramentas.

Estes testes NÃO importam bpy: analisam os fontes com ast (sintaxe aceita
pelo Python 3.10 do Blender 3.6) e verificam o contrato pelo código.
"""
from __future__ import annotations

import ast
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ADDON_DIR = REPO / "blender_addon"
sys.path.insert(0, str(ADDON_DIR))

#: importes permitidos no addon: stdlib + API do Blender + pacote interno
ALLOWED_IMPORTS = {
    # stdlib
    "__future__", "bmesh", "mathutils", "io", "os", "re", "json", "time",
    "math", "sys", "traceback", "threading", "queue", "secrets",
    "datetime", "contextlib", "typing", "pathlib", "urllib", "platform",
    "importlib", "collections", "dataclasses", "enum", "logging",
    "sqlite3", "uuid", "hmac", "struct", "subprocess", "shutil",
    "tempfile", "asyncio", "ast", "hashlib",
    # API Blender 3.6
    "bpy", "bpy_extras",
}
RELATIVE_OK = True  # imports relativos do próprio pacote universal_mcp


def _addon_files():
    return sorted(ADDON_DIR.rglob("*.py"))


def test_addon_syntax_is_python_310():
    """Todo arquivo do addon deve parsear com a gramática do 3.10."""
    for path in _addon_files():
        source = path.read_text(encoding="utf-8")
        try:
            ast.parse(source, filename=str(path), feature_version=(3, 10))
        except SyntaxError as exc:
            pytest_fail = f"{path.name}: {exc}"
            raise AssertionError(pytest_fail) from exc


def test_addon_imports_are_stdlib_and_bpy_only():
    """Sem dependências externas no Python embutido do Blender (§3.2)."""
    violations = []
    for path in _addon_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level and node.level > 0:  # relativo (pacote interno)
                    continue
                names = [node.module or ""]
            for name in names:
                root = name.split(".")[0]
                if root and root not in ALLOWED_IMPORTS:
                    violations.append(f"{path.name}: import {name}")
    assert violations == [], "imports não permitidos no addon: " + \
        "; ".join(violations)


def test_bl_info_targets_supported_blender():
    init = (ADDON_DIR / "universal_mcp" / "__init__.py").read_text("utf-8")
    tree = ast.parse(init)
    bl_info = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and getattr(
                node.targets[0], "id", "") == "bl_info":
            bl_info = ast.literal_eval(node.value)
    assert bl_info is not None
    assert bl_info["blender"] == (3, 6, 0)
    assert bl_info["category"] == "System"


def test_dispatcher_implements_every_bridge_tool():
    """Cada ferramenta bridge=True do §6.1 precisa de op no dispatcher."""
    from universal_blender_mcp.tool_registry import ServerContext, build_registry
    from universal_blender_mcp.settings import load_settings

    settings = load_settings(REPO)
    registry = build_registry(ServerContext(
        settings=settings, bridge=None, jobs=None, policy=None, audit=None))

    source = (ADDON_DIR / "universal_mcp" / "dispatcher.py").read_text("utf-8")
    registered = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for deco in node.decorator_list:
                if (isinstance(deco, ast.Call)
                        and isinstance(deco.func, ast.Name)
                        and deco.func.id == "operation"
                        and deco.args):
                    registered.add(ast.literal_eval(deco.args[0]))

    bridge_tools = {s.name for s in registry.specs() if s.bridge}
    missing = bridge_tools - registered
    assert missing == set(), f"operações sem dispatcher: {sorted(missing)}"
    assert len(registered) >= 49


def test_addon_schema_mirror_matches_bridge_specs():
    from universal_mcp.operation_schemas import SCHEMAS
    from universal_blender_mcp.tool_registry import ServerContext, build_registry
    from universal_blender_mcp.settings import load_settings
    registry = build_registry(ServerContext(load_settings(REPO), None, None, None, None))
    assert SCHEMAS == {spec.name: spec.input_schema() for spec in registry.specs() if spec.bridge}


def test_auto_connect_invokes_registered_blender_operator(monkeypatch):
    import universal_mcp

    calls = []
    fake_bpy = types.SimpleNamespace(
        ops=types.SimpleNamespace(
            universal_mcp=types.SimpleNamespace(
                connect=lambda: calls.append("connect"))))
    monkeypatch.setitem(sys.modules, "bpy", fake_bpy)
    assert universal_mcp._auto_connect() is None
    assert calls == ["connect"]


def test_network_client_has_no_bpy_import():
    source = (ADDON_DIR / "universal_mcp" / "bridge_client.py").read_text("utf-8")
    assert not any(isinstance(node, ast.Import) and any(a.name == "bpy" for a in node.names)
                   for node in ast.walk(ast.parse(source)))
