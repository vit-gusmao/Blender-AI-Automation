"""Fixtures e sys.path para toda a suíte (sem depender do venv)."""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for entry in (str(REPO_ROOT), str(REPO_ROOT / "mcp_server" / "src"),
              str(REPO_ROOT / "blender_addon")):
    if entry not in os.sys.path:
        os.sys.path.insert(0, entry)

os.environ.setdefault("BLENDER_MCP_ROOT", str(REPO_ROOT))

BLENDER_EXE_CANDIDATES = [
    os.environ.get("BLENDER_EXE", ""),
    r"C:\Blender\blender 3.6\blender.exe",
]


def find_blender() -> Path | None:
    for candidate in BLENDER_EXE_CANDIDATES:
        if candidate and Path(candidate).exists():
            return Path(candidate)
    return None


@pytest.fixture()
def tmp_root(tmp_path: Path) -> Path:
    """Raiz de projeto isolada: copia config/ e aponta storage para tmp."""
    (tmp_path / "config" / "clients").mkdir(parents=True)
    for pattern in ("app.toml", "providers.yaml", "policies.yaml"):
        src = REPO_ROOT / "config" / pattern
        if src.exists():
            shutil.copy(src, tmp_path / "config" / pattern)
    for name in ("codex.json", "cursor.json", "claude.json", "opencode.json"):
        src = REPO_ROOT / "config" / "clients" / name
        if src.exists():
            shutil.copy(src, tmp_path / "config" / "clients" / name)
    (tmp_path / "prompts" / "skills").mkdir(parents=True)
    for src in (REPO_ROOT / "prompts").rglob("*.md"):
        shutil.copy(src, tmp_path / "prompts" / src.relative_to(REPO_ROOT / "prompts"))
    return tmp_path


@pytest.fixture()
def settings(tmp_root: Path):
    from universal_blender_mcp.settings import load_settings

    return load_settings(tmp_root)


class FakeBlender(threading.Thread):
    """Simula o addon após registro explícito e só executa handlers declarados."""

    def __init__(self, base_url: str, token: str,
                 handlers: dict | None = None):
        super().__init__(daemon=True, name="fake-blender")
        self.base_url = base_url
        self.token = token
        self.handlers = handlers or {}
        self.stop_event = threading.Event()
        self.executed: list[str] = []
        self.registered = False

    def register(self):
        """Registra capacidades antes de permitir o início do poller."""
        if self.is_alive():
            raise RuntimeError("cannot register a running FakeBlender")
        out = self._request("POST", "/register", {
            "blender_version": "3.6.2 (fake)",
            "python_version": "3.10.fake",
            "platform": "pytest",
            "capabilities": sorted(self.handlers),
        })
        if not out.get("blender_id"):
            raise RuntimeError(f"FakeBlender registration failed: {out}")
        self.registered = True
        return out

    def start(self):
        if not self.registered:
            raise RuntimeError("FakeBlender.register() is required before start()")
        super().start()

    def _request(self, method: str, path: str, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.base_url + path, data=data,
                                     method=method)
        req.add_header("X-Bridge-Token", self.token)
        if data:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())

    def run(self):
        while not self.stop_event.is_set():
            try:
                out = self._request("GET", "/poll?wait=1")
                command = out.get("command")
                if command:
                    self.executed.append(command["operation"])
                    handler = self.handlers.get(command["operation"])
                    try:
                        if handler is None:
                            raise LookupError(
                                "unsupported fake operation: "
                                f"{command['operation']}")
                        data = handler(command["arguments"])
                        envelope = {
                            "command_id": command["command_id"],
                            "status": "completed", "success": True,
                            "data": data, "scene_revision": 1, "error": None,
                            "final": True,
                        }
                    except Exception as exc:  # noqa: BLE001
                        error_type = ("UnsupportedOperation"
                                      if handler is None
                                      else type(exc).__name__)
                        envelope = {
                            "command_id": command["command_id"],
                            "status": "failed", "success": False, "data": None,
                            "scene_revision": None,
                            "error": {"type": error_type,
                                      "message": str(exc)},
                            "final": True,
                        }
                    self._request("POST", "/results", envelope)
            except OSError:
                self.stop_event.wait(0.2)

    def stop(self):
        self.stop_event.set()
        self.join(timeout=5)


@pytest.fixture()
def bridge_env(tmp_root: Path):
    """Bridge real em porta efêmera + FakeBlender conectado."""
    from universal_blender_mcp.bridge_server import BridgeServer
    from universal_blender_mcp.job_manager import JobManager

    jobs = JobManager(tmp_root / "jobs.db")
    token = "test-token-123"
    server = BridgeServer("127.0.0.1", 0, token, jobs, queue_max=8,
                          command_timeout_s=5.0, heartbeat_timeout_s=10.0)
    server.start(daemon=True)
    url = server.url
    fake = FakeBlender(url, token, handlers={
        "blender_ping": lambda args: {"pong": True,
                                      "blender_version": "3.6.2 (fake)"},
        "scene_get_info": lambda args: {"scene_name": "Scene", "object_count": 0},
        "object_create": lambda args: {"object_name": args.get("name", "X"),
                                       "object_type": args.get("type")},
    })
    fake.register()
    fake.start()
    deadline = time.time() + 5
    while not fake.registered and time.time() < deadline:
        time.sleep(0.05)

    yield {"server": server, "url": url, "token": token, "fake": fake,
           "jobs": jobs}

    fake.stop()
    server.stop()
