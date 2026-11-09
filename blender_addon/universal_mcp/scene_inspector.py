# SPDX-License-Identifier: MIT
"""Inspeção e validação estrutural da cena (operações somente-leitura)."""
from __future__ import annotations

import os
import hashlib
import json
from typing import Any, Dict, List

import bpy

from .scene_serializer import (material_summary, mesh_stats, modifier_summary,
                               object_transforms, to_jsonable)

ALL_CHECKS = ("invalid_meshes", "duplicate_names", "broken_modifiers",
              "missing_images", "missing_materials", "orphan_actions")


def scene_info(include_collections: bool = False) -> Dict[str, Any]:
    scene = bpy.context.scene
    counts: Dict[str, int] = {}
    for obj in scene.objects:
        counts[obj.type] = counts.get(obj.type, 0) + 1
    out = {
        "scene_name": scene.name,
        "blender_version": bpy.app.version_string,
        "frame_start": scene.frame_start,
        "frame_end": scene.frame_end,
        "fps": scene.render.fps,
        "active_camera": scene.camera.name if scene.camera else None,
        "filepath": bpy.data.filepath,
        "is_saved": bool(bpy.data.filepath),
        "object_count": len(scene.objects),
        "objects_by_type": counts,
        "materials": len(bpy.data.materials),
        "actions": len(bpy.data.actions),
        "unit_system": scene.unit_settings.system,
    }
    if include_collections:
        out["collections"] = [
            {"name": c.name, "objects": [o.name for o in c.objects]}
            for c in _scene_collections(scene)
        ]
    return out


def _scene_collections(scene):
    seen = set()

    def walk(coll):
        for child in coll.children:
            if child.name not in seen:
                seen.add(child.name)
                yield child
                yield from walk(child)

    yield scene.collection
    yield from walk(scene.collection)


def list_objects(obj_type: str = None, collection: str = None,
                 limit: int = 200) -> List[Dict[str, Any]]:
    objects = bpy.context.scene.objects
    if collection and collection in bpy.data.collections:
        objects = bpy.data.collections[collection].objects
    out = []
    for obj in objects:
        if obj_type and obj.type != obj_type:
            continue
        entry: Dict[str, Any] = {
            "name": obj.name, "type": obj.type,
            "parent": obj.parent.name if obj.parent else None,
            "materials": [m.name for m in obj.material_slots if m.material]
            if hasattr(obj, "material_slots") else [],
        }
        try:
            entry["visible"] = obj.visible_get()
        except RuntimeError:
            entry["visible"] = None
        if obj.type == "MESH" and obj.data:
            entry["mesh"] = mesh_stats(obj.data)
        out.append(entry)
        if limit and len(out) >= limit:
            break
    return out


def object_info(name: str, include_modifiers: bool = True,
                include_materials: bool = True) -> Dict[str, Any]:
    obj = bpy.data.objects.get(name)
    if obj is None:
        raise KeyError(f"objeto não encontrado: {name}")
    out: Dict[str, Any] = {
        "name": obj.name,
        "type": obj.type,
        "transforms": object_transforms(obj),
        "matrix_world": [[round(c, 6) for c in row] for row in obj.matrix_world],
        "parent": obj.parent.name if obj.parent else None,
        "parent_type": obj.parent_type,
        "collections": [c.name for c in obj.users_collection],
        "hide_viewport": obj.hide_viewport,
        "hide_render": obj.hide_render,
        "select_get": obj.select_get(),
    }
    if obj.type == "MESH" and obj.data:
        out["mesh"] = mesh_stats(obj.data)
        out["vertex_groups"] = [g.name for g in obj.vertex_groups]
    if hasattr(obj, "material_slots"):
        out["material_slots"] = [
            {"slot": i, "material": slot.material.name if slot.material else None}
            for i, slot in enumerate(obj.material_slots)
        ]
    if include_modifiers:
        out["modifiers"] = [modifier_summary(m) for m in obj.modifiers]
        if obj.type == "ARMATURE":
            out["bones"] = [b.name for b in obj.data.bones]
    if not include_modifiers:
        out.pop("modifiers", None)
    if not include_materials:
        out.pop("material_slots", None)
        out.pop("materials", None)
    if obj.type == "CAMERA":
        out["camera"] = {"lens": obj.data.lens, "type": obj.data.type}
    if obj.type == "LIGHT":
        out["light"] = {"type": obj.data.type, "energy": obj.data.energy}
    if obj.animation_data and obj.animation_data.action:
        out["action"] = obj.animation_data.action.name
    return out


