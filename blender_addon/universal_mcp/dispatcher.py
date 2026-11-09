# SPDX-License-Identifier: MIT
"""Executor de operações na THREAD PRINCIPAL do Blender (camada 4).

O bridge_client entrega comandos à CommandQueue; o timer registrado em
``__init__.py`` (bpy.app.timers, §5.1) chama :func:`tick` que consome a fila,
executa a operação via :func:`execute_command` e devolve o envelope §6.4.

Toda operação é uma função ``op_<nome>(args, command)`` retornando dados
plain. Erros viram envelopes estruturados — nunca exceções para a Bridge.
"""
from __future__ import annotations

import math
import re
import time
import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import bpy
from mathutils import Vector

from . import scene_inspector, snapshot_manager
from .command_queue import CommandQueue
from .compatibility import blender_36 as compat
from .scene_serializer import to_jsonable
from .operation_validation import validate_arguments
from .render_state import PendingRender
from .storage_paths import storage_root
from .path_utils import normalize_argument_paths, resolve_blender_path
from .security import (DESTRUCTIVE_OPERATIONS, MUTATING_OPERATIONS,
                       AddonSecurity, is_destructive)

_OPERATIONS: Dict[str, Callable[[Dict[str, Any], Dict[str, Any]], Dict[str, Any]]] = {}

# Renders assíncronos pendentes (modo interface) — completados por handlers
# render_complete/render_cancel registrados no __init__ do addon.
PENDING_RENDERS: Dict[str, PendingRender] = {}
_REVISION_KEY = "mcp_scene_revision"


def operation(name: str):
    def deco(fn):
        _OPERATIONS[name] = fn
        return fn
    return deco


def operation_names():
    return sorted(_OPERATIONS)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_prefs():
    try:
        return bpy.context.preferences.addons["universal_mcp"].preferences
    except KeyError:
        return None


def _security() -> AddonSecurity:
    prefs = get_prefs()
    if prefs is None:
        return AddonSecurity()
    return AddonSecurity.from_prefs(prefs)


def _revision() -> int:
    return int(bpy.context.scene.get(_REVISION_KEY, 0))


def _bump_revision() -> int:
    rev = _revision() + 1
    bpy.context.scene[_REVISION_KEY] = rev
    return rev


def _obj(name: str):
    obj = bpy.data.objects.get(name)
    if obj is None:
        raise KeyError(f"objeto não encontrado: {name}")
    return obj


def _set_vec(target, values: Optional[List[float]]) -> None:
    if values is None:
        return
    if len(values) != len(target):
        raise ValueError(f"esperado {len(target)} componentes, recebido {len(values)}")
    for i, v in enumerate(values):
        target[i] = float(v)


def _find_operator(candidates: List[str]) -> Callable:
    """Retorna o primeiro operador existente entre candidatos.

    ``bpy.ops`` cria wrappers dinamicamente para QUALQUER nome — nomes
    inválidos só falham no ``poll()`` ("could not be found"). Por isso a
    existência é confirmada chamando poll em try/except.
    """
    last_error = None
    for path in candidates:
        owner, _, attr = path.partition(".")
        try:
            namespace = getattr(bpy.ops, owner)
            op = getattr(namespace, attr.replace(".", "_"))
            if not op.poll():
                raise RuntimeError(f"operator poll failed: {path}")
            return op
        except (AttributeError, RuntimeError) as exc:
            last_error = exc
            continue
    raise RuntimeError(
        f"nenhum operador disponível em: {candidates} ({last_error})")


def invoke_operator_checked(op, *args, **kwargs):
    if not op.poll():
        raise RuntimeError(f"operator poll failed: {op}")
    result = op(*args, **kwargs)
    # INVOKE starts a modal render; the final outcome is correlated separately.
    permitted = ({"FINISHED"}, {"RUNNING_MODAL"}) if args and args[0] == "INVOKE_DEFAULT" else ({"FINISHED"},)
    if result not in permitted:
        raise RuntimeError(f"operator did not finish: {result}")
    return result


@contextmanager
def preserve_selection():
    selected_names = [o.name for o in bpy.context.selected_objects]
    active = bpy.context.view_layer.objects.active
    active_name = active.name if active else None
    mode = active.mode if active else "OBJECT"
    # Never retain a ViewLayer/Object RNA wrapper across a handler: arbitrary
    # operators/Python can replace Main as well as the structured open tools.
    del active
    main_replaced = []

    @bpy.app.handlers.persistent
    def on_load_pre(_dummy):
        main_replaced.append(True)

    bpy.app.handlers.load_pre.append(on_load_pre)
    try:
        yield
    finally:
        if on_load_pre in bpy.app.handlers.load_pre:
            bpy.app.handlers.load_pre.remove(on_load_pre)
        # A file load owns the loaded scene's saved selection. libraries.load /
        # libraries.write do not replace Main and still restore normal state.
        if not main_replaced:
            view = bpy.context.view_layer
            current = view.objects.active
            surviving = [view.objects[name] for name in selected_names if name in view.objects]
            surviving_active = view.objects.get(active_name) if active_name else None
            if (list(bpy.context.selected_objects) != surviving or current != surviving_active
                    or current and current.mode != mode):
                if current and current.mode != "OBJECT":
                    invoke_operator_checked(bpy.ops.object.mode_set, mode="OBJECT")
                for obj in view.objects:
                    obj.select_set(False)
                for obj in surviving:
                    obj.select_set(True)
                view.objects.active = surviving_active
                if surviving_active and mode != "OBJECT":
                    invoke_operator_checked(bpy.ops.object.mode_set, mode=mode)


def _filtered_props(op, params: Dict[str, Any]) -> Dict[str, Any]:
    allowed = compat.operator_properties(op)
    if not allowed:
        return {}
    return {k: v for k, v in (params or {}).items() if k in allowed}


