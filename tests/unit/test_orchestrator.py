import asyncio
import threading
import time
import json
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from agent_gateway import orchestrator as mod
from agent_gateway.planning_engine import PlanningEngine
from agent_gateway.providers.base import ChatResult


class Provider:
    config = SimpleNamespace(provider_id='executor', model='fake', tool_calling=True)

    async def chat(self, **kwargs):
        return ChatResult('done')


class Client:
    instances = []

    def __init__(self, **kwargs):
        self.closed = False
        self.server_info = {}
        self.instances.append(self)

    async def start(self):
        pass

    async def close(self):
        self.closed = True

    async def list_tools(self):
        return []

    async def call_tool(self, name, arguments):
        return {'content': '{"success":true}', 'parsed': {'success': True, 'data': {}}, 'is_error': False}


def make_orchestrator(tmp_root, monkeypatch):
    monkeypatch.setattr(mod, 'MCPStdioClient', Client)
    o = mod.Orchestrator(tmp_root)
    o.router = SimpleNamespace(default_id='default',
        for_role=lambda role: Provider(), provider=lambda pid: Provider(),
        describe_routing=lambda: {'default': 'default', 'executor': 'executor', 'planner': 'planner'})
    return o


def test_executor_role_is_used_without_explicit_override(tmp_root, monkeypatch):
    o = make_orchestrator(tmp_root, monkeypatch)
    roles = []
    def role(name):
        roles.append(name)
        return Provider()
    o.router.for_role = role
    run = mod.TaskRun('r', 'task', None)
    asyncio.run(o._run_async(run, 'task', None, False))
    assert run.status == 'completed'
    assert roles == ['executor']


@pytest.mark.parametrize('after_start', [False, True])
def test_failure_before_or_after_mcp_start_is_terminal(tmp_root, monkeypatch, after_start):
    o = make_orchestrator(tmp_root, monkeypatch)
    def bad(*args):
        raise RuntimeError('provider unavailable')
    if after_start:
        monkeypatch.setattr(PlanningEngine, 'plan', bad)
    else:
        o.router.provider = bad
        o.router.for_role = bad
    run = mod.TaskRun('r', 'task', 'explicit')
    asyncio.run(o._run_async(run, 'task', None, after_start))
    assert run.status == 'failed'
    assert run.finished_at is not None
    assert run.error
    if after_start:
        assert Client.instances[-1].closed


def test_failed_verification_needs_repair(tmp_root, monkeypatch):
    o = make_orchestrator(tmp_root, monkeypatch)
    run = mod.TaskRun('r', 'task', 'explicit')
    asyncio.run(o._run_async(run, 'task', [{'kind': 'unknown'}], False))
    assert run.status == 'needs_repair'
    assert run.report['stop_reason'] == 'verification_failed'


def test_invalid_planner_output_fails_unless_direct_fallback_is_enabled():
    with pytest.raises(RuntimeError, match='Planner'):
        asyncio.run(PlanningEngine(Provider()).plan('task', {}, []))
    engine = PlanningEngine(Provider(), fallback='direct')
    plan = asyncio.run(engine.plan('task', {}, []))
    assert plan['fallback'] is True
    assert plan['mode'] == 'direct'


def test_planner_rejects_malformed_steps():
    class Bad(Provider):
        async def chat(self, **kwargs):
            return ChatResult('{"steps":["do it"]}')
    with pytest.raises(RuntimeError, match='Planner'):
        asyncio.run(PlanningEngine(Bad()).plan('task', {}, []))


def configure(tmp_root, **values):
    path = tmp_root / 'config' / 'app.toml'
    text = path.read_text(encoding='utf-8')
    for key, value in values.items():
        import re
        text = re.sub(r'^' + key + r'\s*=.*\n', '', text, flags=re.M)
    lines = '\n'.join(f'{key} = {json.dumps(value)}' for key, value in values.items())
    path.write_text(text.replace('[gateway]', '[gateway]\n' + lines), encoding='utf-8')


