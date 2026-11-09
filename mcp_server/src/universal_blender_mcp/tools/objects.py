"""Ferramentas de criação e manipulação de objetos."""
from __future__ import annotations

from ..authorization import AuthLevel
from ..tool_registry import ServerContext, ToolRegistry, bridge_tool

PRIMITIVE_TYPES = ["CUBE", "UV_SPHERE", "IO_SPHERE", "CYLINDER", "CONE", "TORUS",
                   "PLANE", "MONKEY", "EMPTY", "ARMATURE", "CAMERA"]


def register(ctx: ServerContext, registry: ToolRegistry) -> None:
    bridge_tool(
        ctx, registry, "object_create",
        "Cria um objeto primitivo (CUBE, UV_SPHERE, CYLINDER, CONE, TORUS, "
        "PLANE, MONKEY, EMPTY, ARMATURE, CAMERA) com nome, transformações e "
        "coleção opcionais.",
        "objects",
        {"properties": {
            "type": {"type": "string", "enum": PRIMITIVE_TYPES},
            "name": {"type": "string"},
            "location": {"type": "array", "items": {"type": "number"}},
            "rotation": {"type": "array", "items": {"type": "number"},
                         "description": "Rotação XYZ em radianos"},
            "scale": {"type": "array", "items": {"type": "number"}},
            "collection": {"type": "string"},
        }, "required": ["type"]},
        AuthLevel.SCENE_WRITE, "low",
    )
    bridge_tool(
        ctx, registry, "object_delete",
        "Exclui objetos da cena (global, não apenas da coleção). Use com "
        "checkpoint prévio.",
        "objects",
        {"properties": {
            "name": {"type": "string"},
            "names": {"type": "array", "items": {"type": "string"}},
            "delete_data": {"type": "boolean",
                            "description": "Remove também os datablocks órfãos"},
        }},
        AuthLevel.PROJECT_OPS, "destructive",
    )
    bridge_tool(
        ctx, registry, "object_duplicate",
        "Duplica um objeto existente, opcionalmente vinculado (linked).",
        "objects",
        {"properties": {
            "name": {"type": "string"},
            "new_name": {"type": "string"},
            "linked": {"type": "boolean"},
        }, "required": ["name"]},
        AuthLevel.SCENE_WRITE, "low",
    )
    bridge_tool(
        ctx, registry, "object_rename",
        "Renomeia um objeto (recusa se o novo nome já existir na cena).",
        "objects",
        {"properties": {"name": {"type": "string"}, "new_name": {"type": "string"}},
         "required": ["name", "new_name"]},
        AuthLevel.SCENE_WRITE, "low",
    )
    bridge_tool(
        ctx, registry, "object_transform",
        "Define localização, rotação (radianos) e escala de um objeto. "
        "Valores ausentes permanecem inalterados.",
        "objects",
        {"properties": {
            "name": {"type": "string"},
            "location": {"type": "array", "items": {"type": "number"}},
            "rotation": {"type": "array", "items": {"type": "number"}},
            "rotation_mode": {"type": "string",
                              "enum": ["XYZ", "XZY", "YXZ", "YZX", "ZXY", "ZYX",
                                       "QUATERNION", "AXIS_ANGLE"]},
            "scale": {"type": "array", "items": {"type": "number"}},
        }, "required": ["name"]},
        AuthLevel.SCENE_WRITE, "low",
    )
