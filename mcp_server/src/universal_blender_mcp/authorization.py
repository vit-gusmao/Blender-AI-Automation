"""Níveis de autorização e políticas (§14 do plano).

Aplicados pelo servidor ANTES de qualquer despacho — a garantia vem do
código, não do System Prompt. O addon replica as checagens (defesa em
profundidade) via blender_addon/universal_mcp/security.py.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Set

from .settings import Settings


class AuthLevel(IntEnum):
    READ_ONLY = 1
    SCENE_WRITE = 2
    PROJECT_OPS = 3
    ADVANCED = 4


LEVEL_NAMES = {1: "read_only", 2: "scene_write", 3: "project_ops", 4: "advanced"}


def level_from_name(name: Optional[str], default: AuthLevel = AuthLevel.SCENE_WRITE) -> AuthLevel:
    if not name:
        return default
    for value, lvl in LEVEL_NAMES.items():
        if lvl == name.lower():
            return AuthLevel(value)
    return default


@dataclass
class Decision:
    allowed: bool
    level_required: AuthLevel
    level_granted: AuthLevel
    reason: str = ""


# Padrões replicados de config/policies.yaml (defesa se o arquivo sumir).
DEFAULT_TOOL_LEVELS: Dict[str, str] = {
    "python_execute_blender": "advanced",
    "python_execute_external": "advanced",
    "project_open": "project_ops",
    "project_save": "project_ops",
    "project_checkpoint": "project_ops",
    "asset_import": "project_ops",
    "asset_export": "project_ops",
    "object_delete": "project_ops",
    "addon_operator_invoke": "project_ops",
    "simulation_bake": "project_ops",
    "render_final": "project_ops",
    "render_preview": "scene_write",
}

DEFAULT_STRICT_PATTERNS = (
    "subprocess", "os.system", "os.popen", "shutil.rmtree", "ctypes",
    "socket.socket", "urllib.request", "requests", "__import__",
)
UNSAFE_HOST_APPROVAL_TOKEN = "I_ACKNOWLEDGE_UNSANDBOXED_HOST_EXECUTION"


@dataclass
class PathPolicy:
    allow: List[Path] = field(default_factory=list)
    deny: List[Path] = field(default_factory=list)

    def resolve_allowed(self, path: str | Path, root: Path,
                        blend_root: Optional[Path] = None) -> Path:
        """Resolve um caminho canônico e exige allowlist/denylist.

        ``//`` pertence ao Blender, não ao filesystem do processo servidor.
        Ele só pode ser resolvido quando o servidor recebeu a raiz do projeto
        ativo; sem esse contexto a operação falha fechada.
        """
        if not self.allow:
            raise PermissionError("allowlist vazia; caminho negado")

        raw = str(path)
        if raw.startswith("//"):
            if blend_root is None:
                raise PermissionError(
                    "caminho // requer projeto Blender ativo e salvo")
            base = Path(blend_root)
            if base.suffix.lower() == ".blend":
                base = base.parent
            candidate = base / raw[2:]
        else:
            candidate = Path(path)
            if not candidate.is_absolute():
                candidate = Path(root) / candidate

        try:
            resolved = candidate.resolve(strict=False)
        except (OSError, RuntimeError, ValueError) as exc:
            raise PermissionError(f"caminho não pôde ser resolvido: {path}") from exc

        for denied in self.deny:
            try:
                is_denied = resolved.is_relative_to(
                    denied.resolve(strict=False))
            except (OSError, ValueError):
                continue
            if is_denied:
                raise PermissionError(
                    f"caminho negado explicitamente: {resolved}")
        for allowed in self.allow:
            try:
                if resolved.is_relative_to(allowed.resolve(strict=False)):
                    return resolved
            except (OSError, RuntimeError, ValueError):
                continue
        raise PermissionError(f"caminho fora da allowlist: {resolved}")

    def is_allowed(self, path: str | Path, root: Path,
                   blend_root: Optional[Path] = None) -> bool:
        try:
            self.resolve_allowed(path, root, blend_root=blend_root)
            return True
        except PermissionError:
            return False


@dataclass
class PythonPolicy:
    allow_blender: bool = True
    allow_external: bool = True
    strict_patterns: List[str] = field(default_factory=lambda: list(DEFAULT_STRICT_PATTERNS))
    require_approval: bool = True

    def scan_source(self, code: str) -> List[str]:
        findings: List[str] = []
        for pattern in self.strict_patterns:
            if pattern.startswith("os."):
                found = re.search(rf"\b{re.escape(pattern)}\s*\(", code)
            elif "." in pattern:
                # "a.b" também cobre o import disfarçado: from a import b
                module, _, attr = pattern.partition(".")
                found = (re.search(re.escape(pattern), code)
                         or re.search(rf"\bfrom\s+{re.escape(module)}\s+import\s+"
                                      rf"[^\n]*\b{re.escape(attr)}\b", code))
            elif "(" in pattern:
                found = re.search(re.escape(pattern), code)
            else:
                found = re.search(rf"\b{re.escape(pattern)}\b", code)
            if found:
                findings.append(pattern)
        return findings


@dataclass
class Policy:
    default_level: AuthLevel = AuthLevel.SCENE_WRITE
    tool_levels: Dict[str, AuthLevel] = field(default_factory=dict)
    paths: PathPolicy = field(default_factory=PathPolicy)
    python: PythonPolicy = field(default_factory=PythonPolicy)
    external_allowed: bool = True
    external_backend: Optional[str] = None
    allow_unsafe_host_execution: bool = False
    unsafe_approval_token: str = UNSAFE_HOST_APPROVAL_TOKEN
    workspace_only: bool = True
    auto_checkpoint: bool = True
    audit_arguments: bool = True
    audit_log_all: bool = True
    approval_required_tools: Set[str] = field(
        default_factory=lambda: {"object_delete"})

    def level_for(self, tool_name: str, registry_level: AuthLevel) -> AuthLevel:
        override = self.tool_levels.get(tool_name)
        if override is None:
            return registry_level
        return max(registry_level, override) if override < registry_level else override

    def authorize(self, tool_name: str, required: AuthLevel,
                  granted: AuthLevel) -> Decision:
        eff = self.level_for(tool_name, required)
        if granted >= eff:
            return Decision(True, eff, granted)
        return Decision(
            False, eff, granted,
            f"ferramenta '{tool_name}' requer nível {LEVEL_NAMES.get(int(eff))}; "
            f"sessão concedida: {LEVEL_NAMES.get(int(granted))}",
        )

    def require_approval(self, tool_name: str, args: Dict[str, Any],
                         session: Any) -> Decision:
        """Aplica elevação dinâmica e consentimento explícito da requisição.

        ``requested_by`` é deliberadamente ignorado: ele identifica a origem,
        não concede autorização. A aprovação pode vir do campo booleano
        ``approval`` da chamada ou de uma capacidade de sessão já autenticada.
        """
        granted = self._session_level(session)
        required = self.tool_levels.get(tool_name, AuthLevel.READ_ONLY)
        approval_required = tool_name in self.approval_required_tools

        if tool_name in ("python_execute_blender", "python_execute_external"):
            required = max(required, AuthLevel.ADVANCED)
            approval_required = (tool_name == "python_execute_blender"
                                 or self.python.require_approval)
        elif (tool_name == "addon_operator_invoke"
              and args.get("mode") == "INVOKE"):
            required = AuthLevel.ADVANCED
            approval_required = True
        elif tool_name == "scene_validate" and args.get("fix") is True:
            required = max(required, AuthLevel.SCENE_WRITE)

        level_decision = self.authorize(tool_name, required, granted)
        if not level_decision.allowed:
            return level_decision

        approved = args.get("approval") is True
        if isinstance(session, Mapping):
            approved = approved or session.get("approved") is True
            approved_tools = session.get("approved_tools", ())
            if isinstance(approved_tools, (list, tuple, set, frozenset)):
                approved = approved or tool_name in approved_tools
        if approval_required and not approved:
            return Decision(
                False, level_decision.level_required, granted,
                f"ferramenta '{tool_name}' requer aprovação explícita",
            )
        return level_decision

    def _session_level(self, session: Any) -> AuthLevel:
        raw = session.get("level") if isinstance(session, Mapping) else session
        if isinstance(raw, AuthLevel):
            return raw
        if isinstance(raw, int) and not isinstance(raw, bool):
            try:
                return AuthLevel(raw)
            except ValueError:
                return self.default_level
        if isinstance(raw, str):
            return level_from_name(raw, self.default_level)
        return self.default_level


def load_policy(settings: Settings) -> Policy:
    cfg = settings.policies_cfg or {}
    auth = cfg.get("authorization", {}) or {}
    policy = Policy(
        default_level=level_from_name(auth.get("default_level")),
        tool_levels={
            k: level_from_name(v) for k, v in (auth.get("tool_levels", {}) or {}).items()
        },
        approval_required_tools=set(
            auth.get("approval_required_tools", ["object_delete"]) or []),
    )
    paths = cfg.get("paths", {}) or {}
    policy.paths = PathPolicy(
        allow=[settings.path(p) for p in (paths.get("allow", []) or [])],
        deny=[Path(p) for p in (paths.get("deny", []) or [])],
    )
    py = cfg.get("python_execution", {}) or {}
    if py:
        policy.python = PythonPolicy(
            allow_blender=bool(py.get("allow_blender", True)),
            allow_external=bool(py.get("allow_external", True)),
            strict_patterns=list(py.get("strict_patterns", DEFAULT_STRICT_PATTERNS)),
            require_approval=bool(py.get("require_approval", True)),
        )
    ext = cfg.get("external_execution", {}) or {}
    policy.external_allowed = bool(ext.get("allow", True))
    backend = ext.get("backend")
    policy.external_backend = str(backend).strip() if backend else None
    policy.allow_unsafe_host_execution = bool(
        ext.get("allow_unsafe_host_execution", False))
    policy.unsafe_approval_token = str(
        ext.get("unsafe_approval_token", UNSAFE_HOST_APPROVAL_TOKEN))
    policy.workspace_only = bool(ext.get("workspace_only", True))
    ckpt = cfg.get("checkpoints", {}) or {}
    policy.auto_checkpoint = bool(ckpt.get("auto_before_destructive", True))
    audit_cfg = cfg.get("audit", {}) or {}
    policy.audit_arguments = bool(audit_cfg.get("log_arguments", True))
    policy.audit_log_all = bool(audit_cfg.get("log_all", True))
    return policy
