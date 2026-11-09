"""Integração: Bridge real + addon simulado — fluxo completo §6.4."""
from __future__ import annotations

import json
import threading
import time
import urllib.request

import pytest

from tests.conftest import FakeBlender
from universal_blender_mcp import bridge_server
from universal_blender_mcp.bridge_server import BridgeServer, BridgeState
from universal_blender_mcp.job_manager import JobManager
from universal_blender_mcp.schemas import build_command


def _post(url, payload, token=None, timeout=10):
    import urllib.error

    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Bridge-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"raw": body}


def _get(url, token=None, timeout=10):
    req = urllib.request.Request(url)
    if token:
        req.add_header("X-Bridge-Token", token)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode())


def test_status_open_and_auth_enforced(bridge_env):
    status, data = _get(f"{bridge_env['url']}/status")
    assert status == 200 and data["ok"] is True
    assert data["blender_connected"] is True

    # sem token → 401
    with pytest.raises(Exception) as err:
        _get(f"{bridge_env['url']}/poll")
    assert "401" in str(err.value) or "HTTP Error" in str(err.value)

    # token errado → 401 (helper _post captura HTTPError; checa status)
    status, body = _post(f"{bridge_env['url']}/commands",
                         {"operation": "blender_ping"}, token="wrong")
    assert status == 401


def test_full_roundtrip_submit_poll_result(bridge_env):
    url, token = bridge_env["url"], bridge_env["token"]
    status, out = _post(f"{url}/commands", {
        "operation": "object_create",
        "arguments": {"type": "CUBE", "name": "MCP_Test_Cube"},
        "idempotency_key": "create-cube-001",
    }, token=token)
    assert status == 200 and out["status"] == "queued"

    deadline = time.time() + 10
    result = None
    while time.time() < deadline:
        _, info = _get(f"{url}/commands/{out['command_id']}", token=token)
        if info["job"]["status"] == "completed":
            result = info["result"]
            break
        time.sleep(0.1)
    assert result is not None, f"job não completou: {info['job']}"
    assert result["success"] is True
    assert result["data"]["object_name"] == "MCP_Test_Cube"
    assert result["data"]["object_type"] == "CUBE"
    assert result["scene_revision"] == 1


def test_fake_blender_rejects_operation_without_explicit_handler(tmp_path):
    """A missing fake handler must expose a failed contract, never a no-op."""
    jobs = JobManager(tmp_path / "jobs.db")
    server = BridgeServer("127.0.0.1", 0, "tok", jobs,
                          command_timeout_s=2.0)
    server.start(daemon=True)
    fake = FakeBlender(server.url, "tok", handlers={})
    fake.register()
    fake.start()
    try:
        deadline = time.monotonic() + 2.0
        while not fake.registered and time.monotonic() < deadline:
            time.sleep(0.01)
        assert fake.registered

        status, submitted = _post(
            f"{server.url}/commands",
            {"operation": "operation_without_handler", "arguments": {}},
            token="tok",
        )
        assert status == 200
        deadline = time.monotonic() + 2.0
        result = None
        while time.monotonic() < deadline:
            _, info = _get(
                f"{server.url}/commands/{submitted['command_id']}",
                token="tok",
            )
            if info["job"]["status"] in ("completed", "failed"):
                result = info["result"]
                break
            time.sleep(0.01)

        assert result is not None
        assert result["success"] is False
        assert result["status"] == "failed"
        assert result["error"]["type"] == "UnsupportedOperation"
    finally:
        fake.stop()
        server.stop()
        jobs.close()


def test_fake_blender_requires_registration_before_thread_start():
    fake = FakeBlender("http://127.0.0.1:1", "tok", handlers={})
    try:
        with pytest.raises(RuntimeError, match="register"):
            fake.start()
    finally:
        if fake.is_alive():
            fake.stop()
    assert fake.executed == []


