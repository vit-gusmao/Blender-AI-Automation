"""Blender 3.6 regression assertions; --task=N selects a TDD task."""
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "blender_addon"))
import bpy
from universal_mcp import dispatcher as d

AUTH = {"level": "advanced", "approved": True}


def run(operation, arguments=None, **kwargs):
    return d.execute_command({"command_id": "phase5", "operation": operation,
                              "arguments": arguments or {},
                              "authorization": AUTH, **kwargs})


def ok(operation, arguments=None, **kwargs):
    out = run(operation, arguments, **kwargs)
    assert out["success"], out
    return out["data"]


def task2():
    rev = d._revision()
    invalid = run("object_transform", {"name": "Cube", "location": [1]}, dry_run=True)
    assert not invalid["success"], invalid
    assert d._revision() == rev
    assert not run("object_transform", {"name": "Missing"}, dry_run=True)["success"]
    assert ok("object_transform", {"name": "Cube", "location": [1, 2, 3]},
              dry_run=True)["dry_run"]
    assert tuple(bpy.data.objects["Cube"].location) == (0, 0, 0)
    assert not run("python_execute_blender", {"code": "result=1"},
                   authorization={})["success"]
    out = run("python_execute_blender", {"code": "print('partial'); import sys; print('err',file=sys.stderr); raise SystemExit(3)"})
    assert out["error"]["type"] == "SystemExit", out
    assert "partial" in out["data"]["stdout"] and "err" in out["data"]["stderr"]
    data = ok("python_execute_blender", {"code": "result = hasattr(bpy_extras, 'object_utils')"})
    assert data["result"] is True
    assert data["timeout_enforced"] is False
    before = d._revision()
    ok("blender_ping")
    assert d._revision() == before
    ok("object_transform", {"name": "Cube", "location": [0, 0, 0]})
    assert d._revision() == before + 1


def mesh_fingerprint(mesh):
    return (tuple(tuple(v.co) for v in mesh.vertices),
            tuple(tuple(e.vertices) for e in mesh.edges),
            tuple(tuple(p.vertices) for p in mesh.polygons))


def task3():
    ok("object_create", {"type": "CUBE", "name": "DeleteA"})
    ok("object_create", {"type": "CUBE", "name": "DeleteB"})
    out = run("object_delete", {"names": ["DeleteA", "Missing"]})
    assert not out["success"] and "DeleteA" in bpy.data.objects
    orphan = bpy.data.meshes.new("UnrelatedOrphan")
    data = bpy.data.objects["DeleteA"].data
    data_name = data.name
    result = ok("object_delete", {"names": ["DeleteA", "DeleteB"], "delete_data": True})
    assert result["count"] == 2 and data_name not in bpy.data.meshes
    assert orphan.name in bpy.data.meshes
    assert not run("object_create", {"type": "CAMERA", "name": "Cube"})["success"]
    ok("object_transform", {"name": "Cube", "rotation_mode": "AXIS_ANGLE",
                            "rotation": [1.2, 0, 0, 1]})
    assert abs(bpy.data.objects["Cube"].rotation_axis_angle[0] - 1.2) < 1e-5
    obj = bpy.data.objects["Cube"]
    before = mesh_fingerprint(obj.data)
    out = run("mesh_edit", {"object": "Cube", "operations": [
        {"op": "SUBDIVIDE_EDGES", "params": {"cuts": 1}},
        {"op": "BEVEL", "params": {"segments": "bad"}}]})
    assert not out["success"] and mesh_fingerprint(obj.data) == before, out
    ok("object_create", {"type": "CUBE", "name": "Operand", "location": [0.5, 0, 0]})
    ok("mesh_boolean", {"object": "Cube", "operand": "Operand", "operation": "DIFFERENCE"})


def task4():
    ok("armature_create", {"name": "Rig", "bones": [
        {"name": "Bone", "head": [0, 0, -2], "tail": [0, 0, 2]}]})
    ok("rig_assign_weights", {"mesh": "Cube", "armature": "Rig", "mode": "AUTOMATIC_WEIGHTS"})
    assert bpy.data.objects["Cube"].parent.name == "Rig"
    assert not run("rig_assign_weights", {"mesh": "Cube", "armature": "Rig", "mode": "BAD"})["success"]
    action = bpy.data.actions.new("Keys")
    fc = action.fcurves.new("location", index=0)
    assert ok("animation_fcurve", {"action": "Keys", "operation": "LIST"})["fcurves"][0]["index"] == 0
    for frame in (1, 2, 3):
        kp = fc.keyframe_points.insert(frame, frame * 2)
        kp.interpolation = "LINEAR"
    ok("animation_fcurve", {"action": "Keys", "operation": "DELETE_KEYFRAME",
                            "data_path": "location", "index": 0, "frame": 2})
    assert [(tuple(k.co), k.interpolation) for k in fc.keyframe_points] == [
        ((1.0, 2.0), "LINEAR"), ((3.0, 6.0), "LINEAR")]
    ok("physics_configure", {"object": "Cube", "type": "RIGID_BODY"})
    assert bpy.data.objects["Cube"].rigid_body
    ok("physics_configure", {"object": "Cube", "type": "RIGID_BODY", "remove": True})
    assert bpy.data.objects["Cube"].rigid_body is None
    ok("camera_configure", {"fov": 0.9})
    assert abs(bpy.context.scene.camera.data.angle - 0.9) < 1e-5


