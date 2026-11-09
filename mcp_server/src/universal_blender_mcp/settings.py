"""Carregamento de configuração (TOML/YAML) com parsers stdlib de reserva.

O plano exige TOML/YAML para configuração. ``tomllib`` (3.11+) e ``yaml``
são usados quando disponíveis; fallbacks internos cobrem o subconjunto de
sintaxe usado pelos arquivos em ``config/`` para que o servidor, a Bridge
e os testes funcionem em qualquer Python >= 3.10 sem dependências.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict

log = logging.getLogger(__name__)

try:  # quando importado como pacote, reaproveita os metadados de __init__
    from . import SERVER_NAME
except ImportError:  # execução standalone (ex.: smoke em subprocesso)
    SERVER_NAME = "universal-blender-mcp"

# ---------------------------------------------------------------------------
# Mini-parser TOML (subconjunto: seções aninhadas, escalares, arrays 1 linha)
# ---------------------------------------------------------------------------

_TOML_SECTION = re.compile(r"^\s*\[+([^\]]+)\]+\s*$")
_TOML_KV = re.compile(r'^\s*([A-Za-z0-9_.\-"]+)\s*=\s*(.+?)\s*$')
_TOML_STR = re.compile(r'^"((?:[^"\\]|\\.)*)"')


def _strip_toml_comment(value: str) -> str:
    if value.startswith('"'):
        m = _TOML_STR.match(value)
        if m:
            return m.group(0)
    # comentário após valor sem aspas
    idx = value.find(" #")
    if idx != -1:
        return value[:idx].strip()
    return value


def _parse_scalar(raw: str) -> Any:
    raw = raw.strip()
    if raw.startswith('"'):
        # JSON válido inclusive com apóstrofos internos (BUG-025) — sem troca
        # de aspas, que corrompia valores como "O'Brien's blend".
        return json.loads(raw)
    if raw.startswith("'"):
        return raw[1:-1]
    if raw.startswith("["):
        inner = raw[raw.index("[") : raw.rindex("]") + 1]
        return json.loads(inner)
    if raw in ("true", "false"):
        return raw == "true"
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw  # datas ou tokens desconhecidos seguem como string


def _mini_toml(text: str) -> Dict[str, Any]:
    root: Dict[str, Any] = {}
    current = root
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _TOML_SECTION.match(line)
        if m:
            current = root
            for part in m.group(1).split("."):
                current = current.setdefault(part.strip().strip('"'), {})
            continue
        m = _TOML_KV.match(line)
        if m:
            key, value = m.group(1).strip().strip('"'), _strip_toml_comment(m.group(2))
            current[key] = _parse_scalar(value)
    return root


def load_toml(path: Path) -> Dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    try:
        import tomllib  # py>=3.11

        return tomllib.loads(text)
    except ImportError:
        return _mini_toml(text)


# ---------------------------------------------------------------------------
# Mini-parser YAML (subconjunto: mapeamentos, listas de escalares, comentários)
# ---------------------------------------------------------------------------


def _yaml_scalar(raw: str) -> Any:
    raw = raw.strip()
    if raw in ("null", "~", ""):
        return None
    if raw in ("true", "false"):
        return raw == "true"
    if raw.startswith('"') and raw.endswith('"'):
        return json.loads(raw)
    if raw.startswith("'") and raw.endswith("'"):
        return raw[1:-1]
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def _mini_yaml(text: str) -> Any:
    lines: list[tuple[int, str]] = []
    for raw in text.splitlines():
        stripped = raw
        if "#" in stripped:
            # remove comentários fora de aspas
            out, in_q = [], None
            for ch in stripped:
                if ch in "\"'":
                    in_q = ch if in_q is None else (None if in_q == ch else in_q)
                if ch == "#" and in_q is None:
                    break
                out.append(ch)
            stripped = "".join(out)
        if not stripped.strip():
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        lines.append((indent, stripped.rstrip()))

    pos = 0

    def parse_block(indent: int) -> Any:
        nonlocal pos
        if pos >= len(lines):
            return None
        # lista?
        if lines[pos][1].lstrip().startswith("- "):
            items: list[Any] = []
            while pos < len(lines):
                ind, content = lines[pos]
                if ind != indent or not content.lstrip().startswith("- "):
                    break
                items.append(_yaml_scalar(content.lstrip()[2:]))
                pos += 1
            return items
        mapping: Dict[str, Any] = {}
        while pos < len(lines):
            ind, content = lines[pos]
            if ind < indent:
                break
            if ind > indent:
                raise ValueError(f"indentação inesperada: {content!r}")
            if ":" not in content:
                break
            key, _, rest = content.partition(":")
            key = key.strip().strip('"')
            rest = rest.strip()
            pos += 1
            if rest:
                mapping[key] = _yaml_scalar(rest)
            else:
                if pos < len(lines) and lines[pos][0] > indent:
                    mapping[key] = parse_block(lines[pos][0])
                elif pos < len(lines) and lines[pos][1].lstrip().startswith("- ") and lines[pos][0] >= indent:
                    mapping[key] = parse_block(lines[pos][0])
                else:
                    mapping[key] = None
        return mapping

    return parse_block(0) if lines else {}


def load_yaml(path: Path) -> Dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore

        return yaml.safe_load(text) or {}
    except ImportError:
        return _mini_yaml(text) or {}


# ---------------------------------------------------------------------------
# Localização da raiz do projeto
# ---------------------------------------------------------------------------


def find_project_root() -> Path:
    env = os.environ.get("BLENDER_MCP_ROOT")
    if env:
        return Path(env).resolve()
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "config" / "app.toml").exists():
            return parent
    return here.parents[3]


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def _get(d: dict, path: str, default=None) -> Any:
    cur: Any = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


@dataclass
class Settings:
    root: Path
    raw: Dict[str, Any] = field(default_factory=dict)

    # bridge
    bridge_host: str = "127.0.0.1"
    bridge_port: int = 8765
    token_file: Path = None  # type: ignore[assignment]
    queue_max: int = 16
    command_timeout_s: int = 120
    poll_interval_s: float = 0.35
    heartbeat_timeout_s: float = 15.0

    # server
    server_name: str = SERVER_NAME
    version: str = "0.1.0"
    log_level: str = "INFO"
    transport: str = "stdio"
    http_host: str = "127.0.0.1"
    http_port: int = 8766
    autostart_bridge: bool = True
    http_auth_token_env: str = "BLENDER_MCP_HTTP_TOKEN"
    http_token_file: Path = None  # type: ignore[assignment]
    http_max_body_bytes: int = 1_048_576

    # storage
    storage_root: Path = None  # type: ignore[assignment]
    projects_dir: Path = None  # type: ignore[assignment]
    checkpoints_dir: Path = None  # type: ignore[assignment]
    renders_dir: Path = None  # type: ignore[assignment]
    logs_dir: Path = None  # type: ignore[assignment]
    jobs_db: Path = None  # type: ignore[assignment]
    external_workspace: Path = None  # type: ignore[assignment]

    # gateway / runtimes
    gateway_cfg: Dict[str, Any] = field(default_factory=dict)
    external_runtimes: Dict[str, Any] = field(default_factory=dict)
    external_limits: Dict[str, Any] = field(default_factory=dict)

    # polices (providers.yaml / policies.yaml crus)
    providers_cfg: Dict[str, Any] = field(default_factory=dict)
    policies_cfg: Dict[str, Any] = field(default_factory=dict)

    @property
    def bridge_url(self) -> str:
        return f"http://{self.bridge_host}:{self.bridge_port}"

    def path(self, value: str | Path) -> Path:
        p = Path(value)
        return p if p.is_absolute() else (self.root / p)

    def ensure_dirs(self) -> None:
        for d in (
            self.storage_root,
            self.projects_dir,
            self.checkpoints_dir,
            self.renders_dir,
            self.logs_dir,
            self.external_workspace,
            self.token_file.parent,
        ):
            d.mkdir(parents=True, exist_ok=True)

    def bridge_token(self) -> str:
        """Lê (ou gera na primeira execução) o token local da Bridge.

        A criação é atômica (``O_CREAT|O_EXCL``): em corrida, quem perde a
        criação lê o token do vencedor em vez de gerar um divergente
        (BUG-024). Arquivo vazio remanescente é substituído por escrito
        atômica via ``os.replace``.
        """
        existing = self._read_token()
        if existing:
            return existing
        token = secrets.token_hex(32)
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.token_file,
                         os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                existing = self._read_token()
                if existing:
                    return existing
                time.sleep(0.01)
            return self._replace_token(token)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(token)
        try:  # restringe leitura ao usuário atual quando o SO suportar
            os.chmod(self.token_file, 0o600)
        except OSError:
            pass
        # Corrida residual (review F3): se outro escritor substituiu o
        # arquivo entre a criação exclusiva e a nossa escrita, o conteúdo
        # do arquivo é a verdade compartilhada — adote-o.
        return self._read_token() or token

    def _read_token(self) -> str:
        try:
            return self.token_file.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def _replace_token(self, token: str) -> str:
        """Substitui atomicamente um token vazio/divergente."""
        tmp = self.token_file.with_name(self.token_file.name + ".tmp")
        tmp.write_text(token, encoding="utf-8")
        os.replace(tmp, self.token_file)
        try:
            os.chmod(self.token_file, 0o600)
        except OSError:
            pass
        return token

    def http_auth_token(self, generate: bool = True) -> str:
        """Token Bearer do transporte HTTP MCP (BUG-032).

        Resolve nesta ordem: variável de ambiente → arquivo existente →
        geração exclusiva (somente quando ``generate``). Com
        ``generate=False`` apenas lê: o transporte usa isso para recusar
        binding fora de loopback sem autenticação configurada.
        """
        env = os.environ.get(self.http_auth_token_env, "").strip()
        if env:
            return env
        existing = self._read_http_token()
        if existing:
            return existing
        if not generate:
            return ""
        self.http_token_file.parent.mkdir(parents=True, exist_ok=True)
        # Criação exclusiva (mesmo padrão do Agent Gateway): em corrida,
        # quem perde o 'x' lê o token do vencedor.
        for _ in range(100):
            try:
                with self.http_token_file.open("x", encoding="utf-8") as fh:
                    fh.write(secrets.token_urlsafe(32))
            except FileExistsError:
                time.sleep(0.02)
                existing = self._read_http_token()
                if existing:
                    return existing
                continue
            break
        try:  # restringe leitura ao usuário atual quando o SO suportar
            os.chmod(self.http_token_file, 0o600)
        except OSError:
            pass
        existing = self._read_http_token()
        if existing:
            return existing
        # Resquício vazio (crash entre criar e escrever): substituição
        # atômica — nunca bloquear a subida do transporte HTTP (review F2).
        token = secrets.token_urlsafe(32)
        tmp = self.http_token_file.with_name(self.http_token_file.name + ".tmp")
        tmp.write_text(token, encoding="utf-8")
        os.replace(tmp, self.http_token_file)
        return self._read_http_token() or token

    def _read_http_token(self) -> str:
        try:
            return self.http_token_file.read_text(encoding="utf-8").strip()
        except OSError:
            return ""


def load_settings(root: Path | str | None = None,
                  ensure_dirs: bool = True) -> Settings:
    root = Path(root).resolve() if root else find_project_root()
    cfg_path = root / "config" / "app.toml"
    raw = load_toml(cfg_path) if cfg_path.exists() else {}
    if not cfg_path.exists():
        log.warning("config/app.toml não encontrado em %s; usando padrões.", root)

    s = Settings(root=root, raw=raw)
    b = _get(raw, "bridge", {})
    s.bridge_host = b.get("host", s.bridge_host)
    s.bridge_port = int(b.get("port", s.bridge_port))
    s.token_file = s.path(b.get("token_file", "storage/bridge_token"))
    s.queue_max = int(b.get("queue_max", s.queue_max))
    s.command_timeout_s = float(b.get("command_timeout_s", s.command_timeout_s))
    s.poll_interval_s = float(b.get("poll_interval_s", s.poll_interval_s))
    s.heartbeat_timeout_s = float(b.get("heartbeat_timeout_s", s.heartbeat_timeout_s))

    srv = _get(raw, "server", {})
    s.server_name = srv.get("name", s.server_name)
    s.version = srv.get("version", s.version)
    s.log_level = srv.get("log_level", s.log_level)
    s.transport = srv.get("transport", s.transport)
    s.http_host = srv.get("http_host", s.http_host)
    s.http_port = int(srv.get("http_port", s.http_port))
    s.autostart_bridge = bool(srv.get("autostart_bridge", s.autostart_bridge))
    s.http_auth_token_env = str(
        srv.get("http_auth_token_env", s.http_auth_token_env))
    s.http_token_file = s.path(
        srv.get("http_token_file", "storage/http_token"))
    s.http_max_body_bytes = int(
        srv.get("http_max_body_bytes", s.http_max_body_bytes))

    st = _get(raw, "storage", {})
    s.storage_root = s.path(st.get("root", "storage"))
    s.projects_dir = s.path(st.get("projects", "storage/projects"))
    s.checkpoints_dir = s.path(st.get("checkpoints", "storage/checkpoints"))
    s.renders_dir = s.path(st.get("renders", "storage/renders"))
    s.logs_dir = s.path(st.get("logs", "storage/logs"))
    s.jobs_db = s.path(st.get("jobs_db", "storage/jobs.db"))
    s.external_workspace = s.path(
        st.get("external_workspace", "storage/external_workspace")
    )

    s.gateway_cfg = _get(raw, "gateway", {})
    s.external_runtimes = _get(raw, "external_runtimes", {})
    s.external_limits = _get(raw, "external_runtime_limits", {})

    providers_path = root / "config" / "providers.yaml"
    if providers_path.exists():
        s.providers_cfg = load_yaml(providers_path)
    policies_path = root / "config" / "policies.yaml"
    if policies_path.exists():
        s.policies_cfg = load_yaml(policies_path)

    if ensure_dirs:
        s.ensure_dirs()
    return s