def test_bounded_workers_queue_and_shutdown_rejects_admission(tmp_root, monkeypatch):
    configure(tmp_root, worker_count=1, queue_max=1, max_runs=10)
    o = make_orchestrator(tmp_root, monkeypatch)
    started = threading.Event()
    release = threading.Event()
    def work(run, *args):
        started.set()
        release.wait(2)
    o._run_sync = work
    try:
        o.start_task('first')
        assert started.wait(1)
        o.start_task('second')
        with pytest.raises(RuntimeError, match='GatewayBusy'):
            o.start_task('overflow')
        assert len(o._workers) == 1
        assert len(o.list_runs()) == 2
    finally:
        release.set()
        o.shutdown()
    assert not any(t.is_alive() for t in o._workers)
    with pytest.raises(RuntimeError, match='shutdown'):
        o.start_task('after shutdown')


def test_event_history_is_bounded_and_snapshots_do_not_alias():
    run = mod.TaskRun('r', 'task', 'provider', max_events=3)
    for i in range(10):
        asyncio.run(mod.run_event(run, {'kind': 'event', 'data': {'i': i}}))
    assert len(run.events) == 3
    snapshot = run.to_dict()
    snapshot['events'][0]['data']['i'] = -1
    assert run.events[0]['data']['i'] == 7


def test_run_retention_prunes_ttl_and_caps_count(tmp_root, monkeypatch):
    configure(tmp_root, max_runs=2, run_ttl_s=1)
    o = make_orchestrator(tmp_root, monkeypatch)
    old = mod.TaskRun('old', 'task', 'p')
    old.status, old.finished_at = 'completed', time.time() - 5
    o.runs[old.id] = old
    assert o.list_runs() == []
    for i in range(4):
        run = mod.TaskRun(str(i), 'task', 'p')
        run.status, run.finished_at = 'completed', time.time()
        o.runs[run.id] = run
    assert len(o.list_runs()) == 2


def test_list_runs_uses_locked_registry_snapshot(tmp_root, monkeypatch):
    o = make_orchestrator(tmp_root, monkeypatch)
    class Guarded(dict):
        def values(self):
            assert o._lock.locked(), 'registry snapshot must hold lock'
            return super().values()
    o.runs = Guarded()
    assert o.list_runs() == []


def test_shutdown_cancels_active_run_and_closes_client(tmp_root, monkeypatch):
    configure(tmp_root, worker_count=1)
    o = make_orchestrator(tmp_root, monkeypatch)
    started = threading.Event()
    class Slow(Provider):
        async def chat(self, **kwargs):
            started.set()
            await asyncio.sleep(100)
    o.router.for_role = lambda role: Slow()
    run = o.start_task('task', plan_first=False)
    assert started.wait(2)
    o.shutdown(timeout_s=3)
    assert run.status == 'cancelled'
    assert Client.instances[-1].closed
    assert run.finished_at
    assert not any(w.is_alive() for w in o._workers)


def test_gateway_settings_are_consumed(tmp_root, monkeypatch):
    configure(tmp_root, max_turns=3, max_tool_calls_per_turn=2, request_timeout_s=7,
              context_max_tokens=4000, max_events_per_run=4, default_provider='local_lmstudio')
    o = make_orchestrator(tmp_root, monkeypatch)
    actual = mod.ExecutionLoop
    captured = {}
    class Capture(actual):
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)
            super().__init__(*args, **kwargs)
    monkeypatch.setattr(mod, 'ExecutionLoop', Capture)
    run = mod.TaskRun('r', 'task', None)
    asyncio.run(o._run_async(run, 'task', None, False))
    assert captured['max_turns'] == 3
    assert captured['max_tool_calls_per_turn'] == 2
    assert captured['max_events'] == 4
    assert captured['context_max_tokens'] == 4000


def test_http_auth_validation_and_body_limits(tmp_root, monkeypatch):
    configure(tmp_root, max_request_bytes=128)
    o = make_orchestrator(tmp_root, monkeypatch)
    server = ThreadingHTTPServer(('127.0.0.1', 0), mod.make_handler(o))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = 'http://127.0.0.1:' + str(server.server_port)
    def request(path, payload=None, authenticated=True):
        headers = {'Authorization': 'Bearer ' + o.api_token} if authenticated else {}
        body = json.dumps(payload).encode() if payload is not None else None
        try:
            with urllib.request.urlopen(urllib.request.Request(url + path, body, headers), timeout=2) as response:
                return response.status
        except urllib.error.HTTPError as error:
            return error.code
    try:
        assert request('/health', authenticated=False) == 401
        assert request('/health') == 200
        assert request('/task', ['wrong']) == 400
        assert request('/task', {'prompt': 1}) == 400
        assert request('/task', {'prompt': 'ok', 'verify': ['bad']}) == 400
        assert request('/task', {'prompt': 'ok', 'plan': 'false'}) == 400
        assert request('/task', {'prompt': 'x' * 200}) == 413
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        o.shutdown()


