"""Integração: BridgeGateway (lado servidor MCP) contra a Bridge real."""
from __future__ import annotations

import time
import threading
import json
import multiprocessing
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from universal_blender_mcp.bridge_gateway import (BridgeGateway,
                                                  BridgeUnavailable)
from universal_blender_mcp.bridge_server import BridgeServer
from universal_blender_mcp.job_manager import JobManager
from universal_blender_mcp.schemas import ResultEnvelope, build_command


_REAL_GETADDRINFO = socket.getaddrinfo


def _slow_getaddrinfo(host, port, *args, **kwargs):
    time.sleep(0.25)
    return _REAL_GETADDRINFO("127.0.0.1", port, *args, **kwargs)


def _multiple_getaddrinfo(host, port, *args, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", port)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.2", port))]


def _crashing_getaddrinfo(*args, **kwargs):
    raise SystemExit(7)


@pytest.fixture()
def idle_bridge(tmp_path):
    jobs = JobManager(tmp_path / "jobs.db")
    server = BridgeServer("127.0.0.1", 0, "tok", jobs, command_timeout_s=30)
    server.start()
    try:
        yield server
    finally:
        server.stop()
        jobs.close()


def test_execute_sync_roundtrip(bridge_env):
    gateway = BridgeGateway(bridge_env["url"], bridge_env["token"],
                            command_timeout_s=8, poll_interval_s=0.05)
    envelope = gateway.execute_sync(
        "object_create", {"type": "CUBE", "name": "GW_Cube"})
    assert envelope["success"] is True
    assert envelope["status"] == "completed"
    assert envelope["data"]["object_name"] == "GW_Cube"
    assert envelope["command_id"].startswith("cmd-")


def test_ping_and_status(bridge_env):
    gateway = BridgeGateway(bridge_env["url"], bridge_env["token"])
    ping = gateway.execute_sync("blender_ping", {})
    assert ping["data"]["pong"] is True
    status = gateway.status()
    assert status["blender_connected"] is True
    info = gateway.blender_info()
    assert info["blender"]["blender_version"] == "3.6.2 (fake)"


def test_unavailable_bridge_returns_structured_envelope(tmp_path):
    gateway = BridgeGateway("http://127.0.0.1:59999", "tok",
                            request_timeout_s=1)
    envelope = gateway.execute_sync("blender_ping", {})
    assert envelope["success"] is False
    assert envelope["error"]["type"] == "BridgeUnavailable"
    assert gateway.is_reachable() is False


def test_wait_for_result_timeout(bridge_env):
    """Comando submetido mas nunca executado (sem consumidor) → envelope
    Timeout do gateway (não exceção)."""
    gateway = BridgeGateway(bridge_env["url"], bridge_env["token"],
                            command_timeout_s=0.5, poll_interval_s=0.05)
    # derruba o consumidor para o comando ficar preso na fila
    bridge_env["fake"].stop()
    time.sleep(0.1)
    envelope = gateway.execute_sync("blender_ping", {})
    assert envelope["success"] is False
    assert envelope["error"]["type"] == "Timeout"


def test_gateway_timeout_removes_queued_work_before_returning(idle_bridge):
    gateway = BridgeGateway(idle_bridge.url, "tok", command_timeout_s=0.08,
                            poll_interval_s=0.01)
    result = gateway.execute_sync("object_create", command_id="queued-timeout")
    assert result["error"]["type"] == "Timeout"
    assert idle_bridge.state.queue == []
    assert idle_bridge.state.jobs.get("queued-timeout")["error"]["type"] == "Timeout"
    assert idle_bridge.state.poll_command(0) is None


def test_gateway_timeout_terminates_active_work_and_rejects_late_result(idle_bridge):
    gateway = BridgeGateway(idle_bridge.url, "tok", command_timeout_s=0.08,
                            poll_interval_s=0.01)
    dispatched = []

    def consume():
        dispatched.append(idle_bridge.state.poll_command(1))

    consumer = threading.Thread(target=consume)
    consumer.start()
    result = gateway.execute_sync("object_create", command_id="active-timeout")
    consumer.join(timeout=1)
    assert dispatched[0]["command_id"] == "active-timeout"
    assert result["error"]["type"] == "Timeout"
    assert idle_bridge.state.jobs.get("active-timeout")["status"] == "failed"
    assert idle_bridge.state.active_command is None
    assert idle_bridge.state.submit_result(ResultEnvelope.ok("active-timeout").to_dict()) is False


