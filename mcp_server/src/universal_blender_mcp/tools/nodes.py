"""Ferramentas de node trees: materiais, geometry nodes e composição."""
from __future__ import annotations

from ..authorization import AuthLevel
from ..tool_registry import ServerContext, ToolRegistry, bridge_tool

TREE_TYPES = ["ShaderNodeTree", "GeometryNodeTree", "CompositorNodeTree"]


def register(ctx: ServerContext, registry: ToolRegistry) -> None:
    bridge_tool(
        ctx, registry, "nodes_list",
        "Lista nós e links de um node tree (material, geometry nodes ou "
        "compositing) com tipos, entradas e saídas.",
        "nodes",
        {"properties": {
            "tree": {"type": "string",
                     "description": "Nome do material (ShaderNodeTree), node_group ou cena"},
            "tree_type": {"type": "string", "enum": TREE_TYPES},
        }, "required": ["tree"]},
        AuthLevel.READ_ONLY, "low",
    )
    bridge_tool(
        ctx, registry, "nodes_create",
        "Cria nós em um node tree. Para materiais inexistentes, cria o "
        "material automaticamente.",
        "nodes",
        {"properties": {
            "tree": {"type": "string"},
            "tree_type": {"type": "string", "enum": TREE_TYPES},
            "nodes": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "description": "Ex.: ShaderNodeTexCoord"},
                    "name": {"type": "string"},
                    "location": {"type": "array", "items": {"type": "number"}},
                },
                "required": ["type"],
            }},
        }, "required": ["tree", "nodes"]},
        AuthLevel.SCENE_WRITE, "medium",
    )
    bridge_tool(
        ctx, registry, "nodes_connect",
        "Conecta a saída de um nó à entrada de outro (sockets por nome ou "
        "índice).",
        "nodes",
        {"properties": {
            "tree": {"type": "string"},
            "tree_type": {"type": "string", "enum": TREE_TYPES},
            "from_node": {"type": "string"},
            "from_socket": {},
            "to_node": {"type": "string"},
            "to_socket": {},
        }, "required": ["tree", "from_node", "to_node"]},
        AuthLevel.SCENE_WRITE, "medium",
    )
    bridge_tool(
        ctx, registry, "nodes_set_input",
        "Define o valor de um input de nó (por nome ou índice).",
        "nodes",
        {"properties": {
            "tree": {"type": "string"},
            "tree_type": {"type": "string", "enum": TREE_TYPES},
            "node": {"type": "string"},
            "input": {},
            "value": {},
        }, "required": ["tree", "node", "input", "value"]},
        AuthLevel.SCENE_WRITE, "medium",
    )