@pytest.mark.parametrize('value', [.5, True, 0, -1])
def test_worker_count_requires_positive_integer(tmp_root, value):
    configure(tmp_root, worker_count=value)
    with pytest.raises(ValueError, match='worker_count'):
        mod.Orchestrator(tmp_root)


def test_update_does_not_alias_report():
    run = mod.TaskRun('r', 'task', 'p')
    report = {'nested': {'key': 1}}
    run.update(report=report)
    report['nested']['key'] = 2
    assert run.to_dict()['report']['nested']['key'] == 1


def test_direct_fallback_handles_planner_provider_failure():
    class Broken(Provider):
        async def chat(self, **kwargs):
            raise RuntimeError('provider unavailable')
    result = asyncio.run(PlanningEngine(Broken(), fallback='direct').plan('task', {}, []))
    assert result['fallback'] is True
    assert result['mode'] == 'direct'


def test_repeated_shutdown_does_not_recancel_client_cleanup(tmp_root, monkeypatch):
    configure(tmp_root, worker_count=1)
    o = make_orchestrator(tmp_root, monkeypatch)
    started, cleaning, cleaned = threading.Event(), threading.Event(), threading.Event()
    class Slow(Provider):
        async def chat(self, **kwargs):
            started.set()
            await asyncio.sleep(100)
    class SlowClose(Client):
        async def close(self):
            cleaning.set()
            await asyncio.sleep(.1)
            cleaned.set()
    monkeypatch.setattr(mod, 'MCPStdioClient', SlowClose)
    o.router.for_role = lambda role: Slow()
    run = o.start_task('task', plan_first=False)
    assert started.wait(2)
    first = threading.Thread(target=o.shutdown, kwargs={'timeout_s': 3})
    first.start()
    assert cleaning.wait(2)
    o.shutdown(timeout_s=3)
    first.join(3)
    assert cleaned.is_set()
    assert run.finished_at is not None


@pytest.mark.parametrize('key', ['queue_max', 'max_runs', 'max_events_per_run',
    'max_event_bytes', 'context_max_tokens', 'max_request_bytes', 'max_prompt_chars',
    'max_verify_criteria', 'max_tool_calls_per_turn', 'max_turns', 'max_http_clients'])
def test_integer_limits_reject_fractional_values(tmp_root, key):
    configure(tmp_root, **{key: .5})
    with pytest.raises(ValueError, match=key):
        mod.Orchestrator(tmp_root)


@pytest.mark.parametrize('provider_fails', [False, True])
def test_first_shutdown_during_cleanup_waits_for_close_and_finalizes_registry(
        tmp_root, monkeypatch, provider_fails):
    configure(tmp_root, worker_count=1)
    o = make_orchestrator(tmp_root, monkeypatch)
    cleaning = threading.Event()
    cleanup_finished = threading.Event()
    class SlowClose(Client):
        async def close(self):
            cleaning.set()
            await asyncio.sleep(.15)
            self.closed = True
            cleanup_finished.set()
    class MaybeBroken(Provider):
        async def chat(self, **kwargs):
            if provider_fails:
                raise RuntimeError('backend unavailable')
            return ChatResult('done')
    monkeypatch.setattr(mod, 'MCPStdioClient', SlowClose)
    o.router.for_role = lambda role: MaybeBroken()
    run = o.start_task('task', plan_first=False)
    try:
        assert cleaning.wait(2), 'run must have reached close before its first shutdown'
        o.shutdown(timeout_s=3)
        assert cleanup_finished.is_set()
        assert Client.instances[-1].closed
        assert run.finished_at is not None
        assert not o._active
        assert run.status == ('failed' if provider_fails else 'completed')
    finally:
        o.shutdown(timeout_s=3)
