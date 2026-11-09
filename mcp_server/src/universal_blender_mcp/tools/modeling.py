"""Ferramentas de modelagem: edição de malha, modificadores e booleans."""
from __future__ import annotations

from ..authorization import AuthLevel
from ..tool_registry import ServerContext, ToolRegistry, bridge_tool

MESH_OPS = ["EXTRUDE_REGION", "EXTRUDE_FACES", "EXTRUDE_VERTICES",
            "INSET_FACES", "BEVEL", "SUBDIVIDE_EDGES", "SUBDIVIDE_FACE",
            "LOOP_CUT", "DELETE_FACES", "DELETE_EDGES", "DELETE_VERTICES",
            "MERGE_VERTICES", "RECALC_NORMALS", "FLIP_NORMALS", "SMOOTH_VERTICES",
            "SHADE_SMOOTH", "SHADE_FLAT", "TRIANGULATE", "FILL_HOLE",
            "SET_ORIGIN_TO_GEOMETRY", "MIRROR_X", "MIRROR_Y", "MIRROR_Z"]

MODIFIER_TYPES = ["SUBSURF", "MIRROR", "BEVEL", "ARRAY", "SOLIDIFY", "LATTICE",
                  "CAST", "CURVE", "DISPLACE", "SIMPLE_DEFORM", "SUBSURF_SURFACE",
                  "TRIANGULATE", "WIREFRAME", "WELD", "SCREW", "OCEAN",
                  "GEOMETRY_NODES"]


def register(ctx: ServerContext, registry: ToolRegistry) -> None:
    bridge_tool(
        ctx, registry, "mesh_edit",
        "Aplica operações de edição de malha via bmesh em um objeto MESH: "
        "extrude, inset, bevel, subdivide, loop cut, delete, merge, normais, "
        "shade, triangulate, mirror e origem.",
        "modeling",
        {"properties": {
            "object": {"type": "string"},
            "operations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "op": {"type": "string", "enum": MESH_OPS},
                        "params": {"type": "object"},
                    },
                    "required": ["op"],
                },
            },
        }, "required": ["object", "operations"]},
        AuthLevel.SCENE_WRITE, "medium", timeout_s=180,
    )
    bridge_tool(
        ctx, registry, "mesh_apply_modifier",
        "Adiciona, aplica (evalua na malha) ou remove um modificador de um "
        "objeto. action: ADD | APPLY | REMOVE.",
        "modeling",
        {"properties": {
            "object": {"type": "string"},
            "action": {"type": "string", "enum": ["ADD", "APPLY", "REMOVE"]},
            "modifier": {"type": "string", "description": "Nome do modificador"},
            "type": {"type": "string", "enum": MODIFIER_TYPES,
                     "description": "Tipo (apenas ADD)"},
            "params": {"type": "object", "description": "Atributos do modificador"},
        }, "required": ["object", "action"]},
        AuthLevel.SCENE_WRITE, "medium",
    )
    bridge_tool(
        ctx, registry, "mesh_boolean",
        "Operação booleana entre duas malhas (UNION, DIFFERENCE, INTERSECT).",
        "modeling",
        {"properties": {
            "object": {"type": "string"},
            "operand": {"type": "string", "description": "Segundo objeto"},
            "operation": {"type": "string", "enum": ["UNION", "DIFFERENCE", "INTERSECT"]},
            "solver": {"type": "string", "enum": ["EXACT", "FAST"]},
            "delete_operand": {"type": "boolean"},
        }, "required": ["object", "operand", "operation"]},
        AuthLevel.SCENE_WRITE, "medium", timeout_s=180,
    )
    bridge_tool(
        ctx, registry, "mesh_validate",
        "Valida a topologia de uma malha (mesh.validate) e retorna contagens "
        "de vértices/arestas/faces e problemas encontrados.",
        "modeling",
        {"properties": {"object": {"type": "string"}}, "required": ["object"]},
        AuthLevel.READ_ONLY, "low",
    )
