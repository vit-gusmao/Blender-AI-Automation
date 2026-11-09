"""Integração MCP stdio: servidor real + cliente JSON-RPC (como um harness).

Requer o SDK `mcp` instalado (venv) — o servidor é spawnado como processo
real e falamos initialize/tools/list/tools/call por stdin/stdout.
Pula quando o SDK não está disponível.
"""
from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="SDK mcp não instalado no ambiente")
pytest.importorskip("pydantic")

REPO = Path(__file__).resolve().parents[2]


def _rpc(payload) -> bytes:
    return (json.dumps(payload) + "\n").encode("utf-8")


class StdioRPC:
    def __init__(self, env):
        self.proc = subprocess.Popen(  # noqa: S603 — nosso servidor
            [sys.executable, "-m", "universal_blender_mcp", "serve"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env)
        self.next_id = 0
        self._stdout_lines = queue.Queue()
        self._stderr_lines = []
        self._stderr_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._closed = False
        self._stdout_reader = threading.Thread(
            target=self._read_stdout, name="mcp-stdio-stdout", daemon=True)
        self._stderr_reader = threading.Thread(
            target=self._read_stderr, name="mcp-stdio-stderr", daemon=True)
        self._stdout_reader.start()
        self._stderr_reader.start()

    def _read_stdout(self):
        try:
            while True:
                line = self.proc.stdout.readline()
                if not line:
                    break
                self._stdout_lines.put(line)
        except (OSError, ValueError):
            pass
        finally:
            self._stdout_lines.put(None)

    def _read_stderr(self):
        try:
            while True:
                line = self.proc.stderr.readline()
                if not line:
                    break
                with self._stderr_lock:
                    self._stderr_lines.append(line)
                    del self._stderr_lines[:-200]
        except (OSError, ValueError):
            pass

    def stderr_tail(self) -> str:
        with self._stderr_lock:
            data = b"".join(self._stderr_lines)
        return data.decode("utf-8", "replace")[-2000:]

    def request(self, method, params=None, timeout_readback=1):
        try:
            self.next_id += 1
            rid = self.next_id
            self.proc.stdin.write(_rpc({"jsonrpc": "2.0", "id": rid,
                                        "method": method,
                                        "params": params or {}}))
            self.proc.stdin.flush()
            # lê linhas até a resposta com nosso id
            deadline = time.monotonic() + timeout_readback
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"stdio response deadline elapsed after "
                        f"{timeout_readback}s for {method}")
                try:
                    line = self._stdout_lines.get(timeout=remaining)
                except queue.Empty:
                    raise TimeoutError(
                        f"stdio response deadline elapsed after "
                        f"{timeout_readback}s for {method}") from None
                if line is None:
                    raise RuntimeError(
                        "servidor encerrou antes de responder; stderr:\n"
                        + self.stderr_tail())
                msg = json.loads(line.decode("utf-8"))
                if not isinstance(msg, dict):
                    raise ValueError("stdio JSON-RPC message must be an object")
                if msg.get("id") == rid:
                    return msg
        except BaseException:
            self.close()
            raise

    def notify(self, method, params=None):
        self.proc.stdin.write(_rpc({"jsonrpc": "2.0", "method": method,
                                    "params": params or {}}))
        self.proc.stdin.flush()

    def close(self):
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            try:
                self.proc.stdin.close()
            except (OSError, ValueError):
                pass
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=5)
            for pipe in (self.proc.stdout, self.proc.stderr):
                try:
                    pipe.close()
                except (OSError, ValueError):
                    pass
            for reader in (self._stdout_reader, self._stderr_reader):
                if reader is not threading.current_thread():
                    reader.join(timeout=5)


