"""Shared storage location, independent of an installed addon's directory."""
import os
from pathlib import Path
from .path_utils import resolve_blender_path


def storage_root(prefs=None):
    custom = getattr(prefs, "storage_dir", "") if prefs else ""
    if custom:
        return resolve_blender_path(custom)
    root = os.environ.get("BLENDER_MCP_ROOT")
    if root:
        return resolve_blender_path(root) / "storage"
    import bpy
    return Path(bpy.utils.user_resource("CONFIG", path="universal_mcp", create=True)) / "storage"
