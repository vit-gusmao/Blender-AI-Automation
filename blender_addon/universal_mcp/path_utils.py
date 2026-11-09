"""Resolve once before policy checks and Blender/file-system execution."""
from pathlib import Path

PATH_KEYS = frozenset({"path", "filepath", "directory", "filename"})


def resolve_blender_path(value, relative_root=None):
    raw = str(value)
    if raw.startswith("//"):
        import bpy
        raw = bpy.path.abspath(raw)
    path = Path(raw).expanduser()
    if not path.is_absolute() and relative_root is not None:
        path = Path(relative_root) / path
    return path.resolve()


def normalize_argument_paths(arguments, render_root=None):
    def walk(value, key="", directory=None, depth=0):
        if isinstance(value, dict):
            folder = value.get("directory") or directory
            if folder:
                folder = resolve_blender_path(folder)
            return {child: walk(val, child, folder, depth + 1)
                    for child, val in value.items()}
        if isinstance(value, list):
            return [walk(item, key, directory, depth + 1) for item in value]
        if isinstance(value, str) and value and key in PATH_KEYS:
            base = directory if key == "filename" else (
                render_root if key == "filepath" and depth == 1 else None)
            return str(resolve_blender_path(value, base))
        return value
    return walk(arguments)