def task5():
    class Operator:
        def __init__(self, poll, result):
            self.allowed, self.result = poll, result
            self.called = False
        def poll(self):
            return self.allowed
        def __call__(self, *args, **kwargs):
            self.called = True
            return self.result
    for allowed, result in ((False, {"FINISHED"}), (True, {"CANCELLED"})):
        op = Operator(allowed, result)
        try:
            d.invoke_operator_checked(op)
        except RuntimeError:
            pass
        else:
            raise AssertionError("non-finished operator accepted")
        assert op.called == allowed
    assert isinstance(ok("addon_operator_invoke", {"mode": "LIST_ADDONS"})["addons"], list)
    denied = run("addon_operator_invoke", {"mode": "INVOKE", "operator": "object.select_all"}, authorization={})
    assert not denied["success"]
    assert not run("addon_operator_invoke", {"mode": "INVOKE", "operator": "object.select_all.bad"})["success"]
    ok("object_create", {"type": "CUBE", "name": "ExportOnly"})
    for obj in bpy.context.view_layer.objects:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = bpy.data.objects["Cube"]
    before = {o.name for o in bpy.context.selected_objects}
    path = Path(tempfile.mkdtemp()) / "exact.obj"
    ok("asset_export", {"path": str(path), "format": "OBJ", "objects": ["ExportOnly"]})
    objects = [line for line in path.read_text().splitlines() if line.startswith("o ")]
    assert len(objects) == 1 and "ExportOnly" in objects[0], objects
    assert {o.name for o in bpy.context.selected_objects} == before
    assert bpy.context.view_layer.objects.active.name == "Cube"


def task6():
    obj = bpy.data.objects["Cube"]
    obj.rotation_mode = "XYZ"
    obj.data.edges.add(1)
    obj.data.edges[-1].vertices = obj.data.edges[0].vertices
    before = mesh_fingerprint(obj.data)
    revision = d._revision()
    inspected = ok("scene_validate", {"checks": ["invalid_meshes"], "fix": False})
    assert inspected["issue_count"] > 0 and not inspected["fixed"]
    assert mesh_fingerprint(obj.data) == before and d._revision() == revision
    assert ok("scene_validate", {"checks": []})["checks"] == []
    assert not run("scene_validate", {"fix": True}, authorization={"level": "read_only"})["success"]
    fixed = ok("scene_validate", {"checks": ["invalid_meshes"], "fix": True})
    assert fixed["fixed"] and mesh_fingerprint(obj.data) != before
    assert d._revision() == revision + 1
    ok("object_duplicate", {"name": "Cube", "new_name": "Compare"})
    other = bpy.data.objects["Compare"]
    other.rotation_euler.x += 0.4
    other.data.vertices[0].co.x += 0.25
    a = obj.modifiers.new("SameName", "BEVEL")
    b = other.modifiers.new("SameName", "BEVEL")
    a.width, b.width = 0.1, 0.9
    compared = ok("object_compare", {"a": "Cube", "b": "Compare"})
    assert compared["transform"] != "idêntico" and not compared["geometry"]["same"]
    assert not compared["modifiers"]["same"] and not compared["equal"]


def task7():
    from universal_mcp.render_state import PendingRender
    from universal_mcp.command_queue import CommandQueue
    scene = bpy.context.scene
    scene.render.engine = "BLENDER_EEVEE"
    scene.render.resolution_x, scene.render.resolution_y = 16, 16
    scene.render.resolution_percentage = 75
    scene.render.filepath, scene.render.use_file_extension = "old.png", False
    state = (scene.render.resolution_percentage, scene.render.filepath,
             scene.render.use_file_extension, scene.camera)
    path = Path(tempfile.mkdtemp()) / "render.png"
    ok("render_preview", {"filepath": str(path), "percentage": 25})
    assert path.exists()
    assert state == (scene.render.resolution_percentage, scene.render.filepath,
                     scene.render.use_file_extension, scene.camera)
    pending = PendingRender.capture("first", scene, str(path.with_name("missing.png")), False)
    d.PENDING_RENDERS["first"] = pending
    assert not run("render_preview", {"filepath": str(path)})["success"]
    queue = CommandQueue()
    d.complete_render("complete", queue, scene)
    result = queue.peek_result()
    assert result["command_id"] == "first" and not result["success"]
    assert result["error"]["type"] == "RenderOutputMissing"
    queue.ack_result(result)
    d.complete_render("complete", queue, scene)
    assert queue.peek_result() is None
    d.PENDING_RENDERS["cancel"] = PendingRender.capture("cancel", scene, str(path), False)
    d.complete_render("cancel", queue, scene)
    assert queue.peek_result()["error"]["type"] == "RenderCancelled"


