import bpy, json

out = {"usage": {}, "images": [], "shots": []}

for ob in bpy.context.scene.objects:
    if ob.type != 'MESH':
        continue
    counts = {}
    for p in ob.data.polygons:
        mi = p.material_index
        mname = ob.material_slots[mi].name if mi < len(ob.material_slots) else "??"
        counts[mname] = counts.get(mname, 0) + 1
    if counts:
        out["usage"][ob.name] = counts

for img in bpy.data.images:
    if img.name in ('Render Result', 'Viewer Node'):
        continue
    out["images"].append({"name": img.name, "size": list(img.size),
                          "filepath": img.filepath,
                          "packed": img.packed_file is not None, "users": img.users})

win = bpy.context.window_manager.windows[0] if bpy.context.window_manager.windows else None
area = next((a for a in win.screen.areas if a.type == 'VIEW_3D'), None) if win else None
if area:
    region = next(r for r in area.regions if r.type == 'WINDOW')
    space = area.spaces.active
    base_dir = "C:\\Blender\\RPA\\storage\\renders\\"
    for label, ax in (("front", 'FRONT'), ("left", 'LEFT'), ("right", 'RIGHT'), ("back", 'BACK')):
        with bpy.context.temp_override(window=win, area=area, region=region):
            bpy.ops.view3d.view_axis(type=ax)
            bpy.ops.view3d.view_all(center=False)
            bpy.context.view_layer.update()
            fp = base_dir + "explore_" + label + ".png"
            bpy.ops.screen.screenshot(filepath=fp)
            out["shots"].append(fp)
else:
    out["shots"] = "NO_VIEW3D_AREA"

result = json.dumps(out)