def test_post_submit_transport_failure_preserves_command_id(idle_bridge, monkeypatch):
    gateway = BridgeGateway(idle_bridge.url, "tok")

    def lost_connection(*args, **kwargs):
        raise BridgeUnavailable("connection lost after submission")

    monkeypatch.setattr(gateway, "get", lost_connection)
    result = gateway.execute_sync("object_create", command_id="submitted")
    assert idle_bridge.state.jobs.get("submitted") is not None
    assert result["command_id"] == "submitted"
    assert result["error"]["type"] == "BridgeUnavailable"


def test_post_submit_non_object_http_error_preserves_command_id(idle_bridge, monkeypatch):
    def invalid_error(handler):
        handler._send(503, ["proxy unavailable"])

    monkeypatch.setattr(idle_bridge.httpd.RequestHandlerClass, "do_GET", invalid_error)
    gateway = BridgeGateway(idle_bridge.url, "tok")
    result = gateway.execute_sync("object_create", command_id="proxy-error")
    assert idle_bridge.state.jobs.get("proxy-error") is not None
    assert result["command_id"] == "proxy-error"
    assert result["error"]["type"] == "BridgeUnavailable"


def test_post_submit_truncated_response_preserves_command_id(idle_bridge, monkeypatch):
    def truncated_response(handler):
        handler.send_response(200)
        handler.send_header("Content-Length", "100")
        handler.end_headers()
        handler.wfile.write(b"{")
        handler.close_connection = True

    monkeypatch.setattr(idle_bridge.httpd.RequestHandlerClass, "do_GET", truncated_response)
    gateway = BridgeGateway(idle_bridge.url, "tok")
    result = gateway.execute_sync("object_create", command_id="truncated")
    assert idle_bridge.state.jobs.get("truncated") is not None
    assert result["command_id"] == "truncated"
    assert result["error"]["type"] == "BridgeUnavailable"


