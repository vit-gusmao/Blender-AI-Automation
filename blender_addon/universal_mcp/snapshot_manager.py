# SPDX-License-Identifier: MIT
"""Checkpoints de projeto (§14): cópias datadas antes de ações destrutivas."""
from __future__ import annotations

import datetime
import logging
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import bpy
from .storage_paths import storage_root
from .path_utils import resolve_blender_path

log = logging.getLogger("universal_mcp.snapshot")


def _default_root() -> Path:
    prefs = bpy.context.preferences.addons.get("universal_mcp")
    return storage_root(prefs.preferences if prefs else None) / "checkpoints"


def checkpoints_root(directory: Optional[str] = None) -> Path:
    if directory:
        root = resolve_blender_path(directory)
    else:
        prefs = bpy.context.preferences.addons.get(__package__.split(".")[0])
        custom = getattr(prefs.preferences, "checkpoints_dir", "") if prefs else ""
        root = resolve_blender_path(custom) if custom else _default_root()
    from .dispatcher import _security
    if not _security().path_allowed(str(root)):
        raise PermissionError(f"checkpoint directory not authorized: {root}")
    root.mkdir(parents=True, exist_ok=True)
    return root


def create_checkpoint(label: str = "",
                      directory: Optional[str] = None,
                      max_keep: Optional[int] = None) -> Dict[str, Any]:
    root = checkpoints_root(directory)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    safe_label = "".join(c for c in label if c.isalnum() or c in "-_")[:40]
    name = f"mcp_{safe_label or 'checkpoint'}_{stamp}_{uuid.uuid4().hex[:8]}.blend"
    target = root / name
    # copy=True preserva o filepath atual do projeto (não redireciona o .blend)
    from .dispatcher import invoke_operator_checked, _security
    if not _security().path_allowed(str(target)):
        raise PermissionError(f"checkpoint path not authorized: {target}")
    invoke_operator_checked(bpy.ops.wm.save_as_mainfile, filepath=str(target), copy=True)
    if not target.is_file():
        raise RuntimeError("checkpoint operator did not create its output")
    prefs = bpy.context.preferences.addons.get("universal_mcp")
    keep = max(1, int(max_keep if max_keep is not None else
                      getattr(prefs.preferences, "checkpoint_max_keep", 10) if prefs else 10))
    files = sorted(root.glob("mcp_*.blend"), key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True)
    for old in files[keep:]:
        old.unlink()
    log.info("checkpoint criado: %s", target)
    return {"checkpoint": str(target), "label": label or "checkpoint",
            "source": bpy.data.filepath or "(não salvo)"}


def list_checkpoints(directory: Optional[str] = None,
                     limit: int = 50) -> List[Dict[str, Any]]:
    root = checkpoints_root(directory)
    files = sorted(
        (p for p in root.glob("*.blend")),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )[:limit]
    return [{"name": p.name, "path": str(p),
             "size_bytes": p.stat().st_size,
             "mtime": datetime.datetime.fromtimestamp(p.stat().st_mtime).isoformat()}
            for p in files]


def latest_checkpoint(directory: Optional[str] = None) -> Optional[str]:
    items = list_checkpoints(directory, limit=1)
    return items[0]["path"] if items else None