def selection() -> Dict[str, Any]:
    selected = [{"name": o.name, "type": o.type}
                for o in bpy.context.selected_objects]
    active = bpy.context.view_layer.objects.active
    return {"selected": selected, "active": active.name if active else None,
            "count": len(selected)}


def validate_scene(checks: List[str] = None, fix: bool = False) -> Dict[str, Any]:
    staged = {"meshes": {}, "actions": []}
    committed = False
    try:
        result = _inspect_scene(checks, fix, staged)
        if fix:
            warnings = _commit_repairs(staged)
            if warnings:
                result["cleanup_warnings"] = warnings
        committed = True
        return result
    finally:
        if not committed:
            for copy in staged["meshes"].values():
                bpy.data.meshes.remove(copy)


def _commit_repairs(staged):
    """Keep original meshes and full Action copies until every repair commits."""
    meshes = staged["meshes"]
    mesh_names = {mesh: mesh.name for mesh in meshes}
    backups = {}
    try:
        for name in staged["actions"]:
            action = bpy.data.actions.get(name)
            if action is not None:
                backups[name] = action.copy()
        for original, repaired in meshes.items():
            original.user_remap(repaired)
        if backups:
            bpy.data.batch_remove(ids=[bpy.data.actions[name] for name in backups])
    except BaseException:
        # Original mesh IDs have not been destroyed: restore all users and
        # their exact original geometry/custom data, even after partial commit.
        for original, repaired in meshes.items():
            repaired.user_remap(original)
        for name, backup in backups.items():
            if bpy.data.actions.get(name) is None:
                backup.name = name
            else:
                bpy.data.actions.remove(backup)
        raise
    warnings = []
    # Repairs are committed. Cleanup failure must not report a failed command
    # after committing a scene change; retain the orphan backup and diagnose it.
    for backup in backups.values():
        try:
            bpy.data.actions.remove(backup)
        except Exception as exc:
            warnings.append(str(exc))
    for original, repaired in meshes.items():
        try:
            name = mesh_names[original]
            bpy.data.meshes.remove(original)
            repaired.name = name
        except Exception as exc:
            warnings.append(str(exc))
    return warnings


def _inspect_scene(checks, fix, staged):
    checks = list(ALL_CHECKS) if checks is None else list(checks)
    if any(check not in ALL_CHECKS for check in checks):
        raise ValueError("unknown scene validation check")
    issues: List[Dict[str, Any]] = []
    fixed: List[str] = []

    if "invalid_meshes" in checks:
        for obj in bpy.context.scene.objects:
            if obj.type == "MESH" and obj.data:
                if obj.data in staged["meshes"]:
                    had_errors = True
                else:
                    copy = obj.data.copy()
                    try:
                        had_errors = copy.validate(verbose=False)
                    except BaseException:
                        bpy.data.meshes.remove(copy)
                        raise
                    if had_errors and fix:
                        staged["meshes"][obj.data] = copy
                    else:
                        bpy.data.meshes.remove(copy)
                if had_errors:
                    issues.append({"check": "invalid_meshes", "object": obj.name,
                                   "detail": "malha inválida detectada"})
                    if fix:
                        fixed.append(f"mesh.validate:{obj.name}")

    if "duplicate_names" in checks:
        seen: Dict[str, int] = {}
        for obj in bpy.data.objects:
            seen[obj.name] = seen.get(obj.name, 0) + 1
        for name, n in seen.items():
            if n > 1:
                issues.append({"check": "duplicate_names", "object": name,
                               "detail": f"{n} objetos com o mesmo nome"})

    if "broken_modifiers" in checks:
        for obj in bpy.context.scene.objects:
            for mod in obj.modifiers:
                if mod.type == "BOOLEAN" and mod.object is None:
                    issues.append({"check": "broken_modifiers",
                                   "object": obj.name, "modifier": mod.name,
                                   "detail": "boolean sem objeto operando"})

    if "missing_images" in checks:
        for img in bpy.data.images:
            if img.source == "FILE" and img.filepath:
                abspath = bpy.path.abspath(img.filepath)
                if not os.path.exists(abspath):
                    issues.append({"check": "missing_images", "image": img.name,
                                   "detail": f"arquivo ausente: {abspath}"})

    if "missing_materials" in checks:
        for obj in bpy.context.scene.objects:
            if obj.type == "MESH" and obj.data:
                slots = len(obj.material_slots)
                for poly in obj.data.polygons:
                    if poly.material_index >= slots:
                        issues.append({"check": "missing_materials",
                                       "object": obj.name,
                                       "detail": f"face com material_index "
                                                 f"{poly.material_index} sem slot"})
                        break

    if "orphan_actions" in checks:
        orphans = [a.name for a in bpy.data.actions if a.users == 0]
        for name in orphans:
            issues.append({"check": "orphan_actions", "action": name,
                           "detail": "action sem usuários"})
        if fix and orphans:
            staged["actions"] = orphans
            fixed.extend(f"orphan_action:{name}" for name in orphans)

    return {"checks": checks, "issues": issues, "issue_count": len(issues),
            "fix": fix, "fixed": fixed, "valid": len(issues) == 0}