def test_silent_stdio_response_obeys_deadline_and_reaps_process(monkeypatch):
    """A child that never emits a newline cannot block the test harness."""
    import os
    import threading
    import time

    real_popen = subprocess.Popen

    def silent_child(*_args, **kwargs):
        return real_popen([
            sys.executable,
            "-c",
            "import sys,time; sys.stdin.buffer.readline(); time.sleep(30)",
        ], **kwargs)

    monkeypatch.setattr(subprocess, "Popen", silent_child)
    client = StdioRPC(os.environ.copy())
    outcome = {}
    started = time.monotonic()

    def request():
        try:
            client.request("silent/method", timeout_readback=0.15)
        except BaseException as exc:  # captured for assertion in test thread
            outcome["error"] = exc

    thread = threading.Thread(target=request, name="silent-stdio-request")
    thread.start()
    thread.join(timeout=0.75)
    missed_deadline = thread.is_alive()
    if missed_deadline:
        client.close()
        thread.join(timeout=2)
    elapsed = time.monotonic() - started
    try:
        assert not missed_deadline, "readline ignored timeout_readback"
        assert isinstance(outcome.get("error"), TimeoutError)
        assert elapsed < 0.75
        assert client.proc.poll() is not None
    finally:
        if client.proc.poll() is None:
            client.close()


def test_invalid_json_response_reaps_process_and_reader_threads(monkeypatch):
    """Malformed protocol output must close every resource before raising."""
    import os

    real_popen = subprocess.Popen

    def invalid_json_child(*_args, **kwargs):
        return real_popen([
            sys.executable,
            "-c",
            ("import sys,time; sys.stdin.buffer.readline(); "
             "sys.stdout.buffer.write(b'not-json\\n'); "
             "sys.stdout.buffer.flush(); time.sleep(30)"),
        ], **kwargs)

    monkeypatch.setattr(subprocess, "Popen", invalid_json_child)
    client = StdioRPC(os.environ.copy())
    try:
        with pytest.raises(json.JSONDecodeError):
            client.request("malformed/response", timeout_readback=1)
        assert client.proc.poll() is not None
        assert not client._stdout_reader.is_alive()
        assert not client._stderr_reader.is_alive()
    finally:
        client.close()


@pytest.fixture()
def rpc(tmp_root, monkeypatch):
    monkeypatch.setenv("BLENDER_MCP_ROOT", str(tmp_root))
    import os

    env = {**os.environ,
           "PYTHONPATH": str(REPO / "mcp_server" / "src")}
    client = None
    try:
        client = StdioRPC(env)
        init = client.request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "0"},
        }, timeout_readback=10)
        client.notify("notifications/initialized")
        yield client, init
    finally:
        if client is not None:
            client.close()


@pytest.fixture()
def queued_bridge_rpc(tmp_root):
    from universal_blender_mcp.bridge_gateway import BridgeGateway
    from universal_blender_mcp.bridge_server import BridgeServer
    from universal_blender_mcp.job_manager import JobManager

    token = "stdio-cancel-token"
    jobs = JobManager(tmp_root / "storage" / "jobs.db")
    bridge = BridgeServer("127.0.0.1", 0, token, jobs,
                          command_timeout_s=30.0)
    bridge.start(daemon=True)

    config_path = tmp_root / "config" / "app.toml"
    config = config_path.read_text(encoding="utf-8")
    config = config.replace("port = 8765", f"port = {bridge.port}", 1)
    config_path.write_text(config, encoding="utf-8")
    token_path = tmp_root / "storage" / "bridge_token"
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(token, encoding="utf-8")

    import os

    env = {**os.environ, "BLENDER_MCP_ROOT": str(tmp_root),
           "PYTHONPATH": str(REPO / "mcp_server" / "src")}
    client = None
    try:
        client = StdioRPC(env)
        init = client.request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "0"},
        }, timeout_readback=10)
        client.notify("notifications/initialized")
        yield client, init, BridgeGateway(bridge.url, token), jobs
    finally:
        if client is not None:
            client.close()
        bridge.stop()
        jobs.close()


def test_initialize_handshake(rpc):
    _client, init = rpc
    assert "result" in init
    server_info = init["result"]["serverInfo"]
    assert server_info["name"] == "universal-blender-mcp"


def test_tools_list_full_catalog(rpc):
    client, _ = rpc
    out = client.request("tools/list")
    tools = out["result"]["tools"]
    names = {t["name"] for t in tools}
    assert len(tools) >= 55
    for required in ("blender_ping", "object_create", "material_create",
                     "render_final", "python_execute_blender",
                     "job_get_status", "blender_get_capabilities"):
        assert required in names


