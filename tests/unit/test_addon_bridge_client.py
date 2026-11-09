import threading
import urllib.error

import pytest

from universal_mcp.command_queue import CommandQueue
from universal_mcp import bridge_client


def test_outbox_preserves_all_results_and_requires_ack():
    q = CommandQueue(maxsize=1)
    first, second = {"command_id": "a"}, {"command_id": "b"}
    q.push_result(first)
    q.push_result(second)
    assert q.peek_result() == first
    q.requeue_result(first, "offline")
    assert q.peek_result() == first
    assert q.ack_result(first)
    assert q.peek_result() == second
    assert q.result_diagnostics["overflow"] == 1


@pytest.mark.parametrize("code", [500, 401])
def test_failed_delivery_retains_result(code):
    q = CommandQueue()
    q.push_result({"command_id": "a"})
    client = bridge_client.BridgeClient("localhost", 1, "token", queue=q)
    def fail(*args):
        raise urllib.error.HTTPError("test", code, "failure", {}, None)
    client._request = fail
    client._drain_results()
    assert q.peek_result()["command_id"] == "a"
    assert client.results_sent == 0
    client._request = lambda *args: {"accepted": True}
    client._drain_results(now=float("inf"))
    assert q.peek_result() is None
    assert client.results_sent == 1


def test_registration_precedes_poll_and_uses_captured_payload():
    client = bridge_client.BridgeClient("localhost", 1, "token",
                                        registration_payload={"blender_version": "3.6"})
    calls = []
    def request(method, path, payload=None):
        calls.append(path)
        if path == "/register":
            return {"blender_id": "instance"}
        client.stop()
        return {"command": None}
    client._request = request
    client.run()
    assert calls[0] == "/register"
    assert calls[1].startswith("/poll")


def test_wrong_registration_shape_is_rejected():
    client = bridge_client.BridgeClient("localhost", 1, "token")
    client._request = lambda *args: []
    assert client.register() is False
    assert client.connected is False


def test_shutdown_joins_and_preserves_queue():
    client = bridge_client.BridgeClient("localhost", 1, "token")
    client.run = lambda: client.stop_event.wait()
    client.queue.push_result({"command_id": "unsent"})
    bridge_client._client = client
    client.start()
    bridge_client.stop_client(join_timeout=1)
    assert not client.is_alive()
    assert bridge_client._retained_queue.peek_result()["command_id"] == "unsent"


def test_empty_ack_does_not_discard_completed_result():
    q = CommandQueue()
    result = {"command_id": "keep"}
    q.push_result(result)
    client = bridge_client.BridgeClient("localhost", 1, "token", queue=q)
    client._request = lambda *args: {}
    client._drain_results()
    assert q.peek_result() is result


def test_queue_full_rejection_is_a_complete_envelope():
    q = CommandQueue(maxsize=1)
    q.push_command({"command_id": "existing"})
    client = bridge_client.BridgeClient("localhost", 1, "token", queue=q)
    client._registered = True
    def request(method, path, payload=None):
        if path.startswith("/poll"):
            client.stop()
            return {"command": {"command_id": "rejected"}}
        return {}  # preserve it for inspection
    client._request = request
    client.run()
    assert set(q.peek_result()) >= {"command_id", "status", "success", "data", "scene_revision", "error"}


@pytest.fixture
def result_bridge(tmp_path):
    from universal_blender_mcp.bridge_server import BridgeServer
    from universal_blender_mcp.job_manager import JobManager
    jobs = JobManager(tmp_path / "jobs.db")
    server = BridgeServer("127.0.0.1", 0, "tok", jobs)
    server.start(daemon=True)
    yield server
    server.stop()
    jobs.close()


def _result(command_id):
    return {"command_id": command_id, "status": "completed", "success": True,
            "data": {"answer": 42}, "scene_revision": 1, "error": None, "final": True}


def _active_result(server, command_id):
    from universal_blender_mcp.schemas import build_command
    server.state.enqueue(build_command("blender_ping", command_id=command_id))
    assert server.state.poll_command(0)["command_id"] == command_id
    return _result(command_id)


@pytest.mark.parametrize("data", [{"answer": 42}, {"coordinates": (1, 2, 3)}])
def test_lost_ack_reconciles_with_persisted_bridge_result(result_bridge, data):
    server = result_bridge
    q = CommandQueue()
    envelope = _active_result(server, "lost-ack")
    envelope["data"] = data
    q.push_result(envelope)
    client = bridge_client.BridgeClient("127.0.0.1", server.port, "tok", queue=q)
    request = client._request
    def lost_response(method, path, payload=None):
        out = request(method, path, payload)
        if method == "POST":
            raise OSError("response lost after durable acceptance")
        return out
    client._request = lost_response
    client._drain_results()
    import json
    assert server.state.jobs.get("lost-ack")["result"] == json.loads(json.dumps(envelope))
    assert q.peek_result() is None
    assert client.results_sent == 1


def test_http_400_dead_letters_stale_head_and_delivers_later_result(result_bridge):
    server = result_bridge
    q = CommandQueue()
    q.push_result(_result("stale"))
    later = _active_result(server, "later")
    q.push_result(later)
    client = bridge_client.BridgeClient("127.0.0.1", server.port, "tok", queue=q)
    client._drain_results()
    assert server.state.jobs.get("later")["result"] == later
    assert q.peek_result() is None
    assert q.dead_letters[0]["envelope"]["command_id"] == "stale"
    assert "400" in q.dead_letters[0]["error"]


def test_transient_failures_are_bounded_without_blocking_other_results():
    q = CommandQueue()
    q.push_result(_result("offline"))
    q.push_result(_result("later"))
    client = bridge_client.BridgeClient("localhost", 1, "tok", queue=q,
                                        result_max_attempts=3)
    calls = []
    def request(method, path, payload=None):
        if method == "GET":
            return {"job": {"status": "running", "result": None}}
        calls.append(payload["command_id"])
        if payload["command_id"] == "offline":
            raise OSError("offline")
        return {"ok": True}
    client._request = request
    client._drain_results()
    assert calls == ["offline", "later"]
    client._drain_results(now=float("inf"))
    client._drain_results(now=float("inf"))
    assert q.peek_result() is None
    assert q.dead_letters[0]["attempts"] == 3
    assert q.dead_letters[0]["envelope"]["command_id"] == "offline"


def test_conflicting_persisted_result_is_retained_as_dead_letter(result_bridge):
    server = result_bridge
    persisted = _active_result(server, "conflict")
    assert server.state.submit_result(persisted)
    local = {**persisted, "data": {"answer": "different"}}
    q = CommandQueue()
    q.push_result(local)
    client = bridge_client.BridgeClient("127.0.0.1", server.port, "tok", queue=q)
    client._drain_results()
    assert q.peek_result() is None
    assert q.dead_letters[0]["envelope"] == local
    assert "conflict" in q.dead_letters[0]["error"].lower()