def test_status_probe_rejects_non_bridge_server():
    class OtherService(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok": true, "bridge": "another-service"}')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), OtherService)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        gateway = BridgeGateway(f"http://127.0.0.1:{server.server_port}", "tok")
        with pytest.raises(BridgeUnavailable):
            gateway.status()
        assert gateway.is_reachable() is False
    finally:
        server.shutdown()
        server.server_close()


def test_pending_replay_waits_for_durable_result_instead_of_fabricating_success(idle_bridge):
    idle_bridge.state.enqueue(build_command("object_create", command_id="original",
                                            idempotency_key="pending"))
    gateway = BridgeGateway(idle_bridge.url, "tok", command_timeout_s=0.08,
                            poll_interval_s=0.01)
    result = gateway.execute_sync("object_create", idempotency_key="pending")
    assert result["success"] is False
    assert result["command_id"] == "original"
    assert result["error"]["type"] == "Timeout"
    assert idle_bridge.state.queue == []


def test_gateway_replay_returns_exact_persisted_result_after_restart(tmp_path):
    db = tmp_path / "jobs.db"
    jobs = JobManager(db)
    original = build_command("object_create", command_id="persisted", idempotency_key="durable")
    jobs.create(original)
    expected = ResultEnvelope.fail("persisted", "OperationError", "failed once").to_dict()
    assert jobs.set_result("persisted", expected)
    jobs.close()
    jobs = JobManager(db)
    server = BridgeServer("127.0.0.1", 0, "tok", jobs)
    server.start()
    try:
        gateway = BridgeGateway(server.url, "tok")
        assert gateway.execute_sync("object_create", idempotency_key="durable") == expected
        assert server.state.queue == []
    finally:
        server.stop()
        jobs.close()


def test_submit_and_poll_share_one_absolute_deadline(idle_bridge, monkeypatch):
    requests = []
    gateway = BridgeGateway(idle_bridge.url, "tok", command_timeout_s=0.1,
                            poll_interval_s=0.01, request_timeout_s=5)
    exchange = gateway._exchange

    def observe_request(request, timeout, expires, path):
        requests.append((request, timeout, time.time()))
        response = exchange(request, timeout, expires, path)
        if request.full_url.endswith("/commands"):
            time.sleep(0.04)
        return response

    monkeypatch.setattr(gateway, "_exchange", observe_request)
    result = gateway.execute_sync("object_create", command_id="absolute")
    assert result["error"]["type"] == "Timeout"
    submitted = json.loads(requests[0][0].data)
    assert submitted["deadline_at"] - submitted["created_at"] == pytest.approx(0.1, abs=1e-6)
    work_requests = [entry for entry in requests if not entry[0].full_url.endswith("/expire")]
    assert any(entry[0].method == "GET" for entry in work_requests)
    for _, timeout, started in work_requests:
        assert 0 < timeout <= submitted["deadline_at"] - started + 0.003


def test_terminal_replay_without_result_does_not_fabricate_success(idle_bridge):
    idle_bridge.state.jobs.create(build_command("op", command_id="missing-result",
                                               idempotency_key="missing"))
    assert idle_bridge.state.jobs.set_status("missing-result", "completed")
    gateway = BridgeGateway(idle_bridge.url, "tok")
    result = gateway.execute_sync("op", idempotency_key="missing")
    assert result["command_id"] == "missing-result"
    assert result["success"] is False
    assert result["error"]["type"] == "ResultUnavailable"


def test_running_replay_with_progress_waits_for_terminal_result(idle_bridge):
    idle_bridge.state.enqueue(build_command("op", command_id="progress",
                                            idempotency_key="progress"))
    idle_bridge.state.poll_command(0)
    progress = ResultEnvelope.ok("progress", {"progress": 0.5}).to_dict()
    progress["status"] = "running"
    assert idle_bridge.state.submit_result(progress)
    gateway = BridgeGateway(idle_bridge.url, "tok", command_timeout_s=0.08,
                            poll_interval_s=0.01)
    result = gateway.execute_sync("op", idempotency_key="progress")
    assert result["status"] == "failed"
    assert result["error"]["type"] == "Timeout"


@pytest.mark.parametrize("status,slow_headers", [(200, False), (503, False), (200, True)])
def test_slow_drip_request_obeys_total_deadline(idle_bridge, monkeypatch, status, slow_headers):
    def slow_response(handler):
        try:
            if slow_headers:
                for byte in b"HTTP/1.0 200 OK\r\nContent-Length: 11\r\n\r\n":
                    handler.wfile.write(bytes([byte]))
                    handler.wfile.flush()
                    time.sleep(0.04)
            else:
                handler.send_response(status)
                handler.send_header("Content-Length", "11")
                handler.end_headers()
            for byte in b'{"ok":true}':
                handler.wfile.write(bytes([byte]))
                handler.wfile.flush()
                time.sleep(0.04)
        except OSError:
            pass

    monkeypatch.setattr(idle_bridge.httpd.RequestHandlerClass, "do_GET", slow_response)
    gateway = BridgeGateway(idle_bridge.url, "tok", request_timeout_s=1)
    started = time.monotonic()
    with pytest.raises(BridgeUnavailable):
        gateway._request("GET", "/status", deadline_at=time.time() + 0.06)
    assert time.monotonic() - started < 0.18


def test_slow_drip_reconciliation_obeys_total_budget(idle_bridge, monkeypatch):
    def slow_expiration(handler):
        handler._body()
        handler.send_response(200)
        handler.send_header("Content-Length", "11")
        handler.end_headers()
        try:
            for byte in b'{"ok":true}':
                handler.wfile.write(bytes([byte]))
                handler.wfile.flush()
                time.sleep(0.04)
        except OSError:
            pass

    monkeypatch.setattr(idle_bridge.httpd.RequestHandlerClass, "do_POST", slow_expiration)
    gateway = BridgeGateway(idle_bridge.url, "tok", request_timeout_s=0.06)
    started = time.monotonic()
    result = gateway._timeout_result("expired")
    assert time.monotonic() - started < 0.18
    assert result["command_id"] == "expired"
    assert result["error"]["type"] == "Timeout"


@pytest.mark.parametrize("replay", [False, True])
def test_result_received_after_deadline_is_not_accepted(idle_bridge, monkeypatch, replay):
    gateway = BridgeGateway(idle_bridge.url, "tok", command_timeout_s=0.03)
    terminal = ResultEnvelope.ok("late-success").to_dict()

    def late_response(*args, **kwargs):
        time.sleep(0.05)
        if replay:
            return {"ok": True, "command_id": "late-success", "idempotent_replay": True,
                    "status": "completed", "result": terminal}
        return {"job": {"status": "completed", "result": terminal}, "result": terminal}

    monkeypatch.setattr(gateway, "submit" if replay else "get", late_response)
    result = gateway.execute_sync("op", command_id="late-success")
    assert result["success"] is False
    assert result["error"]["type"] == "Timeout"


def test_header_slow_drip_leaves_no_workers_or_live_connections(idle_bridge, monkeypatch):
    stop = threading.Event()
    disconnected = []

    def endless_header(handler):
        closed = threading.Event()
        disconnected.append(closed)
        handler.close_connection = True
        try:
            handler.wfile.write(b"HTTP/1.0 200 OK\r\nX-Slow: ")
            handler.wfile.flush()
            while not stop.wait(0.01):
                handler.wfile.write(b"x")
                handler.wfile.flush()
        except OSError:
            pass
        finally:
            closed.set()

    monkeypatch.setattr(idle_bridge.httpd.RequestHandlerClass, "do_GET", endless_header)
    gateway = BridgeGateway(idle_bridge.url, "tok", request_timeout_s=1)
    try:
        for _ in range(3):
            with pytest.raises(BridgeUnavailable):
                gateway._request("GET", "/status", deadline_at=time.time() + 0.05)
        # A deadline must release the network transport, not just abandon a
        # daemon that remains blocked while the peer keeps the headers open.
        assert not [thread for thread in threading.enumerate() if thread.name == "bridge-request"]
        assert len(disconnected) == 3
        assert all(closed.wait(0.2) for closed in disconnected)
    finally:
        stop.set()
        for closed in disconnected:
            closed.wait(1)


@pytest.mark.parametrize("reconcile", [False, True])
def test_slow_resolution_obeys_total_budget_without_abandoned_resources(idle_bridge, monkeypatch, reconcile):
    monkeypatch.setattr(socket, "getaddrinfo", _slow_getaddrinfo)
    gateway = BridgeGateway(idle_bridge.url.replace("127.0.0.1", "slow.test"), "tok",
                            request_timeout_s=0.04)
    started = time.monotonic()
    if reconcile:
        assert gateway._timeout_result("resolve-timeout")["error"]["type"] == "Timeout"
    else:
        with pytest.raises(BridgeUnavailable):
            gateway.status()
    assert time.monotonic() - started < 0.18
    assert not [thread for thread in threading.enumerate() if thread.name == "bridge-request"]
    assert not [child for child in multiprocessing.active_children()
                if child.name.startswith("bridge-resolver")]


def test_multiple_connect_attempts_share_remaining_budget(idle_bridge, monkeypatch):
    sockets = []

    class FailingSocket:
        def __init__(self, *args, **kwargs):
            self.closed = False
            sockets.append(self)

        def settimeout(self, timeout):
            self.timeout = timeout

        def connect(self, address):
            time.sleep(self.timeout)
            raise TimeoutError("connect spent its remaining budget")

        def close(self):
            self.closed = True

    monkeypatch.setattr(socket, "getaddrinfo", _multiple_getaddrinfo)
    monkeypatch.setattr(socket, "socket", FailingSocket)
    gateway = BridgeGateway(idle_bridge.url.replace("127.0.0.1", "multiple.test"), "tok",
                            request_timeout_s=1.0)
    started = time.monotonic()
    with pytest.raises(BridgeUnavailable):
        gateway.status()
    assert time.monotonic() - started < 1.2
    assert sockets and all(sock.closed for sock in sockets)


def test_hostname_resolution_roundtrip_reaps_resolver(idle_bridge):
    gateway = BridgeGateway(idle_bridge.url.replace("127.0.0.1", "localhost"), "tok",
                            request_timeout_s=3)
    assert gateway.status()["bridge"] == "universal-blender-mcp/bridge"
    assert not [child for child in multiprocessing.active_children()
                if child.name.startswith("bridge-resolver")]


def test_resolver_process_failure_is_structured_and_reaped(idle_bridge, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _crashing_getaddrinfo)
    gateway = BridgeGateway(idle_bridge.url.replace("127.0.0.1", "crash.test"), "tok",
                            request_timeout_s=3)
    with pytest.raises(BridgeUnavailable):
        gateway.status()
    assert not [child for child in multiprocessing.active_children()
                if child.name.startswith("bridge-resolver")]