def test_idempotent_replay_does_not_execute_twice(bridge_env):
    url, token = bridge_env["url"], bridge_env["token"]
    payload = {"operation": "blender_ping", "arguments": {},
               "idempotency_key": "replay-key"}
    _, first = _post(f"{url}/commands", payload, token=token)
    deadline = time.time() + 10
    while time.time() < deadline:
        _, info = _get(f"{url}/commands/{first['command_id']}", token=token)
        if info["job"]["status"] == "completed":
            break
        time.sleep(0.1)
    pings_before = bridge_env["fake"].executed.count("blender_ping")

    _, second = _post(f"{url}/commands", payload, token=token)
    assert second.get("idempotent_replay") is True
    assert second["command_id"] == first["command_id"]
    pings_after = bridge_env["fake"].executed.count("blender_ping")
    assert pings_after == pings_before  # §6.4: sem duplicação


def test_queue_full_rejection(tmp_path):
    jobs = JobManager(tmp_path / "jobs.db")
    server = BridgeServer("127.0.0.1", 0, "tok", jobs, queue_max=2,
                          command_timeout_s=30)
    server.start(daemon=True)
    try:
        url = server.url
        for i in range(2):
            status, out = _post(f"{url}/commands",
                                {"operation": "op", "arguments": {}},
                                token="tok")
            assert out["status"] == "queued"
        status, out = _post(f"{url}/commands",
                            {"operation": "op", "arguments": {}}, token="tok")
        assert status == 503
        assert out["status"] == "rejected"
        job = jobs.get(out["command_id"])
        assert job["status"] == "failed"
        assert job["error"]["type"] == "QueueFull"
    finally:
        server.stop()


@pytest.fixture()
def bridge_state(tmp_path):
    jobs = JobManager(tmp_path / "jobs.db")
    state = BridgeState("tok", jobs)
    yield state
    jobs.close()


def _result(cid, **updates):
    payload = {"command_id": cid, "status": "completed", "success": True,
               "data": {"done": True}, "scene_revision": 1, "error": None}
    payload.update(updates)
    return payload


def test_replay_result_is_defensively_copied_from_direct_state(bridge_state):
    command = build_command("op", command_id="original", idempotency_key="copy")
    bridge_state.enqueue(command)
    bridge_state.poll_command(0)
    assert bridge_state.submit_result(_result("original"))
    replay = bridge_state.enqueue(build_command("op", idempotency_key="copy"))
    replay["result"]["data"]["done"] = "tampered"
    repeated = bridge_state.enqueue(build_command("op", idempotency_key="copy"))
    assert repeated["result"]["data"] == {"done": True}
    assert bridge_state.results["original"]["data"] == {"done": True}
    assert bridge_state.jobs.get("original")["result"]["data"] == {"done": True}


def test_expiration_route_requires_token_and_preserves_terminal_result(tmp_path):
    jobs = JobManager(tmp_path / "jobs.db")
    server = BridgeServer("127.0.0.1", 0, "tok", jobs)
    server.start()
    try:
        server.state.enqueue(build_command("op", command_id="expire-me"))
        url = server.url + "/commands/expire-me/expire"
        assert _post(url, {"reason": "client deadline elapsed"})[0] == 401
        assert jobs.get("expire-me")["status"] == "queued"
        code, response = _post(url, {"reason": "client deadline elapsed"}, "tok")
        assert code == 200
        assert response["expired"] is True
        assert response["command_id"] == "expire-me"
        assert server.state.queue == []
        expected = jobs.get("expire-me")["result"]
        assert expected["error"]["type"] == "Timeout"
        assert expected["error"]["message"] == "client deadline elapsed"
        _, repeated = _post(url, {"reason": "again"}, "tok")
        assert repeated["expired"] is False
        assert jobs.get("expire-me")["result"] == expected
    finally:
        server.stop()
        jobs.close()


@pytest.mark.parametrize("cid,reason", [("", "expired"), ("bad/id", "expired"),
                                       ("valid", None), ("valid", ""),
                                       ("valid", []), ("valid", "   ")])
