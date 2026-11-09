"""Fix Round 1 regressions, executed inside Blender 3.6/Python 3.10."""
import json
import os
from pathlib import Path
import sys
import tempfile
import traceback
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
from phase5_regressions import bpy, d, mesh_fingerprint, run, ok
from universal_mcp import scene_inspector as inspector
from universal_mcp import snapshot_manager
from universal_mcp.security import AddonSecurity


def invalid_mesh(name):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)],
                     [(0, 1), (0, 1)], [(0, 1, 2)])
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def test_repair_rolls_back_when_later_validation_fails():
    obj = invalid_mesh("RollbackValidation")
    original = obj.data
    before = mesh_fingerprint(original)
    meshes = set(bpy.data.meshes.keys())
    revision = d._revision()
    image = bpy.data.images.new("MissingImage", width=1, height=1)
    image.source, image.filepath = "FILE", "missing.png"
    exists = inspector.os.path.exists
    def fail(path):
        raise RuntimeError("injected later image inspection failure")
    inspector.os.path.exists = fail
    try:
        out = run("scene_validate", {"checks": ["invalid_meshes", "missing_images"], "fix": True})
    finally:
        inspector.os.path.exists = exists
        bpy.data.images.remove(image)
    assert not out["success"], out
    assert obj.data is original and mesh_fingerprint(original) == before
    assert set(bpy.data.meshes.keys()) == meshes and d._revision() == revision


def action_state(action):
    return {"name": action.name, "custom": dict(action.items()),
            "curves": [(fc.data_path, fc.array_index,
                        [(tuple(k.co), tuple(k.handle_left), tuple(k.handle_right), k.interpolation)
                         for k in fc.keyframe_points]) for fc in action.fcurves]}


def test_repair_rolls_back_meshes_and_actions_after_partial_action_deletion():
    obj = invalid_mesh("RollbackCommit")
    original = obj.data
    before = mesh_fingerprint(original)
    meshes = set(bpy.data.meshes.keys())
    for name in ("RollbackActionA", "RollbackActionB"):
        action = bpy.data.actions.new(name)
        action["sentinel"] = name
        action.fcurves.new("location", index=1).keyframe_points.insert(2, 3)
    actions = {a.name: action_state(a) for a in bpy.data.actions}
    revision = d._revision()
    real_bpy = inspector.bpy
    class Actions:
        def __iter__(self):
            return iter(bpy.data.actions)
        def __getitem__(self, key):
            return bpy.data.actions[key]
        def __getattr__(self, key):
            return getattr(bpy.data.actions, key)
        def remove(self, action):
            data.fail_once([action])
            return bpy.data.actions.remove(action)
    class Data:
        failed = False
        actions = Actions()
        def __getattr__(self, key):
            return getattr(bpy.data, key)
        def fail_once(self, ids):
            candidates = [a for a in ids if isinstance(a, bpy.types.Action) and a.name in actions]
            if candidates and not self.failed:
                self.failed = True
                bpy.data.batch_remove(ids=candidates[:1])
                raise RuntimeError("injected failure after first action deletion")
        def batch_remove(self, ids):
            ids = list(ids)
            self.fail_once(ids)
            return bpy.data.batch_remove(ids=ids)
    data = Data()
    inspector.bpy = SimpleNamespace(data=data, context=bpy.context, path=bpy.path)
    try:
        out = run("scene_validate", {"checks": ["invalid_meshes", "orphan_actions"], "fix": True})
    finally:
        inspector.bpy = real_bpy
    assert not out["success"] and data.failed, out
    assert obj.data is original and mesh_fingerprint(original) == before
    assert set(bpy.data.meshes.keys()) == meshes
    assert {a.name: action_state(a) for a in bpy.data.actions} == actions
    assert d._revision() == revision


def test_export_omitted_objects_means_current_selection():
    root = Path(tempfile.mkdtemp(prefix="phase5_export_"))
    ok("object_create", {"type": "CUBE", "name": "SelectedOnly"})
    ok("object_create", {"type": "CUBE", "name": "NotSelected"})
    d.compat.select_only(bpy.data.objects["SelectedOnly"])
    for extra in ({}, {"objects": []}):
        path = root / ("omitted.obj" if not extra else "empty.obj")
        ok("asset_export", {"path": str(path), "format": "OBJ", **extra})
        objects = [line for line in path.read_text().splitlines() if line.startswith("o ")]
        assert len(objects) == 1 and "SelectedOnly" in objects[0], objects
    path = root / "selected.blend"
    ok("asset_export", {"path": str(path), "format": "BLEND"})
    with bpy.data.libraries.load(str(path)) as (source, target):
        assert source.objects == ["SelectedOnly"], source.objects
    for obj in bpy.context.selected_objects:
        obj.select_set(False)
    path = root / "no_selection.blend"
    assert not run("asset_export", {"path": str(path), "format": "BLEND"})["success"]
    assert not path.exists()


def test_pack_authorization_precedes_every_operator():
    root = Path(tempfile.mkdtemp(prefix="phase5_pack_"))
    original = root / "original.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(original))
    sec, checked = d._security, d.invoke_operator_checked
    calls = []
    def record(op, *args, **kwargs):
        calls.append(op.idname_py())
        return {"FINISHED"}
    d._security = lambda: AddonSecurity(restrict_paths=True, allowed_paths=[str(root / "allowed")])
    d.invoke_operator_checked = record
    try:
        revision = d._revision()
        out = run("project_save", {"pack": True})
        assert not out["success"] and out["error"]["type"] == "PermissionError", out
        assert calls == [], calls
        assert d._revision() == revision
    finally:
        d._security, d.invoke_operator_checked = sec, checked


def test_blender_relative_paths_share_one_resolution():
    root = Path(tempfile.mkdtemp(prefix="phase5_paths_"))
    other_cwd = Path(tempfile.mkdtemp(prefix="phase5_cwd_"))
    base = root / "base.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(base))
    sec = d._security
    cwd = Path.cwd()
    d._security = lambda: AddonSecurity(restrict_paths=True, allowed_paths=[str(root)])
    os.chdir(other_cwd)
    try:
        saved = ok("project_save", {"path": "//relative.blend"})
        assert saved["saved"] == str(root / "relative.blend")
        assert (root / "relative.blend").exists()
        snapshot = ok("project_checkpoint", {"directory": "//checkpoints"})
        assert Path(snapshot["checkpoint"]).parent == root / "checkpoints"
        d.compat.select_only(bpy.data.objects["Cube"])
        exported = ok("asset_export", {"path": "//selected.obj", "format": "OBJ"})
        assert exported["exported"] == str(root / "selected.obj")
        imported = ok("asset_import", {"path": "//selected.obj", "format": "OBJ"})
        assert imported["imported"] == str(root / "selected.obj")
        opened = ok("project_open", {"path": "//base.blend", "create_checkpoint": False})
        assert opened["opened"] == str(base) and Path(bpy.data.filepath) == base
    finally:
        os.chdir(cwd)
        d._security = sec


if __name__ == "__main__":
    failures = []
    for name, fn in list(globals().items()):
        if not name.startswith("test_"):
            continue
        try:
            fn()
            print("FIX1 PASS", name, flush=True)
        except BaseException:
            failures.append(name)
            traceback.print_exc()
    print("FIX1 SUMMARY", json.dumps({"failed": failures}), flush=True)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(bool(failures))
