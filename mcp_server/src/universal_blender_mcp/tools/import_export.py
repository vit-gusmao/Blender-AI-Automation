"""Ferramentas de importação, exportação e integração com addons."""
from __future__ import annotations

from ..authorization import AuthLevel
from ..tool_registry import ServerContext, ToolRegistry, bridge_tool

IMPORT_FORMATS = ["AUTO", "OBJ", "FBX", "GLTF", "GLB", "STL", "PLY", "ALEMBIC",
                  "USD", "USDZ", "BLEND", "DAE", "SVG", "SVG_PATH", "X3D"]
EXPORT_FORMATS = ["OBJ", "FBX", "GLTF", "GLB", "STL", "PLY", "ALEMBIC", "USD",
                  "BLEND", "DAE", "X3D"]


def register(ctx: ServerContext, registry: ToolRegistry) -> None:
    bridge_tool(
        ctx, registry, "asset_import",
        "Importa um asset (OBJ, FBX, GLTF/GLB, STL, PLY, ALEMBIC, USD, "
        "BLEND, DAE, X3D) para a cena.",
        "import_export",
        {"properties": {
            "path": {"type": "string"},
            "format": {"type": "string", "enum": IMPORT_FORMATS},
            "params": {"type": "object", "description": "Opções do importador"},
        }, "required": ["path"]},
        AuthLevel.PROJECT_OPS, "medium", timeout_s=300,
    )
    bridge_tool(
        ctx, registry, "asset_export",
        "Exporta objetos (selecionados ou todos) para OBJ, FBX, GLTF/GLB, "
        "STL, PLY, ALEMBIC, USD, BLEND.",
        "import_export",
        {"properties": {
            "path": {"type": "string"},
            "objects": {"type": "array", "items": {"type": "string"},
                        "description": "Vazio = objetos selecionados"},
            "format": {"type": "string", "enum": EXPORT_FORMATS},
            "params": {"type": "object"},
        }, "required": ["path"]},
        AuthLevel.PROJECT_OPS, "medium", timeout_s=300,
    )
    bridge_tool(
        ctx, registry, "addon_operator_invoke",
        "Integração com addons instalados (§8.2): LIST_ADDONS, "
        "LIST_OPERATORS, OPERATOR_INFO e INVOKE com props.",
        "import_export",
        {"properties": {
            "mode": {"type": "string", "enum": ["LIST_ADDONS", "LIST_OPERATORS",
                                                "OPERATOR_INFO", "INVOKE"]},
            "operator": {"type": "string", "description": "bl_idname (INVOKE/OPERATOR_INFO)"},
            "props": {"type": "object"},
            "filter": {"type": "string", "description": "Prefixo para LIST_OPERATORS"},
        }, "required": ["mode"]},
        AuthLevel.PROJECT_OPS, "high", timeout_s=180,
    )