def _mode_override(obj, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Override mínimo para operadores que precisam de objeto ativo."""
    override: Dict[str, Any] = {
        "object": obj, "active_object": obj, "selected_objects": [obj],
    }
    found = compat.get_area_context()
    if found and not bpy.app.background:
        window, screen, area, region = found
        override.update(window=window, screen=screen, area=area, region=region)
    if bpy.context.scene is not None:
        override["scene"] = bpy.context.scene
    if bpy.context.view_layer is not None:
        override["view_layer"] = bpy.context.view_layer
    override.update(extra or {})
    return override


def _renders_dir(create=True) -> Path:
    prefs = get_prefs()
    custom = getattr(prefs, "renders_dir", "") if prefs else ""
    if custom:
        root = resolve_blender_path(custom)
    else:
        root = storage_root(prefs) / "renders"
    if create:
        if not _security().path_allowed(str(root)):
            raise PermissionError(f"path not authorized: {root}")
        root.mkdir(parents=True, exist_ok=True)
    return root


def _maybe_checkpoint(command: Dict[str, Any]) -> Optional[str]:
    if not is_destructive(command.get("operation"), command.get("arguments")):
        return None
    prefs = get_prefs()
    if prefs is None or not getattr(prefs, "auto_checkpoint", True):
        return None
    return snapshot_manager.create_checkpoint(label=f"auto_{command.get('operation')}")["checkpoint"]


# ---------------------------------------------------------------------------
# Conexão / inspeção
# ---------------------------------------------------------------------------

@operation("blender_ping")
def op_blender_ping(args, command):
    return {"pong": True,
            "blender_version": bpy.app.version_string,
            "background": bpy.app.background,
            "scene_revision": _revision(),
            "object_count": len(bpy.context.scene.objects)}


@operation("blender_get_version")
def op_blender_get_version(args, command):
    return compat.version_info()


@operation("scene_get_info")
def op_scene_get_info(args, command):
    return scene_inspector.scene_info(
        include_collections=bool(args.get("include_collections", False)))


@operation("scene_list_objects")
def op_scene_list_objects(args, command):
    return {"objects": scene_inspector.list_objects(
        obj_type=args.get("type"), collection=args.get("collection"),
        limit=int(args.get("limit", 200)))}


@operation("object_get_info")
def op_object_get_info(args, command):
    return scene_inspector.object_info(
        args["name"],
        include_modifiers=bool(args.get("include_modifiers", True)),
        include_materials=bool(args.get("include_materials", True)))


@operation("scene_get_selection")
def op_scene_get_selection(args, command):
    return scene_inspector.selection()


@operation("scene_validate")
def op_scene_validate(args, command):
    return scene_inspector.validate_scene(
        checks=args.get("checks"), fix=bool(args.get("fix", False)))


@operation("object_compare")
def op_object_compare(args, command):
    return scene_inspector.compare_objects(args["a"], args["b"],
                                           args.get("check"))


# ---------------------------------------------------------------------------
# Objetos
# ---------------------------------------------------------------------------

@operation("object_create")
def op_object_create(args, command):
    prim = args["type"]
    path = compat.PRIMITIVES.get(prim)
    if path is None:
        raise ValueError(f"tipo de primitiva não suportado: {prim}")
    name = args.get("name") or f"MCP_{prim.title()}"

    existing = bpy.data.objects.get(name)
    if existing is not None:  # idempotência (§6.4)
        expected_type = {"EMPTY": "EMPTY", "ARMATURE": "ARMATURE",
                         "CAMERA": "CAMERA"}.get(prim, "MESH")
        if existing.type != expected_type:
            raise ValueError(f"{name} is {existing.type}, expected {expected_type}")
        return {"object_name": existing.name, "object_type": existing.type,
                "reused": True, "location": list(existing.location)}

    op = _find_operator([path])
    params: Dict[str, Any] = {}
    if args.get("location"):
        params["location"] = tuple(args["location"])
    if args.get("rotation"):
        params["rotation"] = tuple(args["rotation"])
    with compat.override_3d():
        invoke_operator_checked(op, **_filtered_props(op, params))
    obj = bpy.context.active_object
    if obj is None:
        raise RuntimeError("operador não produziu objeto ativo")
    obj.name = name
    if args.get("scale"):
        _set_vec(obj.scale, args["scale"])
    if args.get("collection"):
        compat.move_to_collection(obj, args["collection"])
    return {"object_name": obj.name, "object_type": obj.type,
            "reused": False, "location": list(obj.location),
            "collection": args.get("collection")}


@operation("object_delete")
def op_object_delete(args, command):
    names = list(args.get("names") or [])
    if args.get("name"):
        names.append(args["name"])
    targets = [_obj(name) for name in dict.fromkeys(names)]
    data_blocks = {obj.data for obj in targets if obj.data is not None}
    removed = []
    for obj in targets:
        name = obj.name
        bpy.data.objects.remove(obj, do_unlink=True)
        removed.append(name)
    if args.get("delete_data"):
        for data in data_blocks:
            if data.users == 0:
                bpy.data.batch_remove(ids=(data,))
    return {"removed": removed, "count": len(removed)}


def _purge_orphans() -> int:
    purged = 0
    for coll, attr in ((bpy.data.meshes, "meshes"), (bpy.data.materials, "materials"),
                       (bpy.data.lights, "lights"), (bpy.data.cameras, "cameras"),
                       (bpy.data.armatures, "armatures")):
        for block in list(coll):
            if block.users == 0:
                coll.remove(block)
                purged += 1
    return purged


@operation("object_duplicate")
def op_object_duplicate(args, command):
    src = _obj(args["name"])
    new_obj = src.copy()
    if not args.get("linked", False):
        if src.data is not None:
            new_obj.data = src.data.copy()
    new_obj.name = args.get("new_name") or f"{src.name}_copy"
    for coll in src.users_collection:
        coll.objects.link(new_obj)
        break
    return {"object_name": new_obj.name, "source": src.name,
            "linked": bool(args.get("linked", False))}


@operation("object_rename")
def op_object_rename(args, command):
    obj = _obj(args["name"])
    new_name = args["new_name"]
    if bpy.data.objects.get(new_name) is not None:
        raise ValueError(f"já existe objeto chamado {new_name}")
    old = obj.name
    obj.name = new_name
    return {"old_name": old, "new_name": obj.name}


@operation("object_transform")
def op_object_transform(args, command):
    obj = _obj(args["name"])
    if args.get("rotation_mode"):
        obj.rotation_mode = args["rotation_mode"]
    _set_vec(obj.location, args.get("location"))
    _set_vec(obj.scale, args.get("scale"))
    if args.get("rotation"):
        rot = args["rotation"]
        if obj.rotation_mode == "QUATERNION":
            # rotação fornecida em radianos XYZ → converte para quaternion
            from mathutils import Euler
            if len(rot) == 4:
                _set_vec(obj.rotation_quaternion, rot)
            else:
                obj.rotation_quaternion = Euler(rot, "XYZ").to_quaternion()
        elif obj.rotation_mode == "AXIS_ANGLE":
            _set_vec(obj.rotation_axis_angle, rot)
        else:
            _set_vec(obj.rotation_euler, rot)
    return {"object_name": obj.name,
            "location": list(obj.location), "scale": list(obj.scale)}


# ---------------------------------------------------------------------------
# Modelagem
# ---------------------------------------------------------------------------

@operation("mesh_edit")
def op_mesh_edit(args, command):
    import bmesh

    obj = _obj(args["object"])
    if obj.type != "MESH":
        raise ValueError(f"objeto {obj.name} não é MESH")
    steps = args["operations"]
    if any(step["op"] == "SET_ORIGIN_TO_GEOMETRY" for step in steps):
        if len(steps) != 1:
            raise ValueError("origin changes require a separate command")
        compat.select_only(obj)
        with compat.override_3d():
            invoke_operator_checked(bpy.ops.object.origin_set, type="ORIGIN_CENTER_OF_MASS")
        return {"object": obj.name, "operations": [{"origin": "center_of_mass"}]}
    applied: List[Dict[str, Any]] = []
    # All steps operate on a detached mesh. Commit to the original datablock
    # only after the complete batch succeeds (including conversion errors).
    working = obj.data.copy()
    try:
        for step in steps:
            op_name = step["op"]
            params = step.get("params") or {}
            applied.append({"op": op_name, **_bmesh_op(working, op_name, params, bmesh)})
        bm = bmesh.new()
        try:
            bm.from_mesh(working)
            bm.to_mesh(obj.data)
        finally:
            bm.free()
    finally:
        bpy.data.meshes.remove(working)
    obj.data.update()
    return {"object": obj.name, "operations": applied,
            "mesh": {"vertices": len(obj.data.vertices),
                     "faces": len(obj.data.polygons)}}


@contextmanager
def _mesh_transaction(me, bmesh):
    bm = bmesh.new()
    bm.from_mesh(me)
    try:
        yield bm
        bm.to_mesh(me)
    finally:
        bm.free()


def _bmesh_op(me, op_name: str, params: Dict[str, Any], bmesh) -> Dict[str, Any]:
    with _mesh_transaction(me, bmesh) as bm:
        if op_name in ("EXTRUDE_REGION", "EXTRUDE_FACES"):
            geom = bm.faces[:]
            result = bmesh.ops.extrude_face_region(bm, geom=geom)
            new = [v for v in result["geom"] if isinstance(v, bmesh.types.BMVert)]
            offset = float(params.get("offset", 0.0))
            if offset and new:
                normal = Vector((0, 0, 1))
                for f in bm.faces:
                    normal = f.normal
                    break
                for v in new:
                    v.co += normal * offset
            return {"extruded": True}
        if op_name == "EXTRUDE_VERTICES":
            result = bmesh.ops.extrude_vert_only(bm, verts=bm.verts[:])
            return {"extruded": len(result.get("verts", []))}
        if op_name == "INSET_FACES":
            bmesh.ops.inset_individual(
                bm, faces=bm.faces[:], thickness=float(params.get("thickness", 0.05)),
                depth=float(params.get("depth", 0.0)))
            return {"inset": True}
        if op_name == "BEVEL":
            bmesh.ops.bevel(
                bm, geom=bm.edges[:], offset=float(params.get("width", 0.02)),
                offset_type="WIDTH", segments=int(params.get("segments", 1)),
                profile=float(params.get("profile", 0.5)))
            return {"bevel": True}
        if op_name == "SUBDIVIDE_EDGES":
            bmesh.ops.subdivide_edges(
                bm, edges=bm.edges[:], cuts=int(params.get("cuts", 1)),
                use_grid_fill=True)
            return {"subdivided": True}
        if op_name in ("SUBDIVIDE_FACE",):
            bmesh.ops.subdivide_edges(
                bm, edges=bm.edges[:], cuts=int(params.get("cuts", 1)))
            return {"subdivided": True}
        if op_name in ("DELETE_FACES", "DELETE_EDGES", "DELETE_VERTICES"):
            kind = {"DELETE_FACES": "faces", "DELETE_EDGES": "edges",
                    "DELETE_VERTICES": "verts"}[op_name]
            if kind == "faces":
                bmesh.ops.delete(bm, geom=bm.faces[:], context="FACES")
            elif kind == "edges":
                bmesh.ops.delete(bm, geom=bm.edges[:], context="EDGES")
            else:
                bmesh.ops.delete(bm, geom=bm.verts[:], context="VERTS")
            return {"deleted": kind}
        if op_name == "MERGE_VERTICES":
            before = len(bm.verts)
            bmesh.ops.remove_doubles(bm, verts=bm.verts[:],
                                     dist=float(params.get("distance", 0.0001)))
            return {"merged": before - len(bm.verts)}
        if op_name == "RECALC_NORMALS":
            bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
            return {"recalc": True}
        if op_name == "FLIP_NORMALS":
            bmesh.ops.reverse_faces(bm, faces=bm.faces[:])
            return {"flipped": True}
        if op_name == "SMOOTH_VERTICES":
            bmesh.ops.smooth_vert(
                bm, verts=bm.verts[:], factor=float(params.get("factor", 0.5)),
                use_axis_x=True, use_axis_y=True, use_axis_z=True)
            return {"smoothed": True}
        if op_name == "TRIANGULATE":
            bmesh.ops.triangulate(bm, faces=bm.faces[:])
            return {"triangulated": True}
        if op_name == "FILL_HOLE":
            bmesh.ops.holes_fill(bm, edges=bm.edges[:],
                                 sides=int(params.get("sides", 8)))
            return {"filled": True}
        if op_name in ("MIRROR_X", "MIRROR_Y", "MIRROR_Z"):
            axis = op_name[-1].lower()
            for v in bm.verts:
                setattr(v.co, axis, -getattr(v.co, axis))
            return {"mirrored": axis}
        if op_name in ("SHADE_SMOOTH", "SHADE_FLAT"):
            for face in bm.faces:
                face.smooth = op_name == "SHADE_SMOOTH"
            return {"shade": op_name}
        if op_name == "LOOP_CUT":
            raise NotImplementedError(
                "LOOP_CUT exige contexto interativo de edição; use "
                "SUBDIVIDE_EDGES ou python_execute_blender com bmesh "
                "(bmesh.ops.subdivide_edgering)")
        raise ValueError(f"operação de malha desconhecida: {op_name}")


@operation("mesh_apply_modifier")
def op_mesh_apply_modifier(args, command):
    obj = _obj(args["object"])
    action = args["action"]
    if action == "ADD":
        mod_type = args.get("type")
        if not mod_type:
            raise ValueError("type obrigatório para ADD")
        mod = obj.modifiers.new(args.get("modifier") or mod_type.title(),
                                mod_type)
        for key, value in (args.get("params") or {}).items():
            if hasattr(mod, key):
                setattr(mod, key, value)
        return {"modifier": mod.name, "type": mod.type, "action": "ADD"}
    if action == "REMOVE":
        mod_name = args.get("modifier")
        if mod_name not in obj.modifiers:
            raise KeyError(f"modificador não encontrado: {mod_name}")
        obj.modifiers.remove(obj.modifiers[mod_name])
        return {"modifier": mod_name, "action": "REMOVE"}
    if action == "APPLY":
        mod_name = args.get("modifier")
        if mod_name and mod_name not in obj.modifiers:
            raise KeyError(f"modificador não encontrado: {mod_name}")
        compat.select_only(obj)
        with compat.override_3d():
            invoke_operator_checked(bpy.ops.object.mode_set, mode="OBJECT")
            invoke_operator_checked(bpy.ops.object.modifier_apply, 
                modifier=mod_name or obj.modifiers[0].name)
        return {"modifier": mod_name, "action": "APPLY"}
    raise ValueError(f"ação desconhecida: {action}")


@operation("mesh_boolean")
def op_mesh_boolean(args, command):
    obj = _obj(args["object"])
    operand = _obj(args["operand"])
    mod = obj.modifiers.new("MCP_Boolean", "BOOLEAN")
    mod.object = operand
    mod.operation = args["operation"].upper()
    mod.solver = args.get("solver", "EXACT")
    compat.select_only(obj)
    with compat.override_3d():
        invoke_operator_checked(bpy.ops.object.mode_set, mode="OBJECT")
        invoke_operator_checked(bpy.ops.object.modifier_apply, modifier=mod.name)
    if args.get("delete_operand", True):
        bpy.data.objects.remove(operand, do_unlink=True)
    return {"object": obj.name, "operation": args["operation"],
            "operand_deleted": bool(args.get("delete_operand", True)),
            "mesh": {"vertices": len(obj.data.vertices),
                     "faces": len(obj.data.polygons)}}


@operation("mesh_validate")
def op_mesh_validate(args, command):
    obj = _obj(args["object"])
    copy = obj.data.copy()
    try:
        fixed = copy.validate(verbose=False)
    finally:
        bpy.data.meshes.remove(copy)
    return {"object": obj.name, "had_errors": fixed,
            "vertices": len(obj.data.vertices),
            "edges": len(obj.data.edges), "faces": len(obj.data.polygons)}


# ---------------------------------------------------------------------------
# Materiais / UV / texturas
# ---------------------------------------------------------------------------

_PRINCIPLED_INPUTS = {
    "base_color": "Base Color", "metallic": "Metallic",
    "roughness": "Roughness", "alpha": "Alpha",
    "emission_color": "Emission", "emission_strength": "Emission Strength",
    "ior": "IOR",
}


def _principled(material) -> Any:
    material.use_nodes = True
    for node in material.node_tree.nodes:
        if node.type == "BSDF_PRINCIPLED":
            return node
    raise RuntimeError("Principled BSDF não encontrado no material")


@operation("material_create")
def op_material_create(args, command):
    name = args["name"]
    mat = bpy.data.materials.get(name)
    reused = mat is not None
    if mat is None:
        mat = bpy.data.materials.new(name)
    bsdf = _principled(mat)
    for key, input_name in _PRINCIPLED_INPUTS.items():
        if key in args and input_name in bsdf.inputs:
            bsdf.inputs[input_name].default_value = args[key]
    return {"material": mat.name, "reused": reused}


@operation("material_assign")
def op_material_assign(args, command):
    obj = _obj(args["object"])
    mat = bpy.data.materials.get(args["material"])
    if mat is None:
        raise KeyError(f"material não encontrado: {args['material']}")
    slot = args.get("slot")
    if slot is None:
        obj.data.materials.append(mat)
        slot = len(obj.material_slots) - 1
    else:
        while len(obj.material_slots) <= slot:
            obj.data.materials.append(None)
        obj.material_slots[slot].material = mat
    return {"object": obj.name, "material": mat.name, "slot": int(slot)}


@operation("material_set_property")
def op_material_set_property(args, command):
    mat = bpy.data.materials.get(args["material"])
    if mat is None:
        raise KeyError(f"material não encontrado: {args['material']}")
    prop = args["property"]
    value = args["value"]
    if prop.startswith("principled."):
        input_name = prop.split(".", 1)[1]
        bsdf = _principled(mat)
        if input_name not in bsdf.inputs:
            raise KeyError(f"input inexistente no Principled: {input_name}")
        bsdf.inputs[input_name].default_value = value
    else:
        if not hasattr(mat, prop):
            raise KeyError(f"propriedade inexistente no material: {prop}")
        setattr(mat, prop, value)
    return {"material": mat.name, "property": prop, "value": to_jsonable(value)}


@operation("uv_unwrap")
def op_uv_unwrap(args, command):
    obj = _obj(args["object"])
    compat.select_only(obj)
    method = args.get("method", "SMART_PROJECT")
    margin = float(args.get("margin", 0.001))
    with compat.override_3d():
        invoke_operator_checked(bpy.ops.object.mode_set, mode="EDIT")
        invoke_operator_checked(bpy.ops.mesh.select_all, action="SELECT")
        try:
            if method == "SMART_PROJECT":
                invoke_operator_checked(bpy.ops.uv.smart_project, 
                    angle_limit=float(args.get("angle_limit", 1.151)),
                    island_margin=margin)
            elif method == "SPHERE_PROJECT":
                invoke_operator_checked(bpy.ops.uv.sphere_project, correct_aspect=True, margin=margin)
            elif method == "CYLINDER_PROJECT":
                invoke_operator_checked(bpy.ops.uv.cylinder_project, correct_aspect=True, margin=margin)
            elif method == "CUBE_PROJECT":
                invoke_operator_checked(bpy.ops.uv.cube_project, cube_size=1.0, margin=margin)
            else:
                invoke_operator_checked(bpy.ops.uv.unwrap, margin=margin)
        finally:
            invoke_operator_checked(bpy.ops.object.mode_set, mode="OBJECT")
    return {"object": obj.name, "method": method,
            "uv_layers": len(obj.data.uv_layers)}


@operation("texture_load")
def op_texture_load(args, command):
    path = args["path"]
    if not _security().path_allowed(path):
        raise PermissionError(f"caminho não autorizado: {path}")
    name = args.get("name") or Path(path).stem
    img = bpy.data.images.get(name)
    loaded = img is None
    if loaded:
        img = bpy.data.images.load(path, check_existing=True)
        img.name = name
    return {"texture": img.name, "path": path, "loaded": loaded,
            "size": list(img.size)}


@operation("texture_assign")
def op_texture_assign(args, command):
    mat = bpy.data.materials.get(args["material"])
    if mat is None:
        raise KeyError(f"material não encontrado: {args['material']}")
    img = bpy.data.images.get(args["texture"])
    if img is None:
        raise KeyError(f"textura não encontrada: {args['texture']}")
    if args.get("non_color"):
        img.colorspace_settings.name = "Non-Color"
    mat.use_nodes = True
    tree = mat.node_tree
    tex = next((n for n in tree.nodes
                if n.type == "TEX_IMAGE" and n.image == img), None)
    if tex is None:
        tex = tree.nodes.new("ShaderNodeTexImage")
        tex.image = img
        tex.location = (-400, 0)
    bsdf = _principled(mat)
    input_name = args.get("input", "Base Color")
    if input_name not in bsdf.inputs:
        raise KeyError(f"input inexistente no Principled: {input_name}")
    tree.links.new(tex.outputs["Color"], bsdf.inputs[input_name])
    return {"material": mat.name, "texture": img.name, "input": input_name}


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

def _resolve_tree(tree_name: str, tree_type: str, readonly=False):
    if tree_type == "ShaderNodeTree":
        mat = bpy.data.materials.get(tree_name)
        if mat is None:
            raise KeyError(f"material não encontrado: {tree_name}")
        if not readonly:
            mat.use_nodes = True
        return mat.node_tree
    if tree_type == "GeometryNodeTree":
        tree = bpy.data.node_groups.get(tree_name)
        if tree is None:
            raise KeyError(f"node group não encontrado: {tree_name}")
        return tree
    if tree_type == "CompositorNodeTree":
        scene = bpy.context.scene
        if not readonly:
            scene.use_nodes = True
        return scene.node_tree
    raise ValueError(f"tree_type desconhecido: {tree_type}")


def _socket_summary(sockets) -> List[Dict[str, Any]]:
    out = []
    for i, s in enumerate(sockets):
        out.append({"index": i, "name": s.name, "type": s.type,
                    "linked": bool(s.links) if hasattr(s, "links") else None})
    return out


@operation("nodes_list")
def op_nodes_list(args, command):
    tree = _resolve_tree(args["tree"], args.get("tree_type", "ShaderNodeTree"), readonly=True)
    if tree is None:
        return {"tree": args["tree"], "nodes": [], "links": []}
    return {
        "tree": tree.name if hasattr(tree, "name") else args["tree"],
        "nodes": [{"name": n.name, "type": n.type,
                   "label": getattr(n, "label", ""),
                   "location": list(n.location),
                   "inputs": _socket_summary(n.inputs),
                   "outputs": _socket_summary(n.outputs)}
                  for n in tree.nodes],
        "links": [{"from": l.from_node.name, "from_socket": l.from_socket.name,
                   "to": l.to_node.name, "to_socket": l.to_socket.name}
                  for l in tree.links],
    }


@operation("nodes_create")
def op_nodes_create(args, command):
    tree = _resolve_tree(args["tree"], args.get("tree_type", "ShaderNodeTree"))
    created = []
    for spec in args["nodes"]:
        node = tree.nodes.new(spec["type"])
        if spec.get("name"):
            node.name = spec["name"]
        if spec.get("location"):
            node.location = spec["location"]
        created.append({"name": node.name, "type": node.type})
    return {"created": created}


@operation("nodes_connect")
def op_nodes_connect(args, command):
    tree = _resolve_tree(args["tree"], args.get("tree_type", "ShaderNodeTree"))
    from_node = tree.nodes.get(args["from_node"])
    to_node = tree.nodes.get(args["to_node"])
    if from_node is None or to_node is None:
        raise KeyError("nó de origem/destino não encontrado")
    from_sock = _pick_socket(from_node.outputs, args.get("from_socket"))
    to_sock = _pick_socket(to_node.inputs, args.get("to_socket"))
    tree.links.new(from_sock, to_sock)
    return {"from": f"{from_node.name}.{from_sock.name}",
            "to": f"{to_node.name}.{to_sock.name}"}


def _pick_socket(sockets, key):
    if key is None:
        return sockets[0]
    if isinstance(key, int):
        return sockets[key]
    for s in sockets:
        if s.name == key:
            return s
    raise KeyError(f"socket não encontrado: {key}")


@operation("nodes_set_input")
def op_nodes_set_input(args, command):
    tree = _resolve_tree(args["tree"], args.get("tree_type", "ShaderNodeTree"))
    node = tree.nodes.get(args["node"])
    if node is None:
        raise KeyError(f"nó não encontrado: {args['node']}")
    sock = _pick_socket(node.inputs, args.get("input"))
    value = args["value"]
    sock.default_value = value
    return {"node": node.name, "input": sock.name, "value": to_jsonable(value)}


# ---------------------------------------------------------------------------
# Rigging
# ---------------------------------------------------------------------------

@operation("armature_create")
def op_armature_create(args, command):
    arm_data = bpy.data.armatures.new(args.get("name", "MCP_Armature"))
    obj = bpy.data.objects.new(args.get("name", "MCP_Armature"), arm_data)
    bpy.context.scene.collection.objects.link(obj)
    compat.select_only(obj)

    with bpy.context.temp_override(**_mode_override(obj)):
        invoke_operator_checked(bpy.ops.object.mode_set, mode="EDIT")
        try:
            for bone_spec in args["bones"]:
                bone = arm_data.edit_bones.new(bone_spec["name"])
                bone.head = bone_spec["head"]
                bone.tail = bone_spec["tail"]
                if bone_spec.get("parent"):
                    bone.parent = arm_data.edit_bones.get(bone_spec["parent"])
                    bone.use_connect = bool(bone_spec.get("use_connect", False))
                if bone_spec.get("radius"):
                    bone.head_radius = bone.tail_radius = bone_spec["radius"]
        finally:
            invoke_operator_checked(bpy.ops.object.mode_set, mode="OBJECT")

    if args.get("collection"):
        compat.move_to_collection(obj, args["collection"])
    return {"armature": obj.name,
            "bones": [b.name for b in obj.data.bones]}


@operation("bone_edit")
def op_bone_edit(args, command):
    arm_obj = _obj(args["armature"])
    if arm_obj.type != "ARMATURE":
        raise ValueError("objeto não é ARMATURE")
    arm_data = arm_obj.data
    compat.select_only(arm_obj)
    with bpy.context.temp_override(**_mode_override(arm_obj)):
        invoke_operator_checked(bpy.ops.object.mode_set, mode="EDIT")
        try:
            if args.get("delete"):
                bone = arm_data.edit_bones.get(args["bone"])
                if bone is None:
                    raise KeyError(f"osso não encontrado: {args['bone']}")
                arm_data.edit_bones.remove(bone)
                return {"armature": arm_obj.name, "bone": args["bone"],
                        "deleted": True}
            bone = arm_data.edit_bones.get(args["bone"])
            if bone is None:
                bone = arm_data.edit_bones.new(args["bone"])
            if args.get("head"):
                bone.head = args["head"]
            if args.get("tail"):
                bone.tail = args["tail"]
            if args.get("roll") is not None:
                bone.roll = args["roll"]
            if args.get("parent"):
                bone.parent = arm_data.edit_bones.get(args["parent"])
            if args.get("use_connect") is not None:
                bone.use_connect = bool(args["use_connect"])
            return {"armature": arm_obj.name, "bone": bone.name,
                    "head": list(bone.head), "tail": list(bone.tail),
                    "roll": bone.roll}
        finally:
            invoke_operator_checked(bpy.ops.object.mode_set, mode="OBJECT")


@operation("rig_assign_weights")
def op_rig_assign_weights(args, command):
    mesh_obj = _obj(args["mesh"])
    arm_obj = _obj(args["armature"])
    if arm_obj.type != "ARMATURE":
        raise ValueError("objeto armature não é ARMATURE")
    mode = args.get("mode", "AUTOMATIC_WEIGHTS")
    parent_type = {"AUTOMATIC_WEIGHTS": "ARMATURE_AUTO",
                   "ENVELOPE_WEIGHTS": "ARMATURE_ENVELOPE"}.get(mode)
    if parent_type is None:
        raise ValueError(f"invalid weight mode: {mode}")
    if mesh_obj.type != "MESH":
        raise ValueError("mesh object must be MESH")
    if args.get("clear_existing"):
        mesh_obj.vertex_groups.clear()
    compat.select_only(mesh_obj)
    arm_obj.select_set(True)
    bpy.context.view_layer.objects.active = arm_obj
    with compat.override_3d():
        invoke_operator_checked(bpy.ops.object.parent_set, type=parent_type)
    return {"mesh": mesh_obj.name, "armature": arm_obj.name,
            "mode": mode,
            "vertex_groups": len(mesh_obj.vertex_groups)}


# ---------------------------------------------------------------------------
# Animação
# ---------------------------------------------------------------------------

@operation("animation_keyframe")
def op_animation_keyframe(args, command):
    obj = _obj(args["object"])
    data_path = args["data_path"]
    frame = args.get("frame", bpy.context.scene.frame_current)
    index = args.get("index", -1)
    if args.get("value") is not None:
        if data_path == "location":
            _set_vec(obj.location, args["value"])
        elif data_path == "scale":
            _set_vec(obj.scale, args["value"])
    if not obj.animation_data:
        obj.animation_data_create()
    ok = obj.keyframe_insert(data_path=data_path, frame=int(frame),
                             index=int(index))
    action = obj.animation_data.action
    return {"object": obj.name, "data_path": data_path,
            "frame": int(frame), "keyframed": bool(ok),
            "action": action.name if action else None}


@operation("animation_action")
def op_animation_action(args, command):
    name = args["action"]
    if args.get("delete"):
        action = bpy.data.actions.get(name)
        if action is None:
            raise KeyError(f"action não encontrada: {name}")
        bpy.data.actions.remove(action)
        return {"action": name, "deleted": True}
    action = bpy.data.actions.get(name)
    created = action is None
    if created:
        action = bpy.data.actions.new(name)
    if args.get("set_fake_user", created):
        action.use_fake_user = True
    target = args.get("assign_to")
    if target:
        obj = _obj(target)
        if not obj.animation_data:
            obj.animation_data_create()
        obj.animation_data.action = action
    return {"action": action.name, "created": created,
            "assigned_to": target}


@operation("animation_fcurve")
def op_animation_fcurve(args, command):
    action = bpy.data.actions.get(args["action"])
    if action is None:
        raise KeyError(f"action não encontrada: {args['action']}")
    operation = args["operation"]
    if operation == "LIST":
        return {"action": action.name, "fcurves": [
            {"data_path": fc.data_path, "index": fc.array_index,
             "keyframes": len(fc.keyframe_points)}
            for fc in action.fcurves]}
    data_path = args.get("data_path")
    index = args.get("index", 0)
    fc = action.fcurves.find(data_path, index=index)
    if operation == "SET_KEYFRAME":
        if fc is None:
            fc = action.fcurves.new(data_path, index=index)
        fc.keyframe_points.add(1)
        kp = fc.keyframe_points[-1]
        kp.co = (float(args.get("frame", 1)), float(args.get("value", 0)))
        return {"action": action.name, "set": [fc.data_path, fc.array_index,
                                               kp.co.x, kp.co.y]}
    if fc is None:
        raise KeyError(f"fcurve não encontrada: {data_path}[{index}]")
    if operation == "SET_INTERPOLATION":
        interp = args.get("interpolation", "BEZIER")
        for kp in fc.keyframe_points:
            kp.interpolation = interp
        return {"action": action.name, "interpolation": interp}
    if operation == "DELETE_KEYFRAME":
        frame = float(args.get("frame"))
        keep = [{"co": tuple(kp.co), "handle_left": tuple(kp.handle_left),
                 "handle_right": tuple(kp.handle_right),
                 "interpolation": kp.interpolation,
                 "handle_left_type": kp.handle_left_type,
                 "handle_right_type": kp.handle_right_type,
                 "easing": kp.easing, "type": kp.type}
                for kp in fc.keyframe_points if kp.co.x != frame]
        removed = len(fc.keyframe_points) - len(keep)
        fc.keyframe_points.clear()
        for saved in keep:
            fc.keyframe_points.add(1)
            kp = fc.keyframe_points[-1]
            for key, value in saved.items():
                setattr(kp, key, value)
        return {"action": action.name, "removed": removed}
    raise ValueError(f"operação fcurve desconhecida: {operation}")


@operation("animation_nla")
def op_animation_nla(args, command):
    obj = _obj(args["object"])
    operation = args["operation"]
    if not obj.animation_data:
        obj.animation_data_create()
    nla = obj.animation_data.nla_tracks
    if operation == "LIST":
        return {"object": obj.name, "tracks": [
            {"track": t.name, "strips": [
                {"name": s.name, "action": s.action.name if s.action else None,
                 "start": s.frame_start, "end": s.frame_end}
                for s in t.strips]}
            for t in nla]}
    if operation == "ADD_STRIP":
        action = bpy.data.actions.get(args["action"])
        if action is None:
            raise KeyError(f"action não encontrada: {args['action']}")
        track_name = args.get("track") or action.name
        track = next((t for t in nla if t.name == track_name), None)
        if track is None:
            track = nla.new()
            track.name = track_name
        start = int(args.get("start", bpy.context.scene.frame_start))
        strip = track.strips.new(action.name, start, action)
        return {"object": obj.name, "track": track.name, "strip": strip.name,
                "start": strip.frame_start, "end": strip.frame_end}
    raise ValueError(f"operação NLA desconhecida: {operation}")


# ---------------------------------------------------------------------------
# Física
# ---------------------------------------------------------------------------

@operation("physics_configure")
def op_physics_configure(args, command):
    obj = _obj(args["object"])
    ptype = args["type"]
    params = args.get("params") or {}
    if args.get("remove"):
        if ptype == "RIGID_BODY":
            compat.select_only(obj)
            if obj.rigid_body:
                with compat.override_3d():
                    invoke_operator_checked(bpy.ops.rigidbody.object_remove, )
            return {"object": obj.name, "removed": obj.rigid_body is None}
        for mod in list(obj.modifiers):
            if mod.type == ptype:
                obj.modifiers.remove(mod)
        return {"object": obj.name, "removed": True}
    if ptype == "RIGID_BODY":
        compat.select_only(obj)
        with compat.override_3d():
            invoke_operator_checked(bpy.ops.rigidbody.object_add, )
        rb = obj.rigid_body
        for key, value in params.items():
            if hasattr(rb, key):
                setattr(rb, key, value)
        return {"object": obj.name, "rigid_body": True}
    mod_type = {"CLOTH": "CLOTH", "SOFT_BODY": "SOFT_BODY",
                "COLLISION": "COLLISION", "FLUID": "FLUID"}.get(ptype)
    if mod_type is None:
        raise ValueError(f"tipo de física desconhecido: {ptype}")
    mod = next((m for m in obj.modifiers if m.type == mod_type), None)
    if mod is None:
        mod = obj.modifiers.new(ptype.title(), mod_type)
    for key, value in params.items():
        if hasattr(mod, key):
            setattr(mod, key, value)
    return {"object": obj.name, "modifier": mod.name, "type": mod_type}


@operation("simulation_bake")
def op_simulation_bake(args, command):
    scene = bpy.context.scene
    scene.frame_start = int(args.get("frame_start", scene.frame_start))
    scene.frame_end = int(args.get("frame_end", scene.frame_end))
    stype = args.get("type", "ALL")
    with compat.override_3d():
        if stype == "FLUID":
            invoke_operator_checked(bpy.ops.fluid.bake_all, )
        else:
            invoke_operator_checked(bpy.ops.ptcache.bake_all, bake=True)
    return {"baked": stype, "frame_start": scene.frame_start,
            "frame_end": scene.frame_end}


# ---------------------------------------------------------------------------
# Câmera / luz
# ---------------------------------------------------------------------------

@operation("camera_configure")
def op_camera_configure(args, command):
    name = args.get("camera")
    cam_obj = _obj(name) if name else bpy.context.scene.camera
    if cam_obj is None:
        raise ValueError("nenhuma câmera (ativo ou por nome) disponível")
    if cam_obj.type != "CAMERA":
        raise ValueError(f"{cam_obj.name} não é câmera")
    cam = cam_obj.data
    if args.get("focal_length"):
        cam.lens = float(args["focal_length"])
    if "fov" in args:
        fov = float(args["fov"])
        if not 0 < fov < math.pi:
            raise ValueError("fov must be between 0 and pi radians")
        cam.lens = cam.sensor_width / (2 * math.tan(fov / 2))
    if args.get("type"):
        cam.type = args["type"]
    if args.get("clip_start"):
        cam.clip_start = float(args["clip_start"])
    if args.get("clip_end"):
        cam.clip_end = float(args["clip_end"])
    if args.get("dof_enable") is not None:
        cam.dof.use_dof = bool(args["dof_enable"])
    if args.get("focus_distance"):
        cam.dof.focus_distance = float(args["focus_distance"])
    if args.get("aperture_fstop"):
        cam.dof.aperture_fstop = float(args["aperture_fstop"])
    bpy.context.scene.camera = cam_obj
    return {"camera": cam_obj.name, "lens": cam.lens, "type": cam.type,
            "active_scene_camera": True}


@operation("light_create")
def op_light_create(args, command):
    ltype = args["type"]
    name = args.get("name") or f"MCP_Light_{ltype.title()}"
    existing = bpy.data.objects.get(name)
    if existing:
        if existing.type != "LIGHT" or existing.data.type != ltype:
            raise ValueError(f"{name} is not a {ltype} light")
        return {"light": name, "reused": True}
    with compat.override_3d():
        invoke_operator_checked(bpy.ops.object.light_add, type=ltype, align="WORLD",
                                 location=tuple(args.get("location", (0, 0, 5))))
    light_obj = bpy.context.active_object
    light_obj.name = name
    if args.get("rotation"):
        _set_vec(light_obj.rotation_euler, args["rotation"])
    if args.get("energy"):
        light_obj.data.energy = float(args["energy"])
    if args.get("color"):
        light_obj.data.color = tuple(args["color"])
    return {"light": light_obj.name, "type": ltype,
            "energy": light_obj.data.energy}


@operation("light_configure")
def op_light_configure(args, command):
    obj = _obj(args["light"])
    if obj.type != "LIGHT":
        raise ValueError(f"{obj.name} não é luz")
    light = obj.data
    if args.get("energy") is not None:
        light.energy = float(args["energy"])
    if args.get("color"):
        light.color = tuple(args["color"])
    if args.get("size") is not None and hasattr(light, "shadow_soft_size"):
        light.shadow_soft_size = float(args["size"])
    if args.get("spot_size") is not None and hasattr(light, "spot_size"):
        light.spot_size = float(args["spot_size"])
    if args.get("spot_blend") is not None and hasattr(light, "spot_blend"):
        light.spot_blend = float(args["spot_blend"])
    if args.get("use_shadow") is not None:
        light.use_shadow = bool(args["use_shadow"])
    return {"light": obj.name, "energy": light.energy}


# ---------------------------------------------------------------------------
# Renderização
# ---------------------------------------------------------------------------

@operation("render_configure")
def op_render_configure(args, command):
    scene = bpy.context.scene
    render = scene.render
    if args.get("engine"):
        render.engine = args["engine"]
    for key in ("resolution_x", "resolution_y", "resolution_percentage"):
        if args.get(key):
            setattr(render, key, int(args[key]))
    if args.get("samples"):
        if render.engine == "CYCLES":
            scene.cycles.samples = int(args["samples"])
        else:
            scene.eevee.taa_render_samples = int(args["samples"])
    if args.get("use_gpu") is not None and render.engine == "CYCLES":
        scene.cycles.device = "GPU" if args["use_gpu"] else "CPU"
    if args.get("file_format"):
        render.image_settings.file_format = args["file_format"]
    if args.get("film_transparent") is not None:
        render.film_transparent = bool(args["film_transparent"])
    if args.get("use_stamp") is not None:
        render.use_stamp = bool(args["use_stamp"])
    if args.get("frame_start") is not None:
        scene.frame_start = int(args["frame_start"])
    if args.get("frame_end") is not None:
        scene.frame_end = int(args["frame_end"])
    if args.get("filepath"):
        render.filepath = args["filepath"]
    return {"engine": render.engine,
            "resolution": [render.resolution_x, render.resolution_y],
            "resolution_percentage": render.resolution_percentage,
            "frame_start": scene.frame_start, "frame_end": scene.frame_end}


def _resolve_render_path(filepath: Optional[str]) -> str:
    if filepath:
        path = resolve_blender_path(filepath, _renders_dir(create=False))
    elif bpy.context.scene.render.filepath:
        path = resolve_blender_path(bpy.context.scene.render.filepath, _renders_dir(create=False))
    else:
        path = _renders_dir(create=False) / f"render_{time.strftime('%Y%m%d_%H%M%S')}.png"
    if not _security().path_allowed(str(path)):
        raise PermissionError(f"render path not authorized: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


@operation("render_preview")
def op_render_preview(args, command):
    return _do_render(args, command, percentage=int(args.get("percentage", 33)))


@operation("render_final")
def op_render_final(args, command):
    return _do_render(args, command, animation=bool(args.get("animation", False)))


def _do_render(args, command, percentage: int = 100, animation: bool = False):
    if PENDING_RENDERS:
        raise RuntimeError("a render command is already active")
    scene = bpy.context.scene
    filepath = _resolve_render_path(args.get("filepath"))
    pending = PendingRender.capture(command.get("command_id", ""), scene, filepath, animation)
    try:
        if scene.camera is None:
            cameras = [o for o in scene.objects if o.type == "CAMERA"]
            if not cameras:
                raise RuntimeError("cena sem câmera — crie/configure antes de renderizar")
            scene.camera = cameras[0]
        scene.render.resolution_percentage = percentage
        scene.render.filepath = filepath
        scene.render.use_file_extension = True
        if animation:
            pending.outputs = tuple(scene.render.frame_path(frame=frame) for frame in
                                    range(scene.frame_start, scene.frame_end + 1, scene.frame_step))
        else:
            extension = scene.render.file_extension
            if extension and not filepath.lower().endswith(extension.lower()):
                filepath += extension
            pending.filepath = filepath
            pending.outputs = (filepath,)
        if bpy.app.background:
            with compat.override_3d():
                invoke_operator_checked(bpy.ops.render.render, animation=animation, write_still=not animation)
            missing = pending.missing_outputs()
            if missing:
                raise RuntimeError(f"RenderOutputMissing: {missing}")
            return {"rendered": True, "filepath": pending.filepath, "mode": "background",
                    "animation": animation}
        PENDING_RENDERS[pending.command_id] = pending
        with compat.override_3d():
            invoke_operator_checked(bpy.ops.render.render, "INVOKE_DEFAULT", animation=animation,
                                    write_still=not animation)
        return {"async": True, "command_id": pending.command_id,
                "filepath": pending.filepath, "mode": "interface"}
    except BaseException:
        PENDING_RENDERS.pop(pending.command_id, None)
        pending.restore()
        raise
    finally:
        if bpy.app.background:
            pending.restore()


def complete_render(kind: str, queue: CommandQueue, scene=None) -> None:
    """Chamado pelos handlers render_complete/render_cancel do addon."""
    for cid, info in list(PENDING_RENDERS.items())[:1]:
        if scene is not None and info.scene != scene:
            return
        PENDING_RENDERS.pop(cid, None)
        missing = info.missing_outputs() if kind == "complete" else []
        ok = kind == "complete" and not missing
        error = None if ok else {
            "type": "RenderOutputMissing" if missing else "RenderCancelled",
            "message": f"render outputs missing: {missing}" if missing else f"render ended via {kind}"}
        info.restore()
        queue.push_result({
            "command_id": cid,
            "status": "completed" if ok else "failed",
            "success": ok,
            "data": {"filepath": info.filepath, "animation": info.animation,
                     "render_event": kind,
                     "duration_s": round(time.time() - info.started, 1)},
            "scene_revision": _revision(),
            "error": error,
            "final": True,
        })


# ---------------------------------------------------------------------------
# Arquivos / projeto
# ---------------------------------------------------------------------------

@operation("project_open")
def op_project_open(args, command):
    path = str(resolve_blender_path(args["path"]))
    sec = _security()
    if not sec.path_allowed(path):
        raise PermissionError(f"caminho não autorizado: {path}")
    invoke_operator_checked(bpy.ops.wm.open_mainfile, filepath=path)
    return {"opened": path}


@operation("project_save")
def op_project_save(args, command):
    pack = bool(args.get("pack", False))
    raw = args.get("path") or bpy.data.filepath
    target = resolve_blender_path(raw) if raw else (
        storage_root(get_prefs()) / "projects" / f"untitled_{time.strftime('%Y%m%d_%H%M%S')}.blend")
    if args.get("incremental") and target.exists():
        stem, index = target.stem, 1
        while target.with_name(f"{stem}.{index:03d}.blend").exists():
            index += 1
        target = target.with_name(f"{stem}.{index:03d}.blend")
    if not _security().path_allowed(str(target)):
        raise PermissionError(f"save path not authorized: {target}")
    # No packing, directories or save operator before the actual target passes.
    if pack:
        invoke_operator_checked(bpy.ops.file.pack_all)
    target.parent.mkdir(parents=True, exist_ok=True)
    invoke_operator_checked(bpy.ops.wm.save_as_mainfile, filepath=str(target))
    return {"saved": str(target), "packed": pack}


@operation("project_checkpoint")
def op_project_checkpoint(args, command):
    out = snapshot_manager.create_checkpoint(label=args.get("label", ""),
                                             directory=args.get("directory"))
    out["checkpoints_dir"] = str(snapshot_manager.checkpoints_root(
        args.get("directory")))
    return out


# ---------------------------------------------------------------------------
# Import / export / addons
# ---------------------------------------------------------------------------

_IMPORTERS = {
    "OBJ": ["wm.obj_import", "import_scene.obj"],
    "FBX": ["import_scene.fbx"],
    "GLTF": ["import_scene.gltf"], "GLB": ["import_scene.gltf"],
    "STL": ["wm.stl_import", "import_mesh.stl"],
    "PLY": ["wm.ply_import", "import_mesh.ply"],
    "ALEMBIC": ["wm.alembic_import"],
    "USD": ["wm.usd_import"], "USDZ": ["wm.usd_import"],
    "DAE": ["import_scene.dae"], "X3D": ["import_scene.x3d"],
    "SVG": ["import_curve.svg"], "SVG_PATH": ["import_curve.svg"],
    "BLEND": ["wm.link"],
}
_EXPORTERS = {
    "OBJ": ["wm.obj_export", "export_scene.obj"],
    "FBX": ["export_scene.fbx"],
    "GLTF": ["export_scene.gltf"], "GLB": ["export_scene.gltf"],
    "STL": ["wm.stl_export", "export_mesh.stl"],
    "PLY": ["wm.ply_export", "export_mesh.ply"],
    "ALEMBIC": ["wm.alembic_export"],
    "USD": ["wm.usd_export"], "DAE": ["export_scene.dae"],
    "X3D": ["export_scene.x3d"], "BLEND": ["wm.save_as_mainfile"],
}


def _format_of(path: str, fmt: Optional[str]) -> str:
    if fmt and fmt != "AUTO":
        return fmt
    return Path(path).suffix.lstrip(".").upper()


@operation("asset_import")
def op_asset_import(args, command):
    path = str(resolve_blender_path(args["path"]))
    if not _security().path_allowed(path):
        raise PermissionError(f"caminho não autorizado: {path}")
    fmt = _format_of(path, args.get("format"))
    if fmt == "BLEND":
        invoke_operator_checked(bpy.ops.wm.open_mainfile, filepath=path)
        return {"imported": path, "format": "BLEND", "mode": "open"}
    candidates = _IMPORTERS.get(fmt)
    if not candidates:
        raise ValueError(f"formato de importação não suportado: {fmt}")
    op = _find_operator(candidates)
    before = set(o.name for o in bpy.context.scene.objects)
    with compat.override_3d():
        invoke_operator_checked(op, filepath=path, **_filtered_props(op, args.get("params")))
    new_objects = [o.name for o in bpy.context.scene.objects if o.name not in before]
    return {"imported": path, "format": fmt, "new_objects": new_objects}


@operation("asset_export")
def op_asset_export(args, command):
    path = str(resolve_blender_path(args["path"]))
    if not _security().path_allowed(path):
        raise PermissionError(f"caminho não autorizado: {path}")
    fmt = _format_of(path, args.get("format"))
    objects = args.get("objects") or [obj.name for obj in bpy.context.selected_objects]
    targets = [_obj(name) for name in objects]
    if not targets:
        raise ValueError("export requires at least one requested or selected object")
    if fmt == "BLEND":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        bpy.data.libraries.write(path, set(targets))
        return {"exported": path, "format": "BLEND"}
    candidates = _EXPORTERS.get(fmt)
    if not candidates:
        raise ValueError(f"formato de exportação não suportado: {fmt}")
    if objects:
        for obj in bpy.context.view_layer.objects:
            obj.select_set(False)
        for obj in targets:
            obj.select_set(True)
        bpy.context.view_layer.objects.active = targets[0]
    op = _find_operator(candidates)
    params = dict(args.get("params") or {})
    selection_keys = {"use_selection", "export_selected_objects", "selected", "use_selected", "selected_objects_only"}
    supported = selection_keys.intersection(compat.operator_properties(op))
    if objects and not supported:
        raise ValueError(f"{fmt} exporter does not support exact selection")
    for key in supported:
        params[key] = bool(objects)
    params["filepath"] = path
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with compat.override_3d():
        invoke_operator_checked(op, **_filtered_props(op, params))
    return {"exported": path, "format": fmt,
            "objects": objects or "selecionados"}


@operation("addon_operator_invoke")
def op_addon_operator_invoke(args, command):
    mode = args["mode"]
    if mode == "LIST_ADDONS":
        addons = list(bpy.context.preferences.addons.keys())
        return {"addons": sorted(addons)}
    if mode == "LIST_OPERATORS":
        flt = (args.get("filter") or "").lower()
        ops = []
        for cls in bpy.types.Operator.__subclasses__():
            idname = getattr(cls, "bl_idname", "")
            if flt and not idname.lower().startswith(flt):
                continue
            ops.append(idname)
        return {"operators": sorted(set(ops))[:300]}
    if mode == "OPERATOR_INFO":
        return _operator_info(args["operator"])
    if mode == "INVOKE":
        return _operator_invoke(args["operator"], args.get("props") or {})
    raise ValueError(f"modo desconhecido: {mode}")


def _resolve_op(idname: str):
    if not re.fullmatch(r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*", idname):
        raise ValueError("operator path must be namespace.name")
    if idname in {"wm.quit_blender", "wm.read_factory_settings", "wm.read_homefile"}:
        raise PermissionError("process/session lifecycle operators are denied")
    owner, _, attr = idname.partition(".")
    namespace = getattr(bpy.ops, owner, None)
    if namespace is None:
        raise KeyError(f"namespace de operador desconhecido: {owner}")
    op = getattr(namespace, attr.replace(".", "_"), None)
    if op is None:
        raise KeyError(f"operador não encontrado: {idname}")
    return op


def _operator_info(idname: str) -> Dict[str, Any]:
    op = _resolve_op(idname)
    props = []
    for p in op.get_rna_type().properties:
        if p.identifier == "rna_type":
            continue
        props.append({"name": p.identifier, "type": p.type,
                      "description": p.description,
                      "default": to_jsonable(getattr(p, "default", None))})
    return {"operator": idname, "properties": props}


def _operator_invoke(idname: str, props: Dict[str, Any]) -> Dict[str, Any]:
    op = _resolve_op(idname)
    unknown = set(props) - compat.operator_properties(op)
    if unknown:
        raise ValueError(f"unknown operator properties: {sorted(unknown)}")
    kwargs = _filtered_props(op, props)
    with compat.override_3d():
        result = invoke_operator_checked(op, **kwargs)
    return {"operator": idname, "result": str(result),
            "props": to_jsonable(kwargs)}


# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------

@operation("python_execute_blender")
def op_python_execute_blender(args, command):
    from . import python_executor
    out = python_executor.execute_code(args["code"], _security())
    if not out.get("success"):
        raise _python_error(out)
    return out


class _python_error(RuntimeError):
    def __init__(self, output: Dict[str, Any]):
        error = output["error"]
        super().__init__(error.get("message", "erro em python_execute"))
        self.error = error
        self.output = output


def validate_operation(op_name, args):
    validate_arguments(op_name, args)
    _validate_argument_paths(args, op_name)
    # Lookup only: dry-run must check the same object preconditions without
    # invoking operators, allocating datablocks, or touching scene state.
    fields = {
        "object_transform": ("name",), "object_duplicate": ("name",),
        "object_rename": ("name",), "object_get_info": ("name",),
        "object_compare": ("a", "b"), "mesh_boolean": ("object", "operand"),
        "rig_assign_weights": ("mesh", "armature"),
    }.get(op_name, ("object",) if "object" in args else ())
    for key in fields:
        _obj(args[key])
    if op_name == "object_create" and args.get("name") in bpy.data.objects:
        existing = _obj(args["name"])
        expected = {"EMPTY": "EMPTY", "ARMATURE": "ARMATURE", "CAMERA": "CAMERA"}.get(args["type"], "MESH")
        if existing.type != expected:
            raise ValueError(f"{existing.name} is {existing.type}, expected {expected}")
    if op_name == "light_create" and args.get("name") in bpy.data.objects:
        existing = _obj(args["name"])
        if existing.type != "LIGHT" or existing.data.type != args["type"]:
            raise ValueError("existing object is not the requested light type")
    if op_name == "object_transform" and "rotation" in args:
        mode = args.get("rotation_mode", _obj(args["name"]).rotation_mode)
        lengths = (3, 4) if mode == "QUATERNION" else (4,) if mode == "AXIS_ANGLE" else (3,)
        if len(args["rotation"]) not in lengths:
            raise ValueError(f"rotation for {mode} requires {lengths} components")
    if op_name == "object_rename" and args["new_name"] in bpy.data.objects:
        raise ValueError("new object name already exists")
    if op_name == "camera_configure":
        cam = _obj(args["camera"]) if args.get("camera") else bpy.context.scene.camera
        if cam is None or cam.type != "CAMERA":
            raise ValueError("a CAMERA object is required")
        if "fov" in args and not 0 < args["fov"] < math.pi:
            raise ValueError("fov must be between 0 and pi radians")
    if op_name == "rig_assign_weights":
        if _obj(args["mesh"]).type != "MESH" or _obj(args["armature"]).type != "ARMATURE":
            raise ValueError("MESH and ARMATURE objects are required")
    if op_name == "python_execute_blender":
        sec = _security()
        findings = sec.scan_python_source(args["code"]) if sec.strict_python else []
        if findings:
            raise _python_error({"success": False, "stdout": "", "stderr": "", "result": None,
                                 "error": {"type": "PolicyBlocked", "message": "source policy blocked",
                                           "findings": findings}})
        compile(args["code"], "<mcp_execute>", "exec")
    if op_name == "addon_operator_invoke" and args["mode"] in ("INVOKE", "OPERATOR_INFO"):
        op = _resolve_op(args.get("operator", ""))
        if args["mode"] == "INVOKE":
            if not op.poll():
                raise RuntimeError("operator poll failed")
            if set(args.get("props") or {}) - compat.operator_properties(op):
                raise ValueError("unknown operator property")
    if op_name in ("render_preview", "render_final"):
        if PENDING_RENDERS:
            raise RuntimeError("a render command is already active")
        if not bpy.context.scene.camera and not any(o.type == "CAMERA" for o in bpy.context.scene.objects):
            raise ValueError("scene requires a camera")
    if op_name in ("mesh_edit", "mesh_apply_modifier", "mesh_boolean", "mesh_validate", "uv_unwrap"):
        obj = _obj(args["object"])
        if obj.type != "MESH" or obj.mode != "OBJECT":
            raise ValueError("mesh operations require a MESH in OBJECT mode")
    if op_name == "object_delete":
        names = list(args.get("names") or []) + ([args["name"]] if args.get("name") else [])
        if not names:
            raise ValueError("name or names required")
        for name in names:
            _obj(name)


def _validate_argument_paths(args, operation):
    sec = _security()
    def walk(value, key="", directory=None):
        if isinstance(value, dict):
            folder = value.get("directory", directory)
            for child, val in value.items():
                if child == "files" and isinstance(val, list) and folder:
                    for item in val:
                        if isinstance(item, dict) and isinstance(item.get("name"), str):
                            walk(str(Path(folder) / item["name"]), "path")
                walk(val, child, folder)
        elif isinstance(value, list):
            for item in value:
                walk(item, key, directory)
        elif isinstance(value, str) and value and key in ("path", "filepath", "directory", "filename"):
            path = resolve_blender_path(value)
            if operation in ("render_preview", "render_final", "render_configure") and not Path(value).is_absolute() and not value.startswith("//"):
                path = resolve_blender_path(value, _renders_dir(create=False))
            if not sec.path_allowed(str(path)):
                raise PermissionError(f"path not authorized: {path}")
    walk(args)


# ---------------------------------------------------------------------------
# Loop de execução (timer da thread principal)
# ---------------------------------------------------------------------------

def execute_command(command: Dict[str, Any],
                    queue: Optional[CommandQueue] = None) -> Dict[str, Any]:
    """Executa um comando e devolve o envelope §6.4."""
    global _ACTIVE_QUEUE
    if queue is not None:
        _ACTIVE_QUEUE = queue
    cid = command.get("command_id", "")
    op_name = command.get("operation", "")
    args = command.get("arguments", {})
    started = time.time()

    def envelope(status: str, success: bool, data=None, error=None):
        rev = _revision()
        return {"command_id": cid, "status": status, "success": success,
                "data": data, "scene_revision": rev, "error": error,
                "final": True, "duration_s": round(time.time() - started, 3)}

    try:
        sec = _security()
        allowed, reason = sec.check_operation(op_name, args, command.get("authorization"))
        if not allowed:
            return envelope("failed", False,
                            error={"type": "PolicyDenied", "message": reason})
        fn = _OPERATIONS.get(op_name)
        if fn is None:
            return envelope("failed", False, error={
                "type": "UnknownOperation",
                "message": f"operação não implementada no addon: {op_name}",
                "available": operation_names()[:80]})
        args = normalize_argument_paths(args, _renders_dir(create=False) if op_name in (
            "render_preview", "render_final", "render_configure") else None)
        validate_operation(op_name, args)
        if command.get("dry_run"):
            return envelope("completed", True, {
                "dry_run": True, "operation": op_name,
                "note": "validação de argumentos apenas"})
        checkpoint = _maybe_checkpoint(command)
        with preserve_selection():
            data = fn(args, command)
        if (op_name in MUTATING_OPERATIONS and not (
                op_name == "animation_fcurve" and args.get("operation") == "LIST" or
                op_name == "animation_nla" and args.get("operation") == "LIST" or
                op_name in ("object_create", "light_create") and data.get("reused") or
                op_name == "addon_operator_invoke" and args.get("mode") != "INVOKE")
                or op_name == "scene_validate" and args.get("fix") and data.get("fixed")):
            _bump_revision()
        if checkpoint:
            if isinstance(data, dict):
                data["checkpoint_before"] = checkpoint
        # async render: comando continua 'running' até o handler finalizar
        if isinstance(data, dict) and data.get("async"):
            out = envelope("running", True, data)
            out["final"] = False
            return out
        return envelope("completed", True, data)
    except _python_error as exc:
        return envelope("failed", False, data=exc.output, error=exc.error)
    except KeyError as exc:
        return envelope("failed", False, error={"type": "NotFound",
                                                "message": str(exc).strip("'")})
    except (ValueError, RuntimeError, NotImplementedError, PermissionError,
            OSError) as exc:
        return envelope("failed", False, error={"type": type(exc).__name__,
                                                "message": str(exc)})
    except Exception as exc:  # noqa: BLE001 — última linha de defesa
        return envelope("failed", False, error={
            "type": type(exc).__name__, "message": str(exc),
            "traceback": traceback.format_exc()[-2000:]})


def tick(queue: CommandQueue, max_items: int = 2) -> float:
    """Um passo do timer: executa comandos pendentes e devolve resultados."""
    global _ACTIVE_QUEUE
    _ACTIVE_QUEUE = queue
    for command in queue.pop_commands(max_items):
        envelope = execute_command(command)
        queue.push_result(envelope)  # 'running' (render async) também vai
    return 0.25


# ---------------------------------------------------------------------------
# Handlers de render assíncrono (modo interface) — completam comandos pendentes
# ---------------------------------------------------------------------------

_ACTIVE_QUEUE: Optional[CommandQueue] = None


@bpy.app.handlers.persistent
def _on_render_complete(scene):
    if _ACTIVE_QUEUE is not None:
        complete_render("complete", _ACTIVE_QUEUE, scene)


@bpy.app.handlers.persistent
def _on_render_cancel(scene):
    if _ACTIVE_QUEUE is not None:
        complete_render("cancel", _ACTIVE_QUEUE, scene)


def load_post_register() -> None:
    handlers = bpy.app.handlers
    for lst, fn in ((handlers.render_complete, _on_render_complete),
                    (handlers.render_cancel, _on_render_cancel)):
        if fn not in lst:
            lst.append(fn)


def load_post_unregister() -> None:
    if _ACTIVE_QUEUE is not None:
        complete_render("cancel", _ACTIVE_QUEUE)
    else:
        for pending in list(PENDING_RENDERS.values()):
            pending.restore()
        PENDING_RENDERS.clear()
    handlers = bpy.app.handlers
    for lst, fn in ((handlers.render_complete, _on_render_complete),
                    (handlers.render_cancel, _on_render_cancel)):
        if fn in lst:
            lst.remove(fn)
