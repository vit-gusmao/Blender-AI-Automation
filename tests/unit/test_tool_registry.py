"""Unit: catálogo de ferramentas — contrato completo do §6.1 do plano."""
from __future__ import annotations

import pytest

from universal_blender_mcp.tool_registry import ServerContext, build_registry

# O catálogo da tabela do §6.1 é o CONTRATO do projeto.
CATALOG = [
    # Conexão
    "blender_ping", "blender_get_version", "blender_get_capabilities",
    # Inspeção
    "scene_get_info", "scene_list_objects", "object_get_info",
    "scene_get_selection",
    # Objetos
    "object_create", "object_delete", "object_duplicate", "object_rename",
    "object_transform",
    # Modelagem
    "mesh_edit", "mesh_apply_modifier", "mesh_boolean", "mesh_validate",
    # Materiais
    "material_create", "material_assign", "material_set_property",
    # Nodes
    "nodes_list", "nodes_create", "nodes_connect", "nodes_set_input",
    # UV / textura
    "uv_unwrap", "texture_load", "texture_assign",
    # Rigging
    "armature_create", "bone_edit", "rig_assign_weights",
    # Animação
    "animation_keyframe", "animation_action", "animation_fcurve",
    "animation_nla",
    # Física
    "physics_configure", "simulation_bake",
    # Câmera e luz
    "camera_configure", "light_create", "light_configure",
    # Renderização
    "render_configure", "render_preview", "render_final",
    # Arquivos
    "project_open", "project_save", "project_checkpoint",
    # Importação
    "asset_import", "asset_export", "addon_operator_invoke",
    # Python
    "python_execute_blender", "python_execute_external",
    # Jobs
    "job_get_status", "job_cancel", "job_get_result",
    # Validação
    "scene_validate", "render_inspect", "object_compare",
]


@pytest.fixture()
def registry(settings):
    ctx = ServerContext(settings=settings, bridge=None, jobs=None,
                        policy=None, audit=None)
    return build_registry(ctx)


def test_full_catalog_present(registry):
    names = set(registry.names())
    missing = [n for n in CATALOG if n not in names]
    assert missing == [], f"ferramentas do §6.1 ausentes: {missing}"
    assert len(registry) == len(CATALOG)


def test_every_tool_has_description_and_schema(registry):
    for spec in registry.specs():
        assert len(spec.description) > 20, spec.name
        schema = spec.input_schema()
        assert schema["type"] == "object", spec.name
        assert isinstance(schema.get("properties"), dict), spec.name


def test_required_args_marked(registry):
    spec = registry.get("object_create")
    assert "type" in spec.params.get("required", [])
    assert spec.params["properties"]["type"]["enum"][0] == "CUBE"


def test_auth_levels(registry):
    assert registry.get("blender_ping").auth_level.name == "READ_ONLY"
    assert registry.get("object_create").auth_level.name == "SCENE_WRITE"
    assert registry.get("project_save").auth_level.name == "PROJECT_OPS"
    assert registry.get("python_execute_blender").auth_level.name == "ADVANCED"
    # ferramentas resolvidas no servidor não despacham para a Bridge
    for name in ("blender_get_capabilities", "job_get_status", "job_cancel",
                 "job_get_result", "render_inspect",
                 "python_execute_external"):
        assert registry.get(name).bridge is False, name
    # o resto despacha com o MESMO nome de operação (contrato do dispatcher)
    assert registry.get("object_create").bridge is True


def test_handlers_attached(registry):
    for spec in registry.specs():
        assert callable(spec.handler), spec.name


def test_categories(registry):
    by_cat = registry.by_category()
    assert "python" in by_cat and "python_execute_blender" in by_cat["python"]
    assert len(by_cat) >= 12


def test_render_inspect_denial_is_not_success(tmp_root):
    """BUG-025: negação de caminho em render_inspect não pode virar sucesso."""
    import asyncio

    from universal_blender_mcp.authorization import load_policy
    from universal_blender_mcp.settings import load_settings
    from universal_blender_mcp.tool_registry import ServerContext, ToolRegistry
    from universal_blender_mcp.tools import rendering

    settings = load_settings(tmp_root)
    settings.autostart_bridge = False
    ctx = ServerContext(settings=settings, bridge=None, jobs=None,
                        policy=load_policy(settings), audit=None)
    reg = ToolRegistry()
    rendering.register(ctx, reg)

    envelope = asyncio.run(
        reg.get("render_inspect").handler({"path": "C:\\Windows\\proibido.png"}))
    assert envelope["success"] is False
    assert envelope["error"]["type"] == "PathNotAllowed"


def test_capabilities_reflect_policy_overrides(settings):
    """BUG-025: capacidades refletem a configuração real (overrides inclusos)."""
    import asyncio

    from universal_blender_mcp.authorization import load_policy
    from universal_blender_mcp.tool_registry import ServerContext, ToolRegistry
    from universal_blender_mcp.tools import scene

    policy = load_policy(settings)
    policy.python.allow_blender = False
    policy.python.allow_external = False
    policy.external_backend = None
    ctx = ServerContext(settings=settings, bridge=None, jobs=None,
                        policy=policy, audit=None)
    ctx.session_level = policy.default_level
    reg = ToolRegistry()
    ctx.registry_ref = reg
    scene.register(ctx, reg)

    envelope = asyncio.run(
        reg.get("blender_get_capabilities").handler({}))
    assert envelope["success"] is True
    data = envelope["data"]
    assert data["session_level"] == "scene_write"
    assert data["policy"]["python_execution"]["allow_blender"] is False
    assert data["policy"]["python_execution"]["allow_external"] is False


def test_external_python_path_insert_is_idempotent(settings):
    """F5: sys.path não acumula entradas duplicadas do root do servidor."""
    import asyncio
    import sys

    from universal_blender_mcp.authorization import load_policy
    from universal_blender_mcp.tool_registry import ServerContext, ToolRegistry
    from universal_blender_mcp.tools import python_execution

    ctx = ServerContext(settings=settings, bridge=None, jobs=None,
                        policy=load_policy(settings), audit=None)
    reg = ToolRegistry()
    python_execution.register(ctx, reg)
    handler = reg.get("python_execute_external").handler
    root_str = str(settings.root)

    # Chamadas inválidas retornam cedo, mas após o sys.path.insert do handler.
    asyncio.run(handler({"runtime": "python"}))
    asyncio.run(handler({"runtime": "python"}))
    assert sys.path.count(root_str) == 1
