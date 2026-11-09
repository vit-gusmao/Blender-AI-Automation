# SPDX-License-Identifier: MIT
"""Segurança local do addon (defesa em profundidade — §14 do plano).

O servidor MCP é a primeira barreira (níveis + allowlist de caminhos).
O addon replica as checagens essenciais para o caso de contato direto.
"""
from __future__ import annotations

import re
import ast
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

DEFAULT_BLOCKED_PATTERNS = (
    "subprocess", "os.system", "os.popen", "shutil.rmtree", "ctypes",
    "socket.socket", "urllib.request", "requests", "__import__",
)

#: Operações mutativas (incrementam scene_revision)
MUTATING_OPERATIONS = frozenset({
    "object_create", "object_delete", "object_duplicate", "object_rename",
    "object_transform", "mesh_edit", "mesh_apply_modifier", "mesh_boolean",
    "material_create", "material_assign", "material_set_property",
    "texture_load", "texture_assign", "uv_unwrap", "nodes_create",
    "nodes_connect", "nodes_set_input", "armature_create", "bone_edit",
    "rig_assign_weights", "animation_keyframe", "animation_action",
    "animation_fcurve", "animation_nla", "physics_configure",
    "simulation_bake", "camera_configure", "light_create", "light_configure",
    "render_configure", "project_open", "project_save", "asset_import",
    "asset_export", "addon_operator_invoke", "python_execute_blender",
})

DESTRUCTIVE_OPERATIONS = frozenset({
    "object_delete", "project_open", "addon_operator_invoke",
    "python_execute_blender",
})


def is_destructive(operation, arguments=None):
    args = arguments or {}
    if operation == "addon_operator_invoke":
        return args.get("mode") == "INVOKE"
    if operation in DESTRUCTIVE_OPERATIONS:
        return True
    if operation == "mesh_apply_modifier":
        return args.get("action") in ("APPLY", "REMOVE")
    if operation == "mesh_boolean":
        return True
    if operation == "mesh_edit":
        return any(step.get("op", "").startswith(("DELETE_", "MERGE_"))
                   for step in args.get("operations", []))
    if operation in ("bone_edit", "animation_action"):
        return bool(args.get("delete"))
    if operation == "animation_fcurve":
        return args.get("operation") == "DELETE_KEYFRAME"
    if operation == "physics_configure":
        return bool(args.get("remove"))
    return operation == "scene_validate" and bool(args.get("fix"))


class AddonSecurity:
    def __init__(self, allow_python: bool = True, strict_python: bool = True,
                 blocked_patterns: Optional[Iterable[str]] = None,
                 restrict_paths: bool = False,
                 allowed_paths: Optional[List[str]] = None):
        self.allow_python = allow_python
        self.strict_python = strict_python
        self.blocked = [p for p in (blocked_patterns or DEFAULT_BLOCKED_PATTERNS) if p]
        self.restrict_paths = restrict_paths
        self.allowed_paths = [Path(p) for p in (allowed_paths or []) if p]

    # -- fábrica a partir das preferências ------------------------------------

    @classmethod
    def from_prefs(cls, prefs) -> "AddonSecurity":
        return cls(
            allow_python=getattr(prefs, "allow_python", True),
            strict_python=getattr(prefs, "strict_python", True),
            blocked_patterns=(
                p.strip() for p in getattr(prefs, "blocked_patterns", "").split(";")
                if p.strip()
            ),
            restrict_paths=getattr(prefs, "restrict_paths", False),
            allowed_paths=[
                p.strip() for p in getattr(prefs, "allowed_paths", "").split(";")
                if p.strip()
            ],
        )

    # -- checagens --------------------------------------------------------------

    def check_operation(self, operation: str, arguments=None,
                        authorization=None) -> Tuple[bool, str]:
        if operation == "python_execute_blender" and not self.allow_python:
            return False, "execução de Python desabilitada nas preferências do addon"
        args, auth = arguments or {}, authorization or {}
        advanced = operation == "python_execute_blender" or (
            operation == "addon_operator_invoke" and args.get("mode") == "INVOKE")
        if advanced and (auth.get("level") != "advanced" or auth.get("approved") is not True):
            return False, "advanced authorization and explicit approval required"
        if operation == "scene_validate" and args.get("fix"):
            if auth.get("level") not in ("scene_write", "project_ops", "advanced"):
                return False, "scene_write authorization required for repairs"
        return True, ""

    def scan_python_source(self, code: str) -> List[str]:
        findings: List[str] = []
        # Resolve ordinary import aliases. This remains defense in depth;
        # dynamic Python cannot be sandboxed by source scanning.
        aliases = {}
        try:
            tree = ast.parse(code)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for item in node.names:
                        if item.asname:
                            aliases[item.asname] = item.name
                elif isinstance(node, ast.ImportFrom):
                    for item in node.names:
                        aliases[item.asname or item.name] = (node.module or "") + "." + item.name
            for alias, original in aliases.items():
                code = re.sub(r"\b" + re.escape(alias) + r"\b", original, code)
        except SyntaxError:
            pass  # compile() reports the syntax error with captured diagnostics
        for pattern in self.blocked:
            if pattern.startswith("os."):
                hit = re.search(rf"\b{re.escape(pattern)}\s*\(", code)
            elif "." in pattern:
                # "a.b" bloqueia uso direto e o import disfarçado from a import b
                module, _, attr = pattern.partition(".")
                hit = (re.search(re.escape(pattern), code)
                       or re.search(rf"\bfrom\s+{re.escape(module)}\s+import\s+"
                                    rf"[^\n]*\b{re.escape(attr)}\b", code))
            elif "(" in pattern:
                hit = re.search(re.escape(pattern), code)
            else:
                hit = re.search(rf"\b{re.escape(pattern)}\b", code)
            if hit:
                findings.append(pattern)
        return findings

    def path_allowed(self, path: str) -> bool:
        if not self.restrict_paths:
            return True
        p = Path(path).resolve()
        return any(p.is_relative_to(root.resolve()) for root in self.allowed_paths)