def compare_objects(a: str, b: str, check: List[str] = None) -> Dict[str, Any]:
    check = ["transform", "geometry", "materials", "modifiers"] if check is None else check
    obj_a = bpy.data.objects.get(a)
    obj_b = bpy.data.objects.get(b)
    if obj_a is None:
        raise KeyError(f"objeto não encontrado: {a}")
    if obj_b is None:
        raise KeyError(f"objeto não encontrado: {b}")
    diff: Dict[str, Any] = {}

    if "transform" in check:
        ta, tb = object_transforms(obj_a), object_transforms(obj_b)
        tdiff = {}
        for key in ("location", "scale", "rotation_mode", "rotation_euler",
                    "rotation_quaternion", "rotation_axis_angle"):
            if ta[key] != tb[key]:
                tdiff[key] = {"a": ta[key], "b": tb[key]}
        diff["transform"] = tdiff or "idêntico"

    if "geometry" in check:
        ga = geometry_fingerprint(obj_a.data) if obj_a.type == "MESH" else None
        gb = geometry_fingerprint(obj_b.data) if obj_b.type == "MESH" else None
        diff["geometry"] = {"a": ga, "b": gb,
                            "same": ga == gb}

    if "materials" in check:
        ma = [s.material.name if s.material else None
              for s in getattr(obj_a, "material_slots", [])]
        mb = [s.material.name if s.material else None
              for s in getattr(obj_b, "material_slots", [])]
        diff["materials"] = {"a": ma, "b": mb, "same": ma == mb}

    if "modifiers" in check:
        mod_a = [_modifier_settings(m) for m in obj_a.modifiers]
        mod_b = [_modifier_settings(m) for m in obj_b.modifiers]
        diff["modifiers"] = {"a": mod_a, "b": mod_b, "same": mod_a == mod_b}

    diff["equal"] = all(
        (v == "idêntico") or (isinstance(v, dict) and v.get("same") is True)
        for v in diff.values()
    )
    return to_jsonable(diff)


def geometry_fingerprint(mesh):
    payload = {"vertices": [list(v.co) for v in mesh.vertices],
               "edges": [list(e.vertices) for e in mesh.edges],
               "polygons": [{"vertices": list(p.vertices), "material": p.material_index,
                             "smooth": p.use_smooth} for p in mesh.polygons]}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {**mesh_stats(mesh), "sha256": hashlib.sha256(encoded).hexdigest()}


def _modifier_settings(mod):
    out = {}
    for prop in mod.bl_rna.properties:
        if prop.identifier == "rna_type" or prop.type == "COLLECTION":
            continue
        try:
            value = getattr(mod, prop.identifier)
            if getattr(prop, "is_array", False):
                value = list(value)
            out[prop.identifier] = to_jsonable(value)
        except (AttributeError, TypeError, ValueError):
            continue
    return out
