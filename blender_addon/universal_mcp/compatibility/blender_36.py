# SPDX-License-Identifier: MIT
"""Helpers de compatibilidade com a API do Blender 3.6."""
from __future__ import annotations

import contextlib
import logging

import bpy

log = logging.getLogger("universal_mcp.compat")

#: Operadores de primitiva suportados por object_create (sem prefixo
#: "bpy.ops." — veja dispatcher._find_operator)
PRIMITIVES = {
    "CUBE": "mesh.primitive_cube_add",
    "UV_SPHERE": "mesh.primitive_uv_sphere_add",
    "IO_SPHERE": "mesh.primitive_ico_sphere_add",
    "CYLINDER": "mesh.primitive_cylinder_add",
    "CONE": "mesh.primitive_cone_add",
    "TORUS": "mesh.primitive_torus_add",
    "PLANE": "mesh.primitive_plane_add",
    "MONKEY": "mesh.primitive_monkey_add",
    "EMPTY": "object.empty_add",
    "ARMATURE": "object.armature_add",
    "CAMERA": "object.camera_add",
}


def version_info() -> dict:
    import platform
    import sys

    return {
        "blender_version": ".".join(str(n) for n in bpy.app.version),
        "version_string": bpy.app.version_string,
        "is_blender_36": bpy.app.version[:2] == (3, 6),
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "binary_path": bpy.app.binary_path,
        "background": bpy.app.background,
    }


def get_area_context(window_manager=None):
    """Localiza uma VIEW_3D area/region para overrides de contexto."""
    wm = window_manager or bpy.context.window_manager
    for window in wm.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                region = next((r for r in area.regions if r.type == "WINDOW"), None)
                if region:
                    return window, window.screen, area, region
    return None


@contextlib.contextmanager
def override_3d():
    """Context manager com override de contexto padrão da 3D View.

    Blender 3.6 possui ``bpy.context.temp_override``. Se não houver janela
    (background), usa o contexto tal como está.
    """
    found = get_area_context()
    if not found or bpy.app.background:
        yield bpy.context
        return
    window, screen, area, region = found
    with bpy.context.temp_override(
        window=window, screen=screen, area=area, region=region,
        scene=bpy.context.scene, view_layer=bpy.context.view_layer,
    ):
        yield bpy.context


def select_only(obj) -> None:
    """Deixa exatamente `obj` selecionado e ativo."""
    for other in bpy.context.selected_objects:
        other.select_set(False)
    if obj is not None:
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj


def ensure_collection(name: str):
    if name in bpy.data.collections:
        return bpy.data.collections[name]
    collection = bpy.data.collections.new(name)
    bpy.context.scene.collection.children.link(collection)
    return collection


def move_to_collection(obj, collection_name: str):
    collection = ensure_collection(collection_name)
    for coll in obj.users_collection:
        coll.objects.unlink(obj)
    collection.objects.link(obj)
    return collection


def operator_properties(op) -> dict:
    """Nomes de propriedades válidas de um operador (para filtrar kwargs)."""
    try:
        return {p.identifier for p in op.get_rna_type().properties
                if p.identifier not in ("rna_type",)}
    except (AttributeError, KeyError, RuntimeError):
        return set()
