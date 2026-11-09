"""Local JSON schemas: stdlib-only mirror of bridge tool arguments."""

SCHEMAS = {
  "addon_operator_invoke": {
    "type": "object",
    "properties": {
      "mode": {
        "type": "string",
        "enum": [
          "LIST_ADDONS",
          "LIST_OPERATORS",
          "OPERATOR_INFO",
          "INVOKE"
        ]
      },
      "operator": {
        "type": "string",
        "description": "bl_idname (INVOKE/OPERATOR_INFO)"
      },
      "props": {
        "type": "object"
      },
      "filter": {
        "type": "string",
        "description": "Prefixo para LIST_OPERATORS"
      }
    },
    "required": [
      "mode"
    ]
  },
  "animation_action": {
    "type": "object",
    "properties": {
      "action": {
        "type": "string"
      },
      "create": {
        "type": "boolean"
      },
      "assign_to": {
        "type": "string",
        "description": "Objeto que recebe a action"
      },
      "set_fake_user": {
        "type": "boolean"
      },
      "delete": {
        "type": "boolean"
      }
    },
    "required": [
      "action"
    ]
  },
  "animation_fcurve": {
    "type": "object",
    "properties": {
      "action": {
        "type": "string"
      },
      "operation": {
        "type": "string",
        "enum": [
          "LIST",
          "SET_KEYFRAME",
          "SET_INTERPOLATION",
          "DELETE_KEYFRAME"
        ]
      },
      "data_path": {
        "type": "string"
      },
      "index": {
        "type": "integer"
      },
      "frame": {
        "type": "integer"
      },
      "value": {
        "type": "number"
      },
      "interpolation": {
        "type": "string",
        "enum": [
          "BEZIER",
          "LINEAR",
          "CONSTANT"
        ]
      }
    },
    "required": [
      "action",
      "operation"
    ]
  },
  "animation_keyframe": {
    "type": "object",
    "properties": {
      "object": {
        "type": "string"
      },
      "data_path": {
        "type": "string"
      },
      "frame": {
        "type": "integer"
      },
      "index": {
        "type": "integer",
        "description": "-1 para todos os canais"
      },
      "value": {}
    },
    "required": [
      "object",
      "data_path"
    ]
  },
  "animation_nla": {
    "type": "object",
    "properties": {
      "object": {
        "type": "string"
      },
      "operation": {
        "type": "string",
        "enum": [
          "LIST",
          "ADD_STRIP"
        ]
      },
      "track": {
        "type": "string"
      },
      "action": {
        "type": "string"
      },
      "start": {
        "type": "integer"
      }
    },
    "required": [
      "object",
      "operation"
    ]
  },
  "armature_create": {
    "type": "object",
    "properties": {
      "name": {
        "type": "string"
      },
      "bones": {
        "type": "array",
        "items": {
          "type": "object",
          "properties": {
            "name": {
              "type": "string"
            },
            "head": {
              "type": "array",
              "items": {
                "type": "number"
              }
            },
            "tail": {
              "type": "array",
              "items": {
                "type": "number"
              }
            },
            "parent": {
              "type": "string"
            },
            "use_connect": {
              "type": "boolean"
            },
            "radius": {
              "type": "number"
            }
          },
          "required": [
            "name",
            "head",
            "tail"
          ]
        }
      },
      "collection": {
        "type": "string"
      }
    },
    "required": [
      "name",
      "bones"
    ]
  },
  "asset_export": {
    "type": "object",
    "properties": {
      "path": {
        "type": "string"
      },
      "objects": {
        "type": "array",
        "items": {
          "type": "string"
        },
        "description": "Vazio = objetos selecionados"
      },
      "format": {
        "type": "string",
        "enum": [
          "OBJ",
          "FBX",
          "GLTF",
          "GLB",
          "STL",
          "PLY",
          "ALEMBIC",
          "USD",
          "BLEND",
          "DAE",
          "X3D"
        ]
      },
      "params": {
        "type": "object"
      }
    },
    "required": [
      "path"
    ]
  },
  "asset_import": {
    "type": "object",
    "properties": {
      "path": {
        "type": "string"
      },
      "format": {
        "type": "string",
        "enum": [
          "AUTO",
          "OBJ",
          "FBX",
          "GLTF",
          "GLB",
          "STL",
          "PLY",
          "ALEMBIC",
          "USD",
          "USDZ",
          "BLEND",
          "DAE",
          "SVG",
          "SVG_PATH",
          "X3D"
        ]
      },
      "params": {
        "type": "object",
        "description": "Opções do importador"
      }
    },
    "required": [
      "path"
    ]
  },
  "blender_get_version": {
    "type": "object",
    "properties": {}
  },
  "blender_ping": {
    "type": "object",
    "properties": {}
  },
  "bone_edit": {
    "type": "object",
    "properties": {
      "armature": {
        "type": "string"
      },
      "bone": {
        "type": "string"
      },
      "head": {
        "type": "array",
        "items": {
          "type": "number"
        }
      },
      "tail": {
        "type": "array",
        "items": {
          "type": "number"
        }
      },
      "roll": {
        "type": "number"
      },
      "parent": {
        "type": "string"
      },
      "use_connect": {
        "type": "boolean"
      },
      "delete": {
        "type": "boolean"
      }
    },
    "required": [
      "armature",
      "bone"
    ]
  },
  "camera_configure": {
    "type": "object",
    "properties": {
      "camera": {
        "type": "string",
        "description": "Padrão: câmera ativa"
      },
      "focal_length": {
        "type": "number"
      },
      "fov": {
        "type": "number",
        "description": "Field of view em radianos"
      },
      "type": {
        "type": "string",
        "enum": [
          "PERSP",
          "ORTHO",
          "PANO"
        ]
      },
      "clip_start": {
        "type": "number"
      },
      "clip_end": {
        "type": "number"
      },
      "dof_enable": {
        "type": "boolean"
      },
      "focus_distance": {
        "type": "number"
      },
      "aperture_fstop": {
        "type": "number"
      }
    }
  },
  "light_configure": {
    "type": "object",
    "properties": {
      "light": {
        "type": "string"
      },
      "energy": {
        "type": "number"
      },
      "color": {
        "type": "array",
        "items": {
          "type": "number"
        }
      },
      "size": {
        "type": "number"
      },
      "spot_size": {
        "type": "number"
      },
      "spot_blend": {
        "type": "number"
      },
      "use_shadow": {
        "type": "boolean"
      }
    },
    "required": [
      "light"
    ]
  },
  "light_create": {
    "type": "object",
    "properties": {
      "type": {
        "type": "string",
        "enum": [
          "POINT",
          "SUN",
          "SPOT",
          "AREA"
        ]
      },
      "name": {
        "type": "string"
      },
      "location": {
        "type": "array",
        "items": {
          "type": "number"
        }
      },
      "rotation": {
        "type": "array",
        "items": {
          "type": "number"
        }
      },
      "energy": {
        "type": "number"
      },
      "color": {
        "type": "array",
        "items": {
          "type": "number"
        }
      }
    },
    "required": [
      "type"
    ]
  },
  "material_assign": {
    "type": "object",
    "properties": {
      "object": {
        "type": "string"
      },
      "material": {
        "type": "string"
      },
      "slot": {
        "type": "integer"
      }
    },
    "required": [
      "object",
      "material"
    ]
  },
  "material_create": {
    "type": "object",
    "properties": {
      "name": {
        "type": "string"
      },
      "base_color": {
        "type": "array",
        "items": {
          "type": "number"
        },
        "description": "RGBA 0..1"
      },
      "metallic": {
        "type": "number"
      },
      "roughness": {
        "type": "number"
      },
      "emission_color": {
        "type": "array",
        "items": {
          "type": "number"
        }
      },
      "emission_strength": {
        "type": "number"
      },
      "alpha": {
        "type": "number"
      }
    },
    "required": [
      "name"
    ]
  },
  "material_set_property": {
    "type": "object",
    "properties": {
      "material": {
        "type": "string"
      },
      "property": {
        "type": "string"
      },
      "value": {}
    },
    "required": [
      "material",
      "property",
      "value"
    ]
  },
  "mesh_apply_modifier": {
    "type": "object",
    "properties": {
      "object": {
        "type": "string"
      },
      "action": {
        "type": "string",
        "enum": [
          "ADD",
          "APPLY",
          "REMOVE"
        ]
      },
      "modifier": {
        "type": "string",
        "description": "Nome do modificador"
      },
      "type": {
        "type": "string",
        "enum": [
          "SUBSURF",
          "MIRROR",
          "BEVEL",
          "ARRAY",
          "SOLIDIFY",
          "LATTICE",
          "CAST",
          "CURVE",
          "DISPLACE",
          "SIMPLE_DEFORM",
          "SUBSURF_SURFACE",
          "TRIANGULATE",
          "WIREFRAME",
          "WELD",
          "SCREW",
          "OCEAN",
          "GEOMETRY_NODES"
        ],
        "description": "Tipo (apenas ADD)"
      },
      "params": {
        "type": "object",
        "description": "Atributos do modificador"
      }
    },
    "required": [
      "object",
      "action"
    ]
  },
  "mesh_boolean": {
    "type": "object",
    "properties": {
      "object": {
        "type": "string"
      },
      "operand": {
        "type": "string",
        "description": "Segundo objeto"
      },
      "operation": {
        "type": "string",
        "enum": [
          "UNION",
          "DIFFERENCE",
          "INTERSECT"
        ]
      },
      "solver": {
        "type": "string",
        "enum": [
          "EXACT",
          "FAST"
        ]
      },
      "delete_operand": {
        "type": "boolean"
      }
    },
    "required": [
      "object",
      "operand",
      "operation"
    ]
  },
  "mesh_edit": {
    "type": "object",
    "properties": {
      "object": {
        "type": "string"
      },
      "operations": {
        "type": "array",
        "items": {
          "type": "object",
          "properties": {
            "op": {
              "type": "string",
              "enum": [
                "EXTRUDE_REGION",
                "EXTRUDE_FACES",
                "EXTRUDE_VERTICES",
                "INSET_FACES",
                "BEVEL",
                "SUBDIVIDE_EDGES",
                "SUBDIVIDE_FACE",
                "LOOP_CUT",
                "DELETE_FACES",
                "DELETE_EDGES",
                "DELETE_VERTICES",
                "MERGE_VERTICES",
                "RECALC_NORMALS",
                "FLIP_NORMALS",
                "SMOOTH_VERTICES",
                "SHADE_SMOOTH",
                "SHADE_FLAT",
                "TRIANGULATE",
                "FILL_HOLE",
                "SET_ORIGIN_TO_GEOMETRY",
                "MIRROR_X",
                "MIRROR_Y",
                "MIRROR_Z"
              ]
            },
            "params": {
              "type": "object"
            }
          },
          "required": [
            "op"
          ]
        }
      }
    },
    "required": [
      "object",
      "operations"
    ]
  },
  "mesh_validate": {
    "type": "object",
    "properties": {
      "object": {
        "type": "string"
      }
    },
    "required": [
      "object"
    ]
  },
  "nodes_connect": {
    "type": "object",
    "properties": {
      "tree": {
        "type": "string"
      },
      "tree_type": {
        "type": "string",
        "enum": [
          "ShaderNodeTree",
          "GeometryNodeTree",
          "CompositorNodeTree"
        ]
      },
      "from_node": {
        "type": "string"
      },
      "from_socket": {},
      "to_node": {
        "type": "string"
      },
      "to_socket": {}
    },
    "required": [
      "tree",
      "from_node",
      "to_node"
    ]
  },
  "nodes_create": {
    "type": "object",
    "properties": {
      "tree": {
        "type": "string"
      },
      "tree_type": {
        "type": "string",
        "enum": [
          "ShaderNodeTree",
          "GeometryNodeTree",
          "CompositorNodeTree"
        ]
      },
      "nodes": {
        "type": "array",
        "items": {
          "type": "object",
          "properties": {
            "type": {
              "type": "string",
              "description": "Ex.: ShaderNodeTexCoord"
            },
            "name": {
              "type": "string"
            },
            "location": {
              "type": "array",
              "items": {
                "type": "number"
              }
            }
          },
          "required": [
            "type"
          ]
        }
      }
    },
    "required": [
      "tree",
      "nodes"
    ]
  },
  "nodes_list": {
    "type": "object",
    "properties": {
      "tree": {
        "type": "string",
        "description": "Nome do material (ShaderNodeTree), node_group ou cena"
      },
      "tree_type": {
        "type": "string",
        "enum": [
          "ShaderNodeTree",
          "GeometryNodeTree",
          "CompositorNodeTree"
        ]
      }
    },
    "required": [
      "tree"
    ]
  },
  "nodes_set_input": {
    "type": "object",
    "properties": {
      "tree": {
        "type": "string"
      },
      "tree_type": {
        "type": "string",
        "enum": [
          "ShaderNodeTree",
          "GeometryNodeTree",
          "CompositorNodeTree"
        ]
      },
      "node": {
        "type": "string"
      },
      "input": {},
      "value": {}
    },
    "required": [
      "tree",
      "node",
      "input",
      "value"
    ]
  },
  "object_compare": {
    "type": "object",
    "properties": {
      "a": {
        "type": "string"
      },
      "b": {
        "type": "string"
      },
      "check": {
        "type": "array",
        "items": {
          "type": "string",
          "enum": [
            "transform",
            "geometry",
            "materials",
            "modifiers"
          ]
        }
      }
    },
    "required": [
      "a",
      "b"
    ]
  },
  "object_create": {
    "type": "object",
    "properties": {
      "type": {
        "type": "string",
        "enum": [
          "CUBE",
          "UV_SPHERE",
          "IO_SPHERE",
          "CYLINDER",
          "CONE",
          "TORUS",
          "PLANE",
          "MONKEY",
          "EMPTY",
          "ARMATURE",
          "CAMERA"
        ]
      },
      "name": {
        "type": "string"
      },
      "location": {
        "type": "array",
        "items": {
          "type": "number"
        }
      },
      "rotation": {
        "type": "array",
        "items": {
          "type": "number"
        },
        "description": "Rotação XYZ em radianos"
      },
      "scale": {
        "type": "array",
        "items": {
          "type": "number"
        }
      },
      "collection": {
        "type": "string"
      }
    },
    "required": [
      "type"
    ]
  },
  "object_delete": {
    "type": "object",
    "properties": {
      "name": {
        "type": "string"
      },
      "names": {
        "type": "array",
        "items": {
          "type": "string"
        }
      },
      "delete_data": {
        "type": "boolean",
        "description": "Remove também os datablocks órfãos"
      }
    }
  },
  "object_duplicate": {
    "type": "object",
    "properties": {
      "name": {
        "type": "string"
      },
      "new_name": {
        "type": "string"
      },
      "linked": {
        "type": "boolean"
      }
    },
    "required": [
      "name"
    ]
  },
  "object_get_info": {
    "type": "object",
    "properties": {
      "name": {
        "type": "string"
      },
      "include_modifiers": {
        "type": "boolean"
      },
      "include_materials": {
        "type": "boolean"
      }
    },
    "required": [
      "name"
    ]
  },
  "object_rename": {
    "type": "object",
    "properties": {
      "name": {
        "type": "string"
      },
      "new_name": {
        "type": "string"
      }
    },
    "required": [
      "name",
      "new_name"
    ]
  },
  "object_transform": {
    "type": "object",
    "properties": {
      "name": {
        "type": "string"
      },
      "location": {
        "type": "array",
        "items": {
          "type": "number"
        }
      },
      "rotation": {
        "type": "array",
        "items": {
          "type": "number"
        }
      },
      "rotation_mode": {
        "type": "string",
        "enum": [
          "XYZ",
          "XZY",
          "YXZ",
          "YZX",
          "ZXY",
          "ZYX",
          "QUATERNION",
          "AXIS_ANGLE"
        ]
      },
      "scale": {
        "type": "array",
        "items": {
          "type": "number"
        }
      }
    },
    "required": [
      "name"
    ]
  },
  "physics_configure": {
    "type": "object",
    "properties": {
      "object": {
        "type": "string"
      },
      "type": {
        "type": "string",
        "enum": [
          "RIGID_BODY",
          "CLOTH",
          "SOFT_BODY",
          "COLLISION",
          "FLUID"
        ]
      },
      "params": {
        "type": "object"
      },
      "remove": {
        "type": "boolean"
      }
    },
    "required": [
      "object",
      "type"
    ]
  },
  "project_checkpoint": {
    "type": "object",
    "properties": {
      "label": {
        "type": "string"
      },
      "directory": {
        "type": "string"
      }
    }
  },
  "project_open": {
    "type": "object",
    "properties": {
      "path": {
        "type": "string"
      },
      "create_checkpoint": {
        "type": "boolean",
        "description": "Checkpoint do projeto atual antes de abrir"
      }
    },
    "required": [
      "path"
    ]
  },
  "project_save": {
    "type": "object",
    "properties": {
      "path": {
        "type": "string",
        "description": "Vazio = salvar no atual"
      },
      "incremental": {
        "type": "boolean"
      },
      "pack": {
        "type": "boolean",
        "description": "Empacota recursos externos"
      }
    }
  },
  "python_execute_blender": {
    "type": "object",
    "properties": {
      "code": {
        "type": "string"
      },
      "timeout_s": {
        "type": "integer"
      }
    },
    "required": [
      "code"
    ]
  },
  "render_configure": {
    "type": "object",
    "properties": {
      "engine": {
        "type": "string",
        "enum": [
          "CYCLES",
          "BLENDER_EEVEE"
        ]
      },
      "resolution_x": {
        "type": "integer"
      },
      "resolution_y": {
        "type": "integer"
      },
      "resolution_percentage": {
        "type": "integer"
      },
      "samples": {
        "type": "integer"
      },
      "use_gpu": {
        "type": "boolean"
      },
      "filepath": {
        "type": "string"
      },
      "file_format": {
        "type": "string",
        "enum": [
          "PNG",
          "JPEG",
          "OPEN_EXR",
          "BMP",
          "TIFF",
          "WEBP",
          "FFMPEG"
        ]
      },
      "frame_start": {
        "type": "integer"
      },
      "frame_end": {
        "type": "integer"
      },
      "film_transparent": {
        "type": "boolean"
      },
      "use_stamp": {
        "type": "boolean"
      }
    }
  },
  "render_final": {
    "type": "object",
    "properties": {
      "filepath": {
        "type": "string"
      },
      "animation": {
        "type": "boolean"
      }
    }
  },
  "render_preview": {
    "type": "object",
    "properties": {
      "filepath": {
        "type": "string"
      },
      "percentage": {
        "type": "integer",
        "description": "Padrão 33"
      }
    }
  },
  "rig_assign_weights": {
    "type": "object",
    "properties": {
      "mesh": {
        "type": "string"
      },
      "armature": {
        "type": "string"
      },
      "mode": {
        "type": "string",
        "enum": [
          "AUTOMATIC_WEIGHTS",
          "ENVELOPE_WEIGHTS"
        ]
      },
      "clear_existing": {
        "type": "boolean"
      }
    },
    "required": [
      "mesh",
      "armature"
    ]
  },
  "scene_get_info": {
    "type": "object",
    "properties": {
      "include_collections": {
        "type": "boolean"
      }
    }
  },
  "scene_get_selection": {
    "type": "object",
    "properties": {}
  },
  "scene_list_objects": {
    "type": "object",
    "properties": {
      "type": {
        "type": "string",
        "description": "Filtrar por tipo (MESH, CAMERA...)"
      },
      "collection": {
        "type": "string"
      },
      "limit": {
        "type": "integer"
      }
    }
  },
  "scene_validate": {
    "type": "object",
    "properties": {
      "checks": {
        "type": "array",
        "items": {
          "type": "string",
          "enum": [
            "invalid_meshes",
            "duplicate_names",
            "broken_modifiers",
            "missing_images",
            "missing_materials",
            "orphan_actions"
          ]
        }
      },
      "fix": {
        "type": "boolean",
        "description": "Aplica correções; requer nível scene_write ou superior"
      }
    }
  },
  "simulation_bake": {
    "type": "object",
    "properties": {
      "type": {
        "type": "string",
        "enum": [
          "ALL",
          "FLUID",
          "RIGID_BODY",
          "CLOTH",
          "SOFT_BODY"
        ]
      },
      "frame_start": {
        "type": "integer"
      },
      "frame_end": {
        "type": "integer"
      }
    }
  },
  "texture_assign": {
    "type": "object",
    "properties": {
      "material": {
        "type": "string"
      },
      "texture": {
        "type": "string"
      },
      "input": {
        "type": "string",
        "description": "Nome do input do BSDF"
      },
      "non_color": {
        "type": "boolean",
        "description": "Usa colorspace Non-Color"
      }
    },
    "required": [
      "material",
      "texture"
    ]
  },
  "texture_load": {
    "type": "object",
    "properties": {
      "path": {
        "type": "string"
      },
      "name": {
        "type": "string"
      }
    },
    "required": [
      "path"
    ]
  },
  "uv_unwrap": {
    "type": "object",
    "properties": {
      "object": {
        "type": "string"
      },
      "method": {
        "type": "string",
        "enum": [
          "SMART_PROJECT",
          "SPHERE_PROJECT",
          "CYLINDER_PROJECT",
          "CUBE_PROJECT",
          "UNWRAP"
        ]
      },
      "margin": {
        "type": "number"
      },
      "angle_limit": {
        "type": "number"
      }
    },
    "required": [
      "object"
    ]
  }
}

