"""Ferramentas de rigging, animação e física."""
from __future__ import annotations

from ..authorization import AuthLevel
from ..tool_registry import ServerContext, ToolRegistry, bridge_tool


def register(ctx: ServerContext, registry: ToolRegistry) -> None:
    # ---- Rigging ---------------------------------------------------------------
    bridge_tool(
        ctx, registry, "armature_create",
        "Cria um armature com ossos declarados (name, head, tail, parent, "
        "use_connect, radius).",
        "rigging",
        {"properties": {
            "name": {"type": "string"},
            "bones": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "head": {"type": "array", "items": {"type": "number"}},
                    "tail": {"type": "array", "items": {"type": "number"}},
                    "parent": {"type": "string"},
                    "use_connect": {"type": "boolean"},
                    "radius": {"type": "number"},
                },
                "required": ["name", "head", "tail"],
            }},
            "collection": {"type": "string"},
        }, "required": ["name", "bones"]},
        AuthLevel.SCENE_WRITE, "medium",
    )
    bridge_tool(
        ctx, registry, "bone_edit",
        "Edita um osso de um armature (head, tail, roll, parent, exclusão).",
        "rigging",
        {"properties": {
            "armature": {"type": "string"},
            "bone": {"type": "string"},
            "head": {"type": "array", "items": {"type": "number"}},
            "tail": {"type": "array", "items": {"type": "number"}},
            "roll": {"type": "number"},
            "parent": {"type": "string"},
            "use_connect": {"type": "boolean"},
            "delete": {"type": "boolean"},
        }, "required": ["armature", "bone"]},
        AuthLevel.SCENE_WRITE, "medium",
    )
    bridge_tool(
        ctx, registry, "rig_assign_weights",
        "Atribui pesos de um armature a uma malha (AUTOMATIC_WEIGHTS ou "
        "ENVELOPE_WEIGHTS).",
        "rigging",
        {"properties": {
            "mesh": {"type": "string"},
            "armature": {"type": "string"},
            "mode": {"type": "string", "enum": ["AUTOMATIC_WEIGHTS", "ENVELOPE_WEIGHTS"]},
            "clear_existing": {"type": "boolean"},
        }, "required": ["mesh", "armature"]},
        AuthLevel.SCENE_WRITE, "high", timeout_s=180,
    )

    # ---- Animação ---------------------------------------------------------------
    bridge_tool(
        ctx, registry, "animation_keyframe",
        "Insere um keyframe em um objeto (data_path: location, "
        "rotation_euler, scale, ...).",
        "animation",
        {"properties": {
            "object": {"type": "string"},
            "data_path": {"type": "string"},
            "frame": {"type": "integer"},
            "index": {"type": "integer", "description": "-1 para todos os canais"},
            "value": {},
        }, "required": ["object", "data_path"]},
        AuthLevel.SCENE_WRITE, "low",
    )
    bridge_tool(
        ctx, registry, "animation_action",
        "Cria, atribui ou remove uma action de animação.",
        "animation",
        {"properties": {
            "action": {"type": "string"},
            "create": {"type": "boolean"},
            "assign_to": {"type": "string", "description": "Objeto que recebe a action"},
            "set_fake_user": {"type": "boolean"},
            "delete": {"type": "boolean"},
        }, "required": ["action"]},
        AuthLevel.SCENE_WRITE, "low",
    )
    bridge_tool(
        ctx, registry, "animation_fcurve",
        "Consulta e edita FCurves de uma action: LIST, SET_KEYFRAME, "
        "SET_INTERPOLATION, DELETE_KEYFRAME.",
        "animation",
        {"properties": {
            "action": {"type": "string"},
            "operation": {"type": "string",
                          "enum": ["LIST", "SET_KEYFRAME", "SET_INTERPOLATION",
                                   "DELETE_KEYFRAME"]},
            "data_path": {"type": "string"},
            "index": {"type": "integer"},
            "frame": {"type": "integer"},
            "value": {"type": "number"},
            "interpolation": {"type": "string",
                              "enum": ["BEZIER", "LINEAR", "CONSTANT"]},
        }, "required": ["action", "operation"]},
        AuthLevel.SCENE_WRITE, "low",
    )
    bridge_tool(
        ctx, registry, "animation_nla",
        "Opera a NLA de um objeto: LIST ou ADD_STRIP (action → track).",
        "animation",
        {"properties": {
            "object": {"type": "string"},
            "operation": {"type": "string", "enum": ["LIST", "ADD_STRIP"]},
            "track": {"type": "string"},
            "action": {"type": "string"},
            "start": {"type": "integer"},
        }, "required": ["object", "operation"]},
        AuthLevel.SCENE_WRITE, "medium",
    )

    # ---- Física -------------------------------------------------------------------
    bridge_tool(
        ctx, registry, "physics_configure",
        "Configura física em um objeto: RIGID_BODY, CLOTH, SOFT_BODY, "
        "COLLISION, FLUID. params recebe atributos específicos.",
        "physics",
        {"properties": {
            "object": {"type": "string"},
            "type": {"type": "string", "enum": ["RIGID_BODY", "CLOTH", "SOFT_BODY",
                                                "COLLISION", "FLUID"]},
            "params": {"type": "object"},
            "remove": {"type": "boolean"},
        }, "required": ["object", "type"]},
        AuthLevel.SCENE_WRITE, "medium",
    )
    bridge_tool(
        ctx, registry, "simulation_bake",
        "Bake de simulação (point cache / fluido) para um intervalo de frames.",
        "physics",
        {"properties": {
            "type": {"type": "string", "enum": ["ALL", "FLUID", "RIGID_BODY",
                                                "CLOTH", "SOFT_BODY"]},
            "frame_start": {"type": "integer"},
            "frame_end": {"type": "integer"},
        }},
        AuthLevel.PROJECT_OPS, "high", timeout_s=600,
    )