def test_expiration_route_rejects_invalid_id_or_reason(tmp_path, cid, reason):
    jobs = JobManager(tmp_path / "jobs.db")
    server = BridgeServer("127.0.0.1", 0, "tok", jobs)
    server.start()
    try:
        server.state.enqueue(build_command("op", command_id="valid"))
        code, response = _post(server.url + f"/commands/{cid}/expire", {"reason": reason}, "tok")
        assert code == 400
        assert response["ok"] is False
        assert jobs.get("valid")["status"] == "queued"
    finally:
        server.stop()
        jobs.close()


def test_poll_never_delivers_durably_cancelled_command(bridge_state):
    command = build_command("object_create", command_id="cancelled")
    bridge_state.enqueue(command)
    bridge_state.jobs.create(command)
    assert bridge_state.jobs.cancel("cancelled")["cancelled"] is True
    assert bridge_state.poll_command(0) is None


def test_cancel_before_poll_removes_command_and_publishes_result(bridge_state):
    bridge_state.enqueue(build_command("object_create", command_id="cancelled"))
    assert bridge_state.cancel("cancelled")["cancelled"] is True
    assert bridge_state.poll_command(0) is None
    assert bridge_state.jobs.get("cancelled")["result"]["status"] == "cancelled"
    assert bridge_state.results["cancelled"]["success"] is False
    assert bridge_state.cancel("cancelled")["cancelled"] is False


def test_dispatch_marks_running_and_prevents_cancellation(bridge_state):
    bridge_state.enqueue(build_command("object_create", command_id="running"))
    assert bridge_state.poll_command(0)["command_id"] == "running"
    assert bridge_state.jobs.get("running")["status"] == "running"
    assert bridge_state.cancel("running")["cancelled"] is False
    assert bridge_state.active_command == "running"


def test_cancel_and_poll_have_exactly_one_winner(bridge_state):
    for index in range(12):
        cid = f"race-{index}"
        bridge_state.enqueue(build_command("object_create", command_id=cid))
        start = threading.Barrier(3)
        outcomes = {}

        def cancel():
            start.wait()
            outcomes["cancel"] = bridge_state.cancel(cid)

        def poll():
            start.wait()
            outcomes["command"] = bridge_state.poll_command(0)

        threads = [threading.Thread(target=cancel), threading.Thread(target=poll)]
        for thread in threads:
            thread.start()
        start.wait()
        for thread in threads:
            thread.join(timeout=3)
            assert not thread.is_alive()
        if outcomes["cancel"]["cancelled"]:
            assert outcomes["command"] is None
            assert bridge_state.jobs.get(cid)["status"] == "cancelled"
        else:
            assert outcomes["command"]["command_id"] == cid
            assert bridge_state.jobs.get(cid)["status"] == "running"
            assert bridge_state.submit_result(_result(cid)) is True


@pytest.mark.parametrize("same_id", [True, False])
def test_pending_duplicates_are_enqueued_once(tmp_path, same_id):
    jobs = JobManager(tmp_path / "jobs.db")
    server = BridgeServer("127.0.0.1", 0, "tok", jobs)
    server.start()
    try:
        command = {"operation": "object_create", "command_id": "first",
                   "idempotency_key": "one-effect"}
        assert _post(server.url + "/commands", command, "tok")[0] == 200
        if not same_id:
            command["command_id"] = "second"
        _, replay = _post(server.url + "/commands", command, "tok")
        assert replay["command_id"] == "first"
        assert replay["idempotent_replay"] is True
        assert len(server.state.queue) == 1
        assert server.state.poll_command(0)["command_id"] == "first"
        _, running_replay = _post(server.url + "/commands", command, "tok")
        assert running_replay["status"] == "running"
        assert server.state.queue == []
    finally:
        server.stop()
        jobs.close()


