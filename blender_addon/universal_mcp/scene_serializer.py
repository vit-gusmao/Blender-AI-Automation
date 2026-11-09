# SPDX-License-Identifier: MIT
"""Conversão segura de tipos do Blender/mathutils para JSON (camada 4)."""
from __future__ import annotations

import bpy


def to_jsonable(value):
    """Converte valores bpy/mathutils em dict/list escalares."""
    import mathutils

    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (mathutils.Vector, mathutils.Color)):
        return [round(c, 6) for c in value]
    if isinstance(value, mathutils.Euler):
        return {"type": "EULER", "rotation_mode": value.order,
                "rotation": [round(c, 6) for c in value]}
    if isinstance(value, mathutils.Quaternion):
        return {"type": "QUATERNION",
                "rotation": [round(c, 6) for c in value]}
    if isinstance(value, mathutils.Matrix):
        return [[round(c, 6) for c in row] for row in value]
    if isinstance(value, (set, frozenset)):
        return sorted(str(v) for v in value)
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    # Tipos bpy (Object, Material, ...) → identificador útil
    if hasattr(value, "name") and hasattr(value, "bl_rna"):
        return {"ref": type(value).__name__, "name": value.name}
    if hasattr(value, "name"):
        return str(value.name)
    return str(value)


def object_transforms(obj: bpy.types.Object) -> dict:
    return {
        "location": [round(c, 6) for c in obj.location],
        "rotation_mode": obj.rotation_mode,
        "rotation_euler": [round(c, 6) for c in obj.rotation_euler]
        if obj.rotation_mode in ("XYZ", "XZY", "YXZ", "YZX", "ZXY", "ZYX") else None,
        "rotation_quaternion": [round(c, 6) for c in obj.rotation_quaternion],
        "rotation_axis_angle": [round(c, 6) for c in obj.rotation_axis_angle],
        "scale": [round(c, 6) for c in obj.scale],
        "dimensions": [round(c, 6) for c in obj.dimensions],
    }


def mesh_stats(mesh) -> dict:
    return {"vertices": len(mesh.vertices), "edges": len(mesh.edges),
            "faces": len(mesh.polygons),
            "uv_layers": len(mesh.uv_layers)}


def modifier_summary(mod) -> dict:
    out = {"name": mod.name, "type": mod.type, "show_viewport": mod.show_viewport,
           "show_render": mod.show_render}
    if mod.type in ("MIRROR", "BOOLEAN", "LATTICE") and getattr(mod, "object", None):
        out["object"] = mod.object.name
    return out


def material_summary(mat) -> dict:
    return {"name": mat.name, "users": mat.users,
            "use_nodes": mat.use_nodes,
            "blend_method": getattr(mat, "blend_method", "")}
