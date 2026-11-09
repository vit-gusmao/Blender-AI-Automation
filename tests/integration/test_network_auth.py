"""Integração: autenticação e limites das entradas de rede (BUG-032).

Lado MCP HTTP com servidor real (subprocesso + HTTP cru) e a integração
combinada com o Agent Gateway. A metade do Gateway foi aprovada na Fase 4;
estes testes cobrem o lado MCP e a combinação. O transporte stdio NÃO tem
bearer: seu nível de sessão continua sendo a fronteira (test_mcp_stdio).
"""
from __future__ import annotations

import http.client
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="SDK mcp não instalado no ambiente")

REPO = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _post(port: int, path: str, body: bytes, headers=None, timeout: float = 8.0):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("POST", path, body=body,
                     headers={"Content-Type": "application/json",
                              "Accept": "application/json, text/event-stream",
                              **(headers or {})})
        resp = conn.getresponse()
        return resp.status, resp.read(2048)
    finally:
        conn.close()


_INIT_BODY = json.dumps({
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2024-11-05", "capabilities": {},
               "clientInfo": {"name": "pytest-network", "version": "0"}},
}).encode("utf-8")


class HttpServer:
    """Servidor MCP HTTP real em subprocesso, com raiz isolada."""

    def __init__(self, tmp_root: Path, *, http_host: str = "127.0.0.1"):
        self.tmp_root = tmp_root
        self.port = _free_port()
        cfg_path = tmp_root / "config" / "app.toml"
        cfg = cfg_path.read_text(encoding="utf-8")
        cfg = cfg.replace("http_port = 8766", f"http_port = {self.port}", 1)
        cfg = cfg.replace("http_host = \"127.0.0.1\"",
                          f"http_host = \"{http_host}\"", 1)
        cfg_path.write_text(cfg, encoding="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "universal_blender_mcp", "serve",
             "--transport", "http"],
            env={**os.environ, "BLENDER_MCP_ROOT": str(tmp_root),
                 "PYTHONPATH": str(REPO / "mcp_server" / "src")},
            cwd=str(REPO), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def wait_ready(self, timeout: float = 20.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                return False
            try:
                status, _ = _post(self.port, "/mcp", _INIT_BODY, timeout=2.0)
                if status in (200, 401):
                    return True
            except OSError:
                time.sleep(0.25)
        return False

    def token(self) -> str:
        return (self.tmp_root / "storage" / "http_token").read_text(
            encoding="utf-8").strip()

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        try:
            if self.proc.stdout:
                self.proc.stdout.close()
        except OSError:
            pass


@pytest.fixture()
def http_server(tmp_root):
    server = HttpServer(tmp_root)
    try:
        assert server.wait_ready(), "servidor HTTP não subiu"
        yield server
    finally:
        server.stop()


def _bearer(token: str):
    return {"Authorization": f"Bearer {token}"}


def test_http_rejects_missing_token(http_server):
    status, _ = _post(http_server.port, "/mcp", _INIT_BODY)
    assert status == 401


def test_http_rejects_wrong_token(http_server):
    status, _ = _post(http_server.port, "/mcp", _INIT_BODY, _bearer("token-errado"))
    assert status == 401


def test_http_accepts_correct_token(http_server):
    status, body = _post(http_server.port, "/mcp", _INIT_BODY,
                         _bearer(http_server.token()))
    assert status == 200, body
    result = json.loads(body)
    assert result["result"]["serverInfo"]["name"] == "universal-blender-mcp"


def test_http_rejects_oversized_body(http_server):
    token = http_server.token()
    huge = json.dumps({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "blender_ping",
                   "arguments": {"pad": "x" * (2_000_000)}},
    }).encode("utf-8")
    assert len(huge) > 1_048_576
    status, _ = _post(http_server.port, "/mcp", huge, _bearer(token),
                      timeout=15.0)
    assert status == 413


def test_http_rejects_non_object_json(http_server):
    status, body = _post(http_server.port, "/mcp", b"[1,2,3]",
                         _bearer(http_server.token()))
    assert status == 400