def test_concurrent_duplicate_submissions_reserve_one_queue_slot(bridge_state):
    start = threading.Barrier(9)
    outcomes = []

    def submit(index):
        start.wait()
        outcomes.append(bridge_state.enqueue(build_command(
            "object_create", command_id=f"duplicate-{index}", idempotency_key="once")))

    threads = [threading.Thread(target=submit, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join(timeout=3)
        assert not thread.is_alive()
    assert len({outcome["command_id"] for outcome in outcomes}) == 1
    assert len(bridge_state.queue) == 1
    assert len(bridge_state.jobs.list()) == 1


@pytest.mark.parametrize("missing", ["command_id", "status", "success", "data",
                                    "scene_revision", "error"])
def test_result_requires_complete_envelope(bridge_state, missing):
    bridge_state.enqueue(build_command("op", command_id="active"))
    bridge_state.poll_command(0)
    payload = _result("active")
    del payload[missing]
    assert bridge_state.submit_result(payload) is False
    assert bridge_state.active_command == "active"
    assert bridge_state.results == {}
    assert bridge_state.jobs.get("active")["status"] == "running"


@pytest.mark.parametrize("updates", [
    {"command_id": "unknown"}, {"command_id": ""}, {"command_id": ["active"]},
    {"status": "queued"}, {"status": "bogus"}, {"status": []},
    {"success": "yes"}, {"success": False}, {"scene_revision": "bad"},
    {"scene_revision": True}, {"scene_revision": -1}, {"error": {}},
    {"final": False}, {"final": "true"},
    {"status": "failed", "success": False, "error": None},
    {"status": "failed", "success": False, "error": {"type": "Oops"}},
])
def test_invalid_result_is_rejected_without_mutation(bridge_state, updates):
    bridge_state.enqueue(build_command("op", command_id="active"))
    bridge_state.poll_command(0)
    assert bridge_state.submit_result(_result("active", **updates)) is False
    assert bridge_state.active_command == "active"
    assert bridge_state.last_revision is None
    assert bridge_state.results == {}
    assert bridge_state.jobs.get("active")["status"] == "running"


def test_known_but_undispatched_command_cannot_submit_result(bridge_state):
    bridge_state.enqueue(build_command("op", command_id="queued"))
    assert bridge_state.submit_result(_result("queued")) is False
    assert bridge_state.jobs.get("queued")["status"] == "queued"


def test_running_progress_keeps_serial_slot_until_final_result(bridge_state):
    bridge_state.enqueue(build_command("op", command_id="active"))
    bridge_state.enqueue(build_command("op", command_id="next"))
    bridge_state.poll_command(0)
    assert bridge_state.submit_result(_result("active", status="running", final=False)) is True
    assert bridge_state.jobs.get("active")["status"] == "running"
    assert bridge_state.poll_command(0) is None
    assert bridge_state.submit_result(_result("active")) is True
    assert bridge_state.poll_command(0)["command_id"] == "next"


def test_terminal_result_cannot_be_overwritten_in_cache_or_database(bridge_state):
    bridge_state.enqueue(build_command("op", command_id="done"))
    bridge_state.poll_command(0)
    first = _result("done")
    assert bridge_state.submit_result(first) is True
    assert bridge_state.submit_result(_result("done", scene_revision=99)) is False
    assert bridge_state.results["done"] == first
    assert bridge_state.jobs.get("done")["result"] == first
    assert bridge_state.last_revision == 1


def test_expire_removes_queued_mutation_and_rejects_late_result(bridge_state):
    bridge_state.enqueue(build_command("object_create", command_id="expired"))
    assert bridge_state.expire("expired", "client deadline elapsed") is True
    assert bridge_state.poll_command(0) is None
    assert bridge_state.submit_result(_result("expired")) is False
    result = bridge_state.jobs.get("expired")["result"]
    assert result["status"] == "failed"
    assert result["error"]["type"] == "Timeout"
    assert result["error"]["message"] == "client deadline elapsed"
    assert bridge_state.expire("expired", "again") is False
    replay = bridge_state.enqueue(build_command("object_create", command_id="expired"))
    assert replay["status"] == "failed"
    assert bridge_state.poll_command(0) is None


def test_command_deadline_counts_queue_wait_and_is_not_extended(bridge_state, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(bridge_server.time, "time", lambda: clock[0])
    bridge_state.enqueue(build_command("object_create", command_id="expired", timeout_s=2))
    clock[0] = 103.0
    assert bridge_state.poll_command(0) is None
    assert bridge_state.jobs.get("expired")["error"]["type"] == "Timeout"


def test_dispatch_includes_bounded_absolute_deadline(bridge_state, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(bridge_server.time, "time", lambda: clock[0])
    bridge_state.command_timeout_s = 10
    bridge_state.enqueue(build_command("op", command_id="deadline", timeout_s=30))
    clock[0] = 102.0
    command = bridge_state.poll_command(0)
    assert command["deadline_at"] == 110.0
    assert command["timeout_s"] == 30
    clock[0] = 110.0
    bridge_state.watchdog_tick()
    assert bridge_state.jobs.get("deadline")["status"] == "failed"
    assert bridge_state.active_command is None
    assert bridge_state.submit_result(_result("deadline")) is False


def test_existing_deadline_is_never_extended(bridge_state, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(bridge_server.time, "time", lambda: clock[0])
    command = build_command("op", command_id="earlier", timeout_s=30)
    command["deadline_at"] = 101.0
    bridge_state.enqueue(command)
    assert bridge_state.poll_command(0)["deadline_at"] == 101.0
    clock[0] = 101.0
    bridge_state.watchdog_tick()
    assert bridge_state.jobs.get("earlier")["status"] == "failed"


def test_result_after_deadline_expires_job_without_waiting_for_watchdog(bridge_state, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(bridge_server.time, "time", lambda: clock[0])
    bridge_state.enqueue(build_command("op", command_id="late", timeout_s=1))
    bridge_state.poll_command(0)
    clock[0] = 102.0
    assert bridge_state.submit_result(_result("late")) is False
    assert bridge_state.jobs.get("late")["error"]["type"] == "Timeout"
    assert bridge_state.results["late"]["status"] == "failed"


def test_result_cache_is_bounded_and_eviction_preserves_http_result(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge_server, "RESULT_CACHE_MAX", 2, raising=False)
    jobs = JobManager(tmp_path / "jobs.db")
    server = BridgeServer("127.0.0.1", 0, "tok", jobs)
    server.start()
    try:
        for index in range(3):
            cid = f"cached-{index}"
            server.state.enqueue(build_command("op", command_id=cid))
            server.state.poll_command(0)
            assert server.state.submit_result(_result(cid)) is True
        assert len(server.state.results) == 2
        _, info = _get(server.url + "/commands/cached-0", "tok")
        assert info["result"] == _result("cached-0")
    finally:
        server.stop()
        jobs.close()


def test_duplicate_command_id_without_idempotency_key_is_enqueued_once(bridge_state):
    bridge_state.enqueue(build_command("object_create", command_id="same-id"))
    replay = bridge_state.enqueue(build_command("object_create", command_id="same-id"))
    assert replay["idempotent_replay"] is True
    assert len(bridge_state.queue) == 1


def test_enqueue_expires_old_commands_before_reserving_queue_capacity(bridge_state, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(bridge_server.time, "time", lambda: clock[0])
    bridge_state.queue_max = 1
    bridge_state.enqueue(build_command("object_create", command_id="old", timeout_s=1))
    clock[0] = 102.0
    result = bridge_state.enqueue(build_command("object_create", command_id="new"))
    assert result["status"] == "queued"
    assert bridge_state.jobs.get("old")["status"] == "failed"
    assert bridge_state.poll_command(0)["command_id"] == "new"


@pytest.mark.parametrize("action", ["cancel", "expire", "complete", "reject"])
def test_terminal_transitions_wake_waiters(bridge_state, action):
    if action != "reject":
        bridge_state.enqueue(build_command("op", command_id="watched"))
    else:
        bridge_state.queue_max = 0
    if action == "complete":
        bridge_state.poll_command(0)
    ready = threading.Event()
    observed = []

    def wait_for_terminal():
        with bridge_state.cond:
            ready.set()
            changed = bridge_state.cond.wait_for(
                lambda: (bridge_state.jobs.get("watched") or {}).get("status")
                in ("completed", "failed", "cancelled"), timeout=5)
            observed.append(changed)

    thread = threading.Thread(target=wait_for_terminal)
    thread.start()
    assert ready.wait(timeout=1)
    if action == "cancel":
        bridge_state.cancel("watched")
    elif action == "expire":
        bridge_state.expire("watched", "expired")
    elif action == "complete":
        bridge_state.submit_result(_result("watched"))
    else:
        bridge_state.enqueue(build_command("op", command_id="watched"))
    thread.join(timeout=1)
    missed_notification = thread.is_alive()
    if missed_notification:
        with bridge_state.cond:
            bridge_state.cond.notify_all()
        thread.join(timeout=1)
    assert not missed_notification
    assert observed == [True]


@pytest.mark.parametrize("updates", [
    {"command_id": []}, {"command_id": ""}, {"operation": 42},
    {"arguments": []}, {"idempotency_key": {}},
    {"timeout_s": 0}, {"timeout_s": -1}, {"timeout_s": True},
    {"timeout_s": "bad"}, {"timeout_s": float("nan")}, {"timeout_s": float("inf")},
    {"created_at": "bad"}, {"created_at": float("nan")},
    {"deadline_at": "bad"}, {"deadline_at": float("nan")},
])
def test_invalid_submission_is_400_without_reserving_job(tmp_path, updates):
    jobs = JobManager(tmp_path / "jobs.db")
    server = BridgeServer("127.0.0.1", 0, "tok", jobs)
    server.start()
    try:
        command = {"operation": "op", "command_id": "invalid", **updates}
        code, response = _post(server.url + "/commands", command, "tok")
        assert code == 400
        assert response["ok"] is False
        assert jobs.list() == []
        assert server.state.queue == []
    finally:
        server.stop()
        jobs.close()


def test_watchdog_fails_stale_active_command(tmp_path):
    """Comando pego pelo poll e nunca respondido → falha estruturada por timeout."""
    import threading

    jobs = JobManager(tmp_path / "jobs.db")
    server = BridgeServer("127.0.0.1", 0, "tok", jobs, queue_max=4,
                          command_timeout_s=0.2, heartbeat_timeout_s=10)
    server.start(daemon=True)

    # consumidor malvado: pega o comando e NUNCA responde
    stop = threading.Event()

    def greedy():
        req = urllib.request.Request(
            server.url + "/register", data=b"{}", method="POST")
        req.add_header("X-Bridge-Token", "tok")
        req.add_header("Content-Type", "application/json")
        try:
            urllib.request.urlopen(req, timeout=5).read()
        except OSError:
            return
        while not stop.is_set():
            try:
                req = urllib.request.Request(server.url + "/poll?wait=0.3")
                req.add_header("X-Bridge-Token", "tok")
                urllib.request.urlopen(req, timeout=5).read()  # descarta
            except OSError:
                return

    evil = threading.Thread(target=greedy, daemon=True)
    evil.start()
    import time as _t
    _t.sleep(0.3)
    try:
        _status, out = _post(f"{server.url}/commands",
                             {"operation": "op", "arguments": {}}, token="tok")
        deadline = _t.time() + 10
        result = None
        while _t.time() < deadline:
            _s, info = _get(f"{server.url}/commands/{out['command_id']}",
                            token="tok")
            if info["job"]["status"] == "failed":
                result = info["result"]
                break
            _t.sleep(0.2)
        assert result is not None, f"watchdog não falhou o comando: {info}"
        assert result["error"]["type"] == "Timeout"
    finally:
        stop.set()
        evil.join(timeout=3)
        server.stop()
