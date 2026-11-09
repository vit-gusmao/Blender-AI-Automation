"""Orquestrador do harness próprio + API HTTP local (§7.2, §12 do plano).

HTTP (127.0.0.1:8767 por padrão):
  POST /task            {"prompt": "...", "provider": "local_ollama",
                         "verify": [{"kind": "object_exists", "name": "X"}]}
  GET  /tasks           lista runs
  GET  /tasks/{id}      status, eventos e relatório
  GET  /health          saúde + provedores cadastrados
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
import tomllib
import copy
import os
import queue
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

from .context_manager import ContextManager  # noqa: F401 — reexportado p/ testes
from .execution_loop import ExecutionLoop
from .mcp_client import MCPStdioClient
from .model_registry import load_registry
from .model_router import ModelRouter
from .planning_engine import PlanningEngine
from .tool_call_adapter import tool_defs_for_provider
from .validation_engine import ValidationEngine  # noqa: F401
from .providers.base import redact


class TaskRun:
    def __init__(self, run_id: str, prompt: str, provider_id: Optional[str],
                 max_events: int = 100, max_event_bytes: int = 16384,
                 secret_values=()):
        self.id = run_id
        self.prompt = prompt
        self.provider_id = provider_id
        self.status = "queued"
        self.created_at = time.time()
        self.finished_at: Optional[float] = None
        self.events: List[Dict[str, Any]] = []
        self.report: Dict[str, Any] = {}
        self.verification: Dict[str, Any] = {}
        self.error: Optional[str] = None
        self.max_events = max(1, max_events)
        self.max_event_bytes = max_event_bytes
        self.secret_values = tuple(secret_values)
        self._lock = threading.RLock()

    def update(self, **values):
        with self._lock:
            for key, value in values.items():
                setattr(self, key, redact(value, self.secret_values))

    def add_event(self, event):
        with self._lock:
            event = redact(copy.deepcopy(event), self.secret_values)
            if len(json.dumps(event, ensure_ascii=False, default=str).encode()) > self.max_event_bytes:
                event = {'ts': event.get('ts'), 'kind': event.get('kind'), 'truncated': True}
            self.events.append(event)
            del self.events[:-self.max_events]

    def to_dict(self, include_events: bool = True) -> Dict[str, Any]:
        with self._lock:
            return self._snapshot(include_events)

    def _snapshot(self, include_events):
        out = {
            "id": self.id, "status": self.status,
            "provider": self.provider_id,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "report": self.report, "verification": self.verification,
            "error": self.error,
        }
        if include_events:
            out["events"] = self.events
        return redact(copy.deepcopy(out), self.secret_values)


class Orchestrator:
    def __init__(self, root: Path, max_turns: Optional[int] = None):
        self.root = Path(root)
        registry = load_registry(self.root / "config" / "providers.yaml")
        errors = registry.validate_all()
        if errors:
            print("[gateway] avisos de configuração:")
            for err in errors:
                print("  -", err)
        self.registry = registry
        self.router = ModelRouter(registry)
        config_path = self.root / 'config' / 'app.toml'
        self.gateway_config = (tomllib.loads(config_path.read_text(encoding='utf-8')).get('gateway', {})
                               if config_path.exists() else {})
        cfg = self.gateway_config
        def positive(key, default, integer=None):
            value = cfg.get(key, default)
            if integer is None:
                integer = type(default) is int
            if (isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0
                    or (integer and not isinstance(value, int))):
                raise ValueError(f'gateway.{key} must be positive')
            return value
        self.max_turns = int(max_turns if max_turns is not None else positive('max_turns', 24))
        self.max_tool_calls_per_turn = int(positive('max_tool_calls_per_turn', 8))
        self.worker_count = positive('worker_count', 2, integer=True)
        self.max_runs = int(positive('max_runs', 100))
        self.run_ttl_s = positive('run_ttl_s', 86400.0)
        self.max_events = int(positive('max_events_per_run', 100))
        self.max_event_bytes = int(positive('max_event_bytes', 16384))
        self.context_max_tokens = int(positive('context_max_tokens', 24000))
        self.request_timeout_s = positive('request_timeout_s', 120.0)
        self.max_request_bytes = int(positive('max_request_bytes', 131072))
        self.max_prompt_chars = int(positive('max_prompt_chars', 32000))
        self.max_verify_criteria = int(positive('max_verify_criteria', 100))
        self.max_http_clients = positive('max_http_clients', 16)
        self.http_timeout_s = positive('http_timeout_s', 10.0)
        default = cfg.get('default_provider')
        if default:
            registry.get(default)
            self.router.default_id = default
        token_env = cfg.get('auth_token_env', 'BLENDER_GATEWAY_TOKEN')
        self.api_token = os.environ.get(token_env) or ''
        if not self.api_token:
            token_file = self.root / cfg.get('token_file', 'storage/gateway_token')
            token_file.parent.mkdir(parents=True, exist_ok=True)
            try:
                with token_file.open('x', encoding='utf-8') as stream:
                    stream.write(secrets.token_urlsafe(32))
            except FileExistsError:
                pass
            self.api_token = token_file.read_text(encoding='utf-8').strip()
            if not self.api_token:
                raise ValueError('gateway token file is empty')
        self._secret_values = tuple(c.api_key for c in registry.configs.values() if c.api_key) + (self.api_token,)
        self.runs: Dict[str, TaskRun] = {}
        self._lock = threading.Lock()
        self._queue = queue.Queue(maxsize=int(positive('queue_max', 16)))
        self._workers: List[threading.Thread] = []
        self._closing = False
        self._active = {}

    # -- execução de tarefas --------------------------------------------------

    def start_task(self, prompt: str, provider_id: Optional[str] = None,
                   verify: Optional[List[Dict[str, Any]]] = None,
                   plan_first: bool = True) -> TaskRun:
        run_id = f"run-{uuid.uuid4().hex[:10]}"
        self._validate_request(prompt, provider_id, verify, plan_first)
        run = TaskRun(run_id, prompt, provider_id, self.max_events,
                      self.max_event_bytes, self._secret_values)
        with self._lock:
            if self._closing:
                raise RuntimeError('gateway shutdown: no new tasks accepted')
            self._prune_locked(reserve=1)
            if len(self.runs) >= self.max_runs or self._queue.full():
                raise RuntimeError('GatewayBusy: run or queue capacity exhausted')
            self.runs[run_id] = run
            self._queue.put_nowait((run, prompt, copy.deepcopy(verify), plan_first))
            if not self._workers:
                for i in range(self.worker_count):
                    worker = threading.Thread(target=self._worker, name=f'gateway-worker-{i}', daemon=True)
                    self._workers.append(worker)
                    worker.start()
        return run

    def _validate_request(self, prompt, provider_id, verify, plan_first):
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > self.max_prompt_chars:
            raise ValueError('prompt must be a nonempty string within the configured limit')
        if not isinstance(plan_first, bool):
            raise ValueError('plan must be boolean')
        if provider_id is not None:
            if not isinstance(provider_id, str):
                raise ValueError('provider must be a registered string id')
            try:
                self.registry.get(provider_id)
            except KeyError:
                raise ValueError('unknown provider') from None
        if verify is not None:
            if not isinstance(verify, list) or len(verify) > self.max_verify_criteria:
                raise ValueError('verify must be a bounded array of criteria')
            for item in verify:
                if not isinstance(item, dict):
                    raise ValueError('criterion must be an object')
                kind = item.get('kind')
                if kind == 'object_exists':
                    valid = isinstance(item.get('name'), str) and bool(item['name'].strip())
                elif kind == 'object_count_min':
                    valid = type(item.get('count')) is int and item['count'] >= 0
                elif kind == 'file_exists':
                    valid = isinstance(item.get('path'), str) and bool(item['path'])
                elif kind == 'tool_success':
                    valid = isinstance(item.get('tool'), str) and bool(item['tool']) and isinstance(item.get('arguments', {}), dict)
                else:
                    valid = False
                if not valid:
                    raise ValueError('invalid verification criterion')

    def _worker(self):
        while True:
            with self._lock:
                if self._closing:
                    return
            try:
                work = self._queue.get(timeout=.1)
            except queue.Empty:
                continue
            run = work[0]
            try:
                self._run_sync(*work)
            except BaseException as exc:
                run.update(status='failed', error=f'{type(exc).__name__}: {exc}', finished_at=time.time())
            finally:
                self._queue.task_done()

    def _prune_locked(self, reserve=0):
        now = time.time()
        terminal = []
        for run in self.runs.values():
            with run._lock:
                if run.finished_at is not None:
                    terminal.append((run.finished_at, run.id))
        for finished, run_id in sorted(terminal):
            if now - finished > self.run_ttl_s or len(self.runs) + reserve > self.max_runs:
                self.runs.pop(run_id, None)

    def shutdown(self, timeout_s=None):
        with self._lock:
            first_shutdown = not self._closing
            self._closing = True
            while True:
                try:
                    work = self._queue.get_nowait()
                except queue.Empty:
                    break
                work[0].update(status='cancelled', finished_at=time.time(),
                               report={'stop_reason': 'shutdown'})
                self._queue.task_done()
            if first_shutdown:
                for event_loop, task in tuple(self._active.values()):
                    if not event_loop.is_closed():
                        event_loop.call_soon_threadsafe(task.cancel)
        deadline = time.monotonic() + (timeout_s if timeout_s is not None else self.request_timeout_s + 10)
        for worker in self._workers:
            if worker is not threading.current_thread():
                worker.join(max(0, deadline - time.monotonic()))
        if any(worker.is_alive() for worker in self._workers):
            raise RuntimeError('GatewayShutdownTimeout: workers still draining')

    def _run_sync(self, run: TaskRun, prompt: str,
                  verify: Optional[List[Dict[str, Any]]], plan_first: bool) -> None:
        asyncio.run(self._run_async(run, prompt, verify, plan_first))

    async def _run_async(self, run: TaskRun, prompt: str,
                         verify: Optional[List[Dict[str, Any]]],
                         plan_first: bool) -> None:
        mcp = None
        try:
            with self._lock:
                if self._closing:
                    run.update(status='cancelled', report={'stop_reason': 'shutdown'})
                    return
                self._active[run.id] = (asyncio.get_running_loop(), asyncio.current_task())
            run.update(status='running')
            provider = (self.router.provider(run.provider_id) if run.provider_id
                        else self.router.for_role('executor'))
            provider.request_timeout_s = self.request_timeout_s
            run.update(provider_id=provider.config.provider_id)
            system_prompt_path = self.root / 'prompts' / 'system_prompt.md'
            system_prompt = (system_prompt_path.read_text(encoding='utf-8')
                             if system_prompt_path.exists() else 'Você é o Universal Blender MCP Agent.')
            mcp = MCPStdioClient(env={'BLENDER_MCP_ROOT': str(self.root)})
            await mcp.start()
            await run_event(run, {"ts": time.time(), "kind": "mcp_ready",
                                  "server": mcp.server_info})

            plan = None
            if plan_first:
                planner = self.router.for_role('planner')
                planner.request_timeout_s = self.request_timeout_s
                engine = PlanningEngine(planner,
                                        fallback=self.gateway_config.get('planner_fallback', 'fail'))
                ping = await mcp.call_tool("scene_get_info", {})
                tools = tool_defs_for_provider(await mcp.list_tools())
                plan = await engine.plan(prompt, ping["parsed"].get("data", {}),
                                         tools)
                await run_event(run, {"ts": time.time(), "kind": "plan",
                                      "fallback": plan.get("fallback", True)})

            loop = ExecutionLoop(
                provider, mcp, system_prompt,
                max_turns=self.max_turns,
                max_tool_calls_per_turn=self.max_tool_calls_per_turn,
                max_events=self.max_events,
                max_event_bytes=self.max_event_bytes,
                context_max_tokens=self.context_max_tokens,
                validation_root=self.root,
                on_event=lambda e: run_event(run, e),
            )
            outcome = await loop.run(prompt, plan=plan)
            run.update(report=outcome.report)

            if verify and outcome.status == 'completed':
                outcome.verification = await loop.verify(verify)
                run.update(verification=outcome.verification)
                if not outcome.verification['passed']:
                    outcome.status = 'needs_repair'
                    outcome.stop_reason = 'verification_failed'
            outcome.report['stop_reason'] = outcome.stop_reason
            run.update(status=outcome.status, report=outcome.report)
        except asyncio.CancelledError:
            run.update(status='cancelled', report={'stop_reason': 'shutdown'})
        except Exception as exc:  # noqa: BLE001 — run nunca pode derrubar o host
            run.update(status='failed', error=f'{type(exc).__name__}: {exc}',
                       report={'stop_reason': 'execution_error'})
        finally:
            try:
                if mcp is not None:
                    # Shutdown may arrive after execution, while this finally is
                    # already closing MCP. Keep cleanup alive and join it even
                    # when the run task receives cancellation at that await.
                    cleanup = asyncio.create_task(mcp.close())
                    while not cleanup.done():
                        try:
                            await asyncio.shield(cleanup)
                        except asyncio.CancelledError:
                            continue
                    cleanup.result()
            except (Exception, asyncio.CancelledError) as exc:
                run.update(status='failed', error=f'MCPCloseError: {type(exc).__name__}: {exc}',
                           report={'stop_reason': 'mcp_close_error'})
            finally:
                run.update(finished_at=time.time())
                with self._lock:
                    self._active.pop(run.id, None)

    # -- consultas ----------------------------------------------------------------

    def get(self, run_id: str) -> Optional[TaskRun]:
        with self._lock:
            self._prune_locked()
            return self.runs.get(run_id)

    def list_runs(self) -> List[Dict[str, Any]]:
        with self._lock:
            self._prune_locked()
            return [r.to_dict(include_events=False) for r in self.runs.values()]

    def health(self) -> Dict[str, Any]:
        with self._lock:
            self._prune_locked()
            count = len(self.runs)
        return redact({
            "ok": True,
            "gateway": "universal-agent-gateway/0.1",
            "providers": self.registry.describe(),
            "routing": self.router.describe_routing(),
            "runs": count,
        }, self._secret_values)


async def run_event(run: TaskRun, event: Dict[str, Any]) -> None:
    run.add_event(event)


# ---------------------------------------------------------------------------
# HTTP API (stdlib)
# ---------------------------------------------------------------------------


def make_handler(orchestrator: Orchestrator):
    class Handler(BaseHTTPRequestHandler):
        server_version = "UniversalAgentGateway/0.1"

        def setup(self):
            super().setup()
            self.connection.settimeout(orchestrator.http_timeout_s)

        def _authenticated(self):
            value = self.headers.get('Authorization', '')
            return secrets.compare_digest(value.encode(), ('Bearer ' + orchestrator.api_token).encode())

        def log_message(self, fmt, *args):
            pass

        def _send(self, code: int, payload: Dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            if not self._authenticated():
                return self._send(401, {'error': 'authentication required'})
            path = self.path.rstrip("/")
            if path == "/health":
                return self._send(200, orchestrator.health())
            if path == "/tasks":
                return self._send(200, {"tasks": orchestrator.list_runs()})
            if path.startswith("/tasks/"):
                run = orchestrator.get(path.split("/tasks/", 1)[1])
                if run is None:
                    return self._send(404, {"error": "run não encontrado"})
                return self._send(200, run.to_dict())
            return self._send(404, {"error": f"rota desconhecida: {path}"})

        def do_POST(self):  # noqa: N802
            if not self._authenticated():
                return self._send(401, {'error': 'authentication required'})
            if self.path.rstrip("/") != "/task":
                return self._send(404, {"error": "rota desconhecida"})
            try:
                if self.headers.get('Transfer-Encoding'):
                    return self._send(400, {'error': 'Transfer-Encoding unsupported'})
                length = int(self.headers.get('Content-Length') or 0)
                if length < 1:
                    return self._send(400, {'error': 'invalid Content-Length'})
                if length > orchestrator.max_request_bytes:
                    return self._send(413, {'error': 'request exceeds byte limit'})
                data = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError, OSError):
                return self._send(400, {"error": "JSON inválido"})
            if not isinstance(data, dict) or set(data) - {'prompt', 'provider', 'verify', 'plan'}:
                return self._send(400, {'error': 'expected task object with known fields'})
            try:
                run = orchestrator.start_task(
                    data.get('prompt'), provider_id=data.get('provider'),
                    verify=data.get('verify'), plan_first=data.get('plan', True))
            except ValueError as exc:
                return self._send(400, {'error': str(exc)})
            except RuntimeError as exc:
                return self._send(503, {'error': str(exc)})
            return self._send(202, {"id": run.id, "status": run.status,
                                    "detail": f"GET /tasks/{run.id}"})

    return Handler


class GatewayHTTPServer(ThreadingHTTPServer):
    """Bound HTTP threads independently of the execution worker queue."""
    daemon_threads = True

    def __init__(self, address, handler, max_clients=16):
        self._clients = threading.BoundedSemaphore(max_clients)
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if not self._clients.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._clients.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._clients.release()


def serve(root: Optional[Path] = None, host: Optional[str] = None,
          port: Optional[int] = None) -> None:
    root = Path(root) if root else Path(__file__).resolve().parents[1]
    orchestrator = Orchestrator(root)
    host = host if host is not None else orchestrator.gateway_config.get('http_host', '127.0.0.1')
    port = port if port is not None else int(orchestrator.gateway_config.get('http_port', 8767))
    httpd = GatewayHTTPServer((host, port), make_handler(orchestrator),
                             orchestrator.max_http_clients)
    print(f"[gateway] ouvindo em http://{host}:{port} "
          f"(provedores: {len(orchestrator.registry.configs)})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        orchestrator.shutdown()


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="agent_gateway",
        description="Universal Agent Gateway — harness próprio (§7.2)")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--providers", action="store_true",
                        help="lista provedores e sai")
    parser.add_argument("--task", default=None,
                        help="executa uma tarefa única e imprime o relatório")
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parents[1]
    if args.providers or args.task:
        orchestrator = Orchestrator(root)
        try:
            if args.providers:
                print(json.dumps(orchestrator.health(), indent=2, ensure_ascii=False))
                return 0
            run = orchestrator.start_task(args.task)
            while run.to_dict()['finished_at'] is None:
                time.sleep(0.5)
            print(json.dumps(run.to_dict(), indent=2, ensure_ascii=False))
            return 0 if run.status == 'completed' else 1
        finally:
            orchestrator.shutdown()
    serve(root, args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