def test_resources_list_and_templates_use_installed_sdk_shape(rpc):
    client, _ = rpc
    listed = client.request("resources/list")
    resources = listed["result"]["resources"]
    assert {item["uri"] for item in resources} == {
        "blender://runtime/info",
        "blender://scene/summary",
        "blender://scene/objects",
        "blender://materials",
        "blender://project/current",
    }
    assert all(item["mimeType"] == "application/json" for item in resources)

    templates = client.request("resources/templates/list")
    assert {item["uriTemplate"]
            for item in templates["result"]["resourceTemplates"]} == {
        "blender://object/{name}", "blender://jobs/{job_id}"}


def test_resources_read_returns_protocol_content_model(rpc):
    client, _ = rpc
    uri = "blender://jobs/missing-job"
    out = client.request("resources/read", {"uri": uri})

    assert "error" not in out, out
    contents = out["result"]["contents"]
    assert len(contents) == 1
    assert contents[0]["uri"] == uri
    assert contents[0]["mimeType"] == "application/json"
    payload = json.loads(contents[0]["text"])
    assert payload["success"] is False
    assert payload["error"]["type"] == "NotFound"


def test_resources_read_uses_equivalent_tool_authorization(rpc):
    client, _ = rpc
    out = client.request("resources/read", {"uri": "blender://materials"})

    assert "error" in out
    serialized = json.dumps(out, ensure_ascii=False)
    assert "advanced" in serialized
    assert "python_execute_blender" in serialized


def test_resources_read_unknown_uri_is_protocol_error(rpc):
    client, _ = rpc
    out = client.request("resources/read", {"uri": "blender://unknown"})

    assert "error" in out
    assert "recurso desconhecido" in json.dumps(out, ensure_ascii=False)


def test_resources_read_audits_reads_and_denials(rpc, tmp_root):
    """Leituras de recurso recebem auditoria equivalente à das ferramentas."""
    client, _ = rpc
    client.request("resources/read", {"uri": "blender://materials"})
    client.request("resources/read", {"uri": "blender://jobs/missing-job"})

    audit_path = tmp_root / "storage" / "logs" / "audit.jsonl"
    records = [json.loads(line) for line in
               audit_path.read_text(encoding="utf-8").splitlines()]
    denied = [r for r in records if r.get("event") == "resource_denied"]
    reads = [r for r in records if r.get("event") == "resource_read"]
    assert any(r.get("tool") == "python_execute_blender"
               and r.get("uri") == "blender://materials" for r in denied)
    assert any(r.get("tool") == "job_get_result"
               and r.get("uri") == "blender://jobs/missing-job" for r in reads)
    assert reads and "arguments" in reads[0]


def test_resources_read_materials_reaches_bridge_at_advanced_level(tmp_root):
    """F1: blender://materials não pode ser recurso morto.

    Em sessão advanced, a leitura do recurso (código 100% construído pelo
    servidor) atravessa o gate com a aprovação do servidor pelo código fixo
    e chega à Bridge; a falha sem Blender é BridgeUnavailable, NUNCA a
    negação de autorização. Em níveis inferiores a negação de nível se aplica.
    """
    import os

    cfg = tmp_root / "config" / "app.toml"
    text = cfg.read_text(encoding="utf-8")
    assert "autostart_bridge = true" in text
    cfg.write_text(text.replace("autostart_bridge = true",
                                "autostart_bridge = false"), encoding="utf-8")

    env = {**os.environ, "BLENDER_MCP_ROOT": str(tmp_root),
           "BLENDER_MCP_SESSION_LEVEL": "advanced",
           "PYTHONPATH": str(REPO / "mcp_server" / "src")}
    client = None
    try:
        client = StdioRPC(env)
        client.request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "0"},
        }, timeout_readback=10)
        client.notify("notifications/initialized")
        out = client.request("resources/read", {"uri": "blender://materials"},
                             timeout_readback=15)
        assert "error" not in out, out
        payload = json.loads(out["result"]["contents"][0]["text"])
        assert payload["error"]["type"] == "BridgeUnavailable", payload
    finally:
        if client is not None:
            client.close()


def test_call_capabilities_returns_envelope(rpc):
    client, _ = rpc
    out = client.request("tools/call", {
        "name": "blender_get_capabilities", "arguments": {}})
    text = out["result"]["content"][0]["text"]
    envelope = json.loads(text)
    assert envelope["success"] is True
    assert envelope["data"]["tool_count"] >= 55
    assert "transports" in envelope["data"]


