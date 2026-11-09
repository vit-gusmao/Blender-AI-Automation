"""Main-replacement regression; run in an isolated Blender process."""
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "blender_addon"))
import bpy

args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
scenario = args[0] if args else "project_open"
root = Path(tempfile.mkdtemp(prefix="phase5_main_load_"))
target = root / "target.blend"

# The two files have different view layers, active objects and saved selections.
# Reusing a stale ViewLayer after loading this target can access freed Main data.
bpy.context.scene.name = "LoadedScene"
bpy.context.view_layer.name = "LoadedLayer"
bpy.data.objects["Cube"].name = "LoadedOnly"
for obj in bpy.context.view_layer.objects:
    obj.select_set(obj.name == "LoadedOnly")
bpy.context.view_layer.objects.active = bpy.data.objects["LoadedOnly"]
bpy.ops.wm.save_as_mainfile(filepath=str(target))
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.context.scene.name = "SourceScene"
bpy.context.view_layer.name = "SourceLayer"
bpy.ops.mesh.primitive_cube_add()
bpy.context.object.name = "SourceOnly"

from universal_mcp import dispatcher as d

auth = {"level": "advanced", "approved": True}
if scenario == "library_roundtrip":
    def library_roundtrip(arguments, command):
        with bpy.data.libraries.load(str(target), link=False) as (source, loaded):
            loaded.objects = ["LoadedOnly"]
        appended = loaded.objects[0]
        bpy.context.scene.collection.objects.link(appended)
        d.compat.select_only(appended)
        bpy.data.libraries.write(str(root / "subset.blend"), {appended})
        return {"appended": appended.name}
    d._OPERATIONS["test_library_roundtrip"] = library_roundtrip
    before_handlers = tuple(bpy.app.handlers.load_pre)
    result = d.execute_command({"command_id": "library", "operation": "test_library_roundtrip",
                                "arguments": {}, "authorization": auth})
    assert result["success"], result
    assert bpy.context.scene.name == "SourceScene"
    assert {obj.name for obj in bpy.context.selected_objects} == {"SourceOnly"}
    assert bpy.context.view_layer.objects.active.name == "SourceOnly"
    assert "LoadedOnly" in bpy.data.objects and (root / "subset.blend").is_file()
    assert tuple(bpy.app.handlers.load_pre) == before_handlers
    print("FIX2 PASS", scenario, flush=True)
    os._exit(0)
if scenario == "project_open":
    operation, arguments = "project_open", {"path": str(target), "create_checkpoint": False}
elif scenario == "asset_import":
    operation, arguments = "asset_import", {"path": str(target), "format": "AUTO"}
elif scenario == "operator_load":
    operation, arguments = "addon_operator_invoke", {
        "mode": "INVOKE", "operator": "wm.open_mainfile", "props": {"filepath": str(target)}}
elif scenario == "python_load":
    operation, arguments = "python_execute_blender", {
        "code": "bpy.ops.wm.open_mainfile(filepath=" + repr(str(target)) + ")\nresult=True"}
else:
    raise AssertionError(scenario)
print("FIX2 BEFORE_LOAD", scenario, flush=True)
result = d.execute_command({"command_id": "main-load", "operation": operation,
                            "arguments": arguments, "authorization": auth})
print("FIX2 AFTER_LOAD", json.dumps(result), flush=True)
assert result["success"], result
assert bpy.context.scene.name == "LoadedScene"
assert bpy.context.view_layer.name == "LoadedLayer"
assert {obj.name for obj in bpy.context.selected_objects} == {"LoadedOnly"}
assert bpy.context.view_layer.objects.active.name == "LoadedOnly"
assert not any(handler.__name__ == "on_load_pre" for handler in bpy.app.handlers.load_pre)
print("FIX2 PASS", scenario, flush=True)
sys.stdout.flush()
sys.stderr.flush()
os._exit(0)
