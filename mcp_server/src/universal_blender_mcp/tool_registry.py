"""Registro central de ferramentas MCP (catálogo do §6.1 do plano)."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .authorization import AuthLevel, LEVEL_NAMES


@dataclass
class ToolSpec:
    name: str
    description: str
    category: str
    auth_level: AuthLevel
    params: Dict[str, Any]
    handler: Optional[Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]] = None
    bridge: bool = True  # True = despacha ao Blender via Bridge
    risk: str = "low"    # low | medium | high | destructive
    timeout_s: int = 120

    def input_schema(self) -> Dict[str, Any]:
        return {"type": "object", **self.params}

    def path_arguments(self, args: Dict[str, Any]) -> List[str]:
        """Extrai os valores de caminho relevantes desta ferramenta."""
        if self.name not in PATH_SENSITIVE_TOOLS:
            return []
        return _path_arguments(
            args, arbitrary=self.name == "addon_operator_invoke")


Handler = Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> ToolSpec:
        if spec.name in self._tools:
            raise ValueError(f"ferramenta duplicada: {spec.name}")
        self._tools[spec.name] = spec
        return spec

    def get(self, name: str) -> Optional[ToolSpec]:
        return self._tools.get(name)

    def names(self) -> List[str]:
        return sorted(self._tools)

    def specs(self) -> List[ToolSpec]:
        return [self._tools[n] for n in self.names()]

    def by_category(self) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = {}
        for spec in self.specs():
            out.setdefault(spec.category, []).append(spec.name)
        return out

    def __len__(self) -> int:
        return len(self._tools)


@dataclass
class ServerContext:
    """Dependências injetadas nos handlers das ferramentas."""
    settings: Any
    bridge: Any                 # BridgeGateway
    jobs: Any                   # JobManager
    policy: Any                 # Policy (authorization.py)
    audit: Any                  # AuditLogger
    registry_ref: Optional[ToolRegistry] = None
    session_level: Optional[AuthLevel] = None


#: Ferramentas cujos argumentos de caminho são validados contra a allowlist (§14)
PATH_SENSITIVE_TOOLS = frozenset({
    "project_open", "project_save", "project_checkpoint", "asset_import",
    "asset_export", "texture_load", "render_final", "render_preview",
    "render_configure", "addon_operator_invoke",
})

_PATH_KEY_PARTS = ("path", "file", "directory", "folder")
_NON_FILESYSTEM_PATH_KEYS = frozenset({"data_path", "rna_path"})
SERVER_AUTHORIZATION_KEY = "__server_authorization__"


def _looks_path_key(key: str) -> bool:
    lowered = key.lower()
    return (lowered not in _NON_FILESYSTEM_PATH_KEYS
            and any(part in lowered for part in _PATH_KEY_PARTS))


def _looks_path_value(value: str) -> bool:
    return (value.startswith(("//", "./", "../", "~", "\\\\"))
            or Path(value).is_absolute())


def _path_arguments(value: Any, key: str = "", arbitrary: bool = False) -> List[str]:
    found: List[str] = []
    if isinstance(value, dict):
        for child_key, child in value.items():
            found.extend(_path_arguments(
                child, str(child_key), arbitrary=arbitrary))
    elif isinstance(value, (list, tuple)):
        for child in value:
            found.extend(_path_arguments(child, key, arbitrary=arbitrary))
    elif isinstance(value, str) and value.strip():
        if _looks_path_key(key) or (arbitrary and _looks_path_value(value)):
            found.append(value)
    return list(dict.fromkeys(found))


def _denied(error_type: str, message: str) -> Dict[str, Any]:
    return {
        "command_id": "", "status": "failed", "success": False,
        "data": None, "scene_revision": None,
        "error": {"type": error_type, "message": message},
    }


def bridge_tool(ctx: ServerContext, registry: ToolRegistry, name: str,
                description: str, category: str, params: Dict[str, Any],
                auth_level: AuthLevel = AuthLevel.SCENE_WRITE,
                risk: str = "low", timeout_s: int = 120) -> None:
    """Registra uma ferramenta cujo handler despacha a operação à Bridge."""
    async def handler(args: Dict[str, Any]) -> Dict[str, Any]:
        policy = getattr(ctx, "policy", None)
        dispatch_args = dict(args)
        trusted_auth = dispatch_args.pop(SERVER_AUTHORIZATION_KEY, None)
        request_approval = dispatch_args.pop("approval", None)

        if policy is not None and trusted_auth is None:
            approval_args = dict(dispatch_args)
            if request_approval is not None:
                approval_args["approval"] = request_approval
            session = getattr(ctx, "session_level", None) or policy.default_level
            decision = policy.require_approval(name, approval_args, session)
            if not decision.allowed:
                error_type = ("ApprovalRequired"
                              if "aprovação explícita" in decision.reason
                              else "Unauthorized")
                return _denied(error_type, decision.reason)
            trusted_auth = {
                "level": LEVEL_NAMES[int(decision.level_granted)],
                "approved": request_approval is True,
            }

        if policy is not None:
            blend_root = (getattr(ctx, "active_blend_root", None)
                          or getattr(ctx.settings, "active_blend_root", None))
            for raw in spec.path_arguments(dispatch_args):
                try:
                    policy.paths.resolve_allowed(
                        raw, ctx.settings.root, blend_root=blend_root)
                except PermissionError as exc:
                    return _denied("PathNotAllowed", str(exc))

        return await ctx.bridge.execute(
            name, dispatch_args, timeout_s=timeout_s,
            requested_by={"tool": name}, authorization=trusted_auth)

    spec = ToolSpec(
        name=name, description=description, category=category,
        auth_level=auth_level, params=params, handler=handler,
        bridge=True, risk=risk, timeout_s=timeout_s,
    )
    registry.register(spec)


def vec(items_type: str = "number", desc: str = "") -> Dict[str, Any]:
    return {"type": "array", "items": {"type": items_type},
            "description": desc} if desc else {"type": "array",
                                               "items": {"type": items_type}}


def enum_param(*values: str, desc: str = "") -> Dict[str, Any]:
    p: Dict[str, Any] = {"type": "string", "enum": list(values)}
    if desc:
        p["description"] = desc
    return p


def build_registry(ctx: ServerContext) -> ToolRegistry:
    from .tools import (animation, files, import_export, materials, modeling,
                        nodes, objects, python_execution, rendering, scene)

    registry = ToolRegistry()
    ctx.registry_ref = registry
    for module in (scene, objects, modeling, materials, nodes, animation,
                   rendering, import_export, files, python_execution):
        module.register(ctx, registry)
    return registry