def _refusal_probe(tmp_root: Path, http_host: str) -> subprocess.Popen:
    port = _free_port()
    cfg_path = tmp_root / "config" / "app.toml"
    cfg = cfg_path.read_text(encoding="utf-8")
    cfg = cfg.replace("http_port = 8766", f"http_port = {port}", 1)
    cfg = cfg.replace('http_host = "127.0.0.1"', f'http_host = "{http_host}"', 1)
    cfg_path.write_text(cfg, encoding="utf-8")
    return subprocess.Popen(
        [sys.executable, "-m", "universal_blender_mcp", "serve",
         "--transport", "http"],
        env={**os.environ, "BLENDER_MCP_ROOT": str(tmp_root),
             "PYTHONPATH": str(REPO / "mcp_server" / "src")},
        cwd=str(REPO), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def test_non_loopback_binding_without_token_refused(tmp_root):
    """0.0.0.0 sem token configurado (arquivo ausente) não pode subir."""
    proc = _refusal_probe(tmp_root, "0.0.0.0")
    try:
        exited = proc.poll()
        deadline = time.monotonic() + 15.0
        while exited is None and time.monotonic() < deadline:
            time.sleep(0.25)
            exited = proc.poll()
        assert exited is not None, "servidor aceitou binding não-loopback sem token"
        output = proc.stdout.read().decode("utf-8", "replace") if proc.stdout else ""
        assert "loopback" in output.lower() or "token" in output.lower(), output[-800:]
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


def test_non_loopback_binding_with_configured_token_serves(tmp_root):
    """0.0.0.0 COM token pré-configurado sobe e autentica."""
    token_file = tmp_root / "storage" / "http_token"
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text("token-configurado-manualmente", encoding="utf-8")

    port = _free_port()
    cfg_path = tmp_root / "config" / "app.toml"
    cfg = cfg_path.read_text(encoding="utf-8")
    cfg = cfg.replace("http_port = 8766", f"http_port = {port}", 1)
    cfg = cfg.replace('http_host = "127.0.0.1"', 'http_host = "0.0.0.0"', 1)
    cfg_path.write_text(cfg, encoding="utf-8")

    proc = subprocess.Popen(
        [sys.executable, "-m", "universal_blender_mcp", "serve",
         "--transport", "http"],
        env={**os.environ, "BLENDER_MCP_ROOT": str(tmp_root),
             "PYTHONPATH": str(REPO / "mcp_server" / "src")},
        cwd=str(REPO), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 20.0
        ready = False
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            try:
                status, _ = _post(port, "/mcp", _INIT_BODY,
                                  _bearer("token-configurado-manualmente"),
                                  timeout=2.0)
                if status == 200:
                    ready = True
                    break
            except OSError:
                time.sleep(0.25)
        assert ready, "binding não-loopback com token deve servir"
        status, _ = _post(port, "/mcp", _INIT_BODY)
        assert status == 401
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        try:
            if proc.stdout:
                proc.stdout.close()
        except OSError:
            pass


def test_combined_gateway_and_mcp_http_reject_unauthenticated(tmp_root):
    """Integração combinada: Gateway e MCP HTTP negam requisições anônimas."""
    # MCP HTTP real (loopback) — token auto-gerado.
    server = HttpServer(tmp_root)
    gateway = None
    try:
        assert server.wait_ready()
        status, _ = _post(server.port, "/mcp", _INIT_BODY)
        assert status == 401
        status, _ = _post(server.port, "/mcp", _INIT_BODY,
                          _bearer(server.token()))
        assert status == 200

        # Agent Gateway real (lado aprovado na Fase 4) na mesma integração.
        import secrets as _secrets

        from agent_gateway.orchestrator import (GatewayHTTPServer,
                                                Orchestrator, make_handler)

        token_env = "BLENDER_GATEWAY_TOKEN"
        os.environ[token_env] = "gateway-token-combinado"
        try:
            orchestrator = Orchestrator(tmp_root)
            gateway = GatewayHTTPServer(
                ("127.0.0.1", 0), make_handler(orchestrator),
                orchestrator.max_http_clients)
            gateway_port = gateway.server_address[1]
            thread = threading.Thread(target=gateway.serve_forever,
                                      name="gateway-combined", daemon=True)
            thread.start()

            def get(path, token):
                conn = http.client.HTTPConnection("127.0.0.1", gateway_port,
                                                  timeout=8.0)
                try:
                    conn.request("GET", path, headers=(
                        {"Authorization": f"Bearer {token}"} if token else {}))
                    resp = conn.getresponse()
                    return resp.status, resp.read(65536)
                finally:
                    conn.close()

            status, _ = get("/health", None)
            assert status == 401
            status, _ = get("/health", _secrets.token_hex(8))
            assert status == 401
            status, body = get("/health", "gateway-token-combinado")
            assert status == 200
            assert json.loads(body)["ok"] is True
        finally:
            os.environ.pop(token_env, None)
    finally:
        server.stop()
        if gateway is not None:
            gateway.shutdown()
            gateway.server_close()
