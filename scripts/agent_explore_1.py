import bpy, json

scene = bpy.context.scene
out = {"objects": [], "materials": []}
for ob in scene.objects:
    entry = {
        "name": ob.name, "type": ob.type,
        "location": [round(v, 4) for v in ob.location],
        "rotation_euler": [round(v, 4) for v in ob.rotation_euler],
        "scale": [round(v, 4) for v in ob.scale],
        "dimensions": [round(v, 4) for v in ob.dimensions],
        "parent": ob.parent.name if ob.parent else None,
        "vertex_count": len(ob.data.vertices) if ob.type == 'MESH' else None,
        "material_slots": [s.name for s in ob.material_slots] if ob.type == 'MESH' else None,
        "modifiers": [(m.name, m.type) for m in ob.modifiers],
        "hide_viewport": ob.hide_viewport, "hide_render": ob.hide_render,
    }
    if ob.type == 'MESH':
        bb = [ob.matrix_world @ v.co for v in ob.data.vertices]
        if bb:
            entry["world_bbox_min"] = [round(min(v[i] for v in bb), 4) for i in range(3)]
            entry["world_bbox_max"] = [round(max(v[i] for v in bb), 4) for i in range(3)]
    out["objects"].append(entry)

for m in bpy.data.materials:
    entry = {"name": m.name, "users": m.users, "use_nodes": m.use_nodes,
             "blend_method": getattr(m, "blend_method", None)}
    if m.use_nodes:
        texs, principled = [], []
        for n in m.node_tree.nodes:
            if n.type == 'TEX_IMAGE':
                texs.append({"node": n.name, "image": n.image.name if n.image else None,
                             "filepath": n.image.filepath if n.image else None})
            if n.type == 'BSDF_PRINCIPLED':
                bc = n.inputs.get('Base Color')
                col = bc.default_value[:] if bc and not bc.is_linked else None
                linked_to = [l.from_node.bl_idname for l in bc.links] if bc else []
                principled.append({"node": n.name, "base_color": [round(c, 4) for c in col] if col else None,
                                   "base_color_linked": linked_to})
        entry["image_textures"] = texs
        entry["principled"] = principled
    else:
        entry["diffuse_color"] = [round(c, 4) for c in m.diffuse_color]
    out["materials"].append(entry)

result = json.dumps(out)