def test_unknown_tool_structured_error(rpc):
    client, _ = rpc
    out = client.request("tools/call", {"name": "no_such_tool",
                                        "arguments": {}})
    envelope = json.loads(out["result"]["content"][0]["text"])
    assert envelope["success"] is False
    assert envelope["error"]["type"] == "UnknownTool"


def test_advanced_tool_denied_at_default_level(rpc):
    """python_execute_blender requer ADVANCED; sessão default é scene_write."""
    client, _ = rpc
    out = client.request("tools/call", {
        "name": "python_execute_blender",
        "arguments": {"code": "result = 1"}})
    envelope = json.loads(out["result"]["content"][0]["text"])
    assert envelope["success"] is False
    assert envelope["error"]["type"] == "Unauthorized"
    assert "advanced" in envelope["error"]["message"]


def test_invalid_arguments_rejected(rpc):
    client, _ = rpc
    out = client.request("tools/call", {"name": "object_create",
                                        "arguments": {}})
    envelope = json.loads(out["result"]["content"][0]["text"])
    assert envelope["error"]["type"] == "InvalidArguments"
    assert "type" in envelope["error"]["message"]


def test_jobs_tools_work_without_blender(rpc):
    client, _ = rpc
    out = client.request("tools/call", {
        "name": "job_get_status", "arguments": {"job_id": "inexistente"}})
    envelope = json.loads(out["result"]["content"][0]["text"])
    assert envelope["success"] is True  # consulta bem-sucedida
    assert envelope["data"]["found"] is False


def test_job_cancel_removes_queued_command_once_via_bridge(queued_bridge_rpc):
    from universal_blender_mcp.schemas import build_command

    client, _, gateway, _jobs = queued_bridge_rpc
    command_id = "stdio-cancel-target"
    submitted = gateway.submit(build_command(
        "object_create", {"type": "MESH", "name": "NeverExecuted"},
        command_id=command_id, timeout_s=30.0))
    assert submitted == {"ok": True, "command_id": command_id,
                         "status": "queued"}
    assert gateway.status()["queue_depth"] == 1

    first = client.request("tools/call", {
        "name": "job_cancel", "arguments": {"job_id": command_id}})
    first_envelope = json.loads(first["result"]["content"][0]["text"])
    assert first_envelope["success"] is True
    assert first_envelope["data"] == {
        "ok": True, "cancelled": True,
        "command_id": command_id, "status": "cancelled",
    }
    assert gateway.status()["queue_depth"] == 0
    first_job = gateway.get(command_id)["job"]
    assert first_job["status"] == "cancelled"
    assert first_job["result"]["command_id"] == command_id
    assert first_job["result"]["error"]["type"] == "Cancelled"

    repeated = client.request("tools/call", {
        "name": "job_cancel", "arguments": {"job_id": command_id}})
    repeated_envelope = json.loads(repeated["result"]["content"][0]["text"])
    assert repeated_envelope["data"]["cancelled"] is False
    assert repeated_envelope["data"]["reason"] == "job já está cancelled"
    repeated_job = gateway.get(command_id)["job"]
    assert repeated_job["finished_at"] == first_job["finished_at"]
    assert repeated_job["result"] == first_job["result"]


def test_job_cancel_offloads_bridge_io_from_event_loop():
    import asyncio
    import threading
    import time

    from universal_blender_mcp.tool_registry import ServerContext, ToolRegistry
    from universal_blender_mcp.tools import scene

    release = threading.Event()

    class BlockingBridge:
        def cancel(self, command_id):
            release.wait(timeout=0.2)
            return {"ok": True, "cancelled": True,
                    "command_id": command_id, "status": "cancelled"}

    registry = ToolRegistry()
    ctx = ServerContext(settings=None, bridge=BlockingBridge(), jobs=object(),
                        policy=None, audit=None)
    scene.register(ctx, registry)

    async def cancel_while_loop_ticks():
        asyncio.get_running_loop().call_later(0.01, release.set)
        started = time.monotonic()
        envelope = await registry.get("job_cancel").handler(
            {"job_id": "event-loop-cancel"})
        return time.monotonic() - started, envelope

    elapsed, envelope = asyncio.run(cancel_while_loop_ticks())
    assert elapsed < 0.1
    assert envelope["data"] == {
        "ok": True, "cancelled": True,
        "command_id": "event-loop-cancel", "status": "cancelled",
    }