def task8():
    import universal_mcp
    from universal_mcp import snapshot_manager as snapshots
    root = Path(tempfile.mkdtemp())
    paths = [snapshots.create_checkpoint("same", str(root), max_keep=2)["checkpoint"] for _ in range(3)]
    assert len(set(paths)) == 3 and len(list(root.glob("*.blend"))) == 2
    from universal_mcp.storage_paths import storage_root
    assert "blender_addon" not in str(storage_root())
    assert universal_mcp.bl_info["blender"] == (3, 6, 0)
    # Register through Blender's real AddonPreferences/RNA lifecycle.
    import addon_utils
    addon_utils.enable("universal_mcp", default_set=True, persistent=False)
    prefs = bpy.context.preferences.addons["universal_mcp"].preferences
    assert prefs.checkpoint_max_keep == 10 and hasattr(prefs, "storage_dir")
    from universal_mcp import operators
    assert hasattr(bpy.context.window_manager, "universal_mcp_task_result")
    block = bpy.data.texts.new("Task")
    block.write("line one\nline two")
    assert operators.task_prompt(block) == "line one\nline two"
    operators._task_results.put({"answer": "visible"})
    operators._consume_task_results()
    assert "visible" in bpy.context.window_manager.universal_mcp_task_result
    addon_utils.disable("universal_mcp", default_set=True)


def final_review():
    from universal_mcp.security import AddonSecurity
    original = d._security
    allowed = Path(tempfile.mkdtemp())
    denied = allowed.parent / (allowed.name + "_denied.blend")
    d._security = lambda: AddonSecurity(restrict_paths=True, allowed_paths=[str(allowed)])
    try:
        out = run("project_save", {"path": str(denied)})
        assert not out["success"] and out["error"]["type"] == "PermissionError", out
        assert not run("render_preview", {"filepath": str(denied.with_suffix('.png'))}, dry_run=True)["success"]
        assert not run("addon_operator_invoke", {"mode": "INVOKE", "operator": "wm.save_as_mainfile",
                                                 "props": {"filepath": str(denied)}})["success"]
        from universal_mcp import snapshot_manager
        folder = allowed.parent / (allowed.name + "_denied_dir")
        try:
            snapshot_manager.create_checkpoint(directory=str(folder))
        except PermissionError:
            pass
        else:
            raise AssertionError("checkpoint outside policy allowed")
        assert not folder.exists(), "denied checkpoint created its directory"
    finally:
        d._security = original
    mat = bpy.data.materials.new("ReadOnlyNodes")
    assert not mat.use_nodes and mat.node_tree is None
    ok("nodes_list", {"tree": mat.name})
    assert not mat.use_nodes and mat.node_tree is None
    out = run("light_create", {"type": "POINT", "name": "Cube"})
    assert not out["success"], out
    data = ok("python_execute_blender", {"code": "import threading\nt = threading.Thread(target=lambda: print('other_thread')); t.start(); t.join(); print('main_thread')"})
    assert "main_thread" in data["stdout"] and "other_thread" not in data["stdout"], data
    assert not run("object_create", {"type": "CAMERA", "name": "Cube"}, dry_run=True)["success"]
    assert not run("camera_configure", {"fov": -1}, dry_run=True)["success"]
    assert not run("python_execute_blender", {"code": "import subprocess"}, dry_run=True)["success"]
    assert not run("python_execute_blender", {"code": "not python@"}, dry_run=True)["success"]
    ok("object_transform", {"name": "Cube", "rotation_mode": "QUATERNION", "rotation": [1, 0, 0, 0]})
    ok("object_transform", {"name": "Cube", "rotation_mode": "AXIS_ANGLE", "rotation": [0.5, 0, 0, 1]})
    ok("object_transform", {"name": "Cube", "rotation": [0.2, 0, 0, 1]})
    rev = d._revision()
    ok("object_create", {"type": "CUBE", "name": "Cube"})
    assert d._revision() == rev
    from universal_mcp.command_queue import CommandQueue
    from universal_mcp.render_state import PendingRender
    queue = CommandQueue()
    scene = bpy.context.scene
    d._ACTIVE_QUEUE = queue
    pending = PendingRender.capture("unload", scene, "absent.png", False)
    scene.render.resolution_percentage = 1
    d.PENDING_RENDERS["unload"] = pending
    d.load_post_unregister()
    assert not d.PENDING_RENDERS and queue.peek_result()["error"]["type"] == "RenderCancelled"
    assert scene.render.resolution_percentage == pending.render_values["resolution_percentage"]


if __name__ == "__main__":
    args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    task = next((int(a.split("=")[1]) for a in args if a.startswith("--task=")), None)
    failures = []
    for number in ([task] if task else list(range(2, 9)) + [9]):
        fn = final_review if number == 9 else globals().get("task" + str(number))
        if fn is None:
            continue
        try:
            fn()
            print("PHASE5 PASS", number, flush=True)
        except BaseException:
            failures.append(number)
            traceback.print_exc()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(1 if failures else 0)
