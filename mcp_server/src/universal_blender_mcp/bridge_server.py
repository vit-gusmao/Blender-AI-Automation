"""Bridge local autenticada entre o servidor MCP e o addon do Blender (§5, §12-Etapa 4).

- Recebe comandos do servidor MCP (POST /commands), autentica por token,
  enfileira, entrega ao addon por long-poll (GET /poll) e recebe resultados
  (POST /results). Execução SERIAL por instância — dois agentes nunca
  modificam a mesma cena simultaneamente.
- stdlib apenas (http.server + sqlite via JobManager) para rodar em qualquer
  Python >= 3.10 sem dependências.
"""
from __future__ import annotations

import hmac
import copy
import json
import logging
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, unquote, urlparse

from .job_manager import JobManager
from .schemas import (ResultEnvelope, TERMINAL_STATUSES, validate_command_envelope,
                      validate_result_envelope)

log = logging.getLogger(__name__)

MAX_BODY = 8 * 1024 * 1024
LONGPOLL_MAX_S = 5.0
RESULT_CACHE_MAX = 256


class BridgeState:
    def __init__(self, token: str, jobs: JobManager, queue_max: int = 16,
                 command_timeout_s: float = 120.0, heartbeat_timeout_s: float = 15.0):
        self.token = token
        self.jobs = jobs
        self.queue_max = queue_max
        self.command_timeout_s = command_timeout_s
        self.heartbeat_timeout_s = heartbeat_timeout_s
        self.started_at = time.time()
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.queue: list[Dict[str, Any]] = []
        self.blender: Optional[Dict[str, Any]] = None
        self.last_poll = 0.0
        self.last_revision: Optional[int] = None
        self.active_command: Optional[str] = None
        self.active_since = 0.0
        self.active_deadline = 0.0
        self.results: Dict[str, Dict[str, Any]] = {}

    # -- auth ----------------------------------------------------------------

    def check_token(self, presented: Optional[str]) -> bool:
        return bool(presented) and hmac.compare_digest(str(presented), self.token)

    # -- fila ----------------------------------------------------------------

    def blender_connected(self) -> bool:
        return (
            self.blender is not None
            and (time.time() - self.last_poll) <= self.heartbeat_timeout_s
        )

    def enqueue(self, command: Dict[str, Any]) -> Dict[str, Any]:
        # Lock order is always Bridge condition -> JobManager. Creation, queue
        # ownership and durable transitions must never be split across handlers.
        with self.cond:
            errors = validate_command_envelope(command)
            if errors:
                raise ValueError("; ".join(errors))
            command = copy.deepcopy(command)
            now = time.time()
            timeout = command.get("timeout_s")
            timeout = self.command_timeout_s if timeout is None else float(timeout)
            command["deadline_at"] = min(
                command.get("deadline_at", float("inf")),
                command.get("created_at", now) + timeout,
                now + self.command_timeout_s,
            )
            self._expire_due_locked()
            job, created = self.jobs.create_or_get(command)
            cid = job["command_id"]
            if not created:
                return {"command_id": cid, "status": job["status"],
                        "idempotent_replay": True,
                        "result": copy.deepcopy(job.get("result") or self.results.get(cid))}
            if len(self.queue) >= self.queue_max:
                payload = ResultEnvelope.fail(
                    cid, "QueueFull", f"fila cheia ({self.queue_max})").to_dict()
                self._finish_locked(payload)
                return {"command_id": cid, "status": "rejected", "error": payload["error"]}
            self.queue.append(command)
            self.cond.notify_all()
            return {"command_id": cid, "status": "queued"}

    def _remember_locked(self, payload: Dict[str, Any]) -> None:
        cid = payload["command_id"]
        self.results.pop(cid, None)
        self.results[cid] = copy.deepcopy(payload)
        while len(self.results) > RESULT_CACHE_MAX:
            self.results.pop(next(iter(self.results)))

    def _finish_locked(self, payload: Dict[str, Any]) -> bool:
        cid = payload["command_id"]
        if not self.jobs.set_result(cid, payload):
            return False
        self.queue = [command for command in self.queue if command["command_id"] != cid]
        if self.active_command == cid:
            self.active_command = None
            self.active_since = 0.0
            self.active_deadline = 0.0
        self._remember_locked(payload)
        self.cond.notify_all()
        return True

    def cancel(self, command_id: str) -> Dict[str, Any]:
        with self.cond:
            payload = ResultEnvelope.fail(
                command_id, "Cancelled", "comando cancelado antes da execução",
                status="cancelled").to_dict()
            if self._finish_locked(payload):
                return {"cancelled": True, "command_id": command_id, "status": "cancelled"}
            job = self.jobs.get(command_id)
            reason = (f"job já está {job['status']}" if job else "job não encontrado")
            return {"cancelled": False, "reason": reason}

    def _expire_locked(self, command_id: str, reason: str) -> bool:
        return self._finish_locked(ResultEnvelope.fail(command_id, "Timeout", reason).to_dict())

    def expire(self, command_id: str, reason: str) -> bool:
        with self.cond:
            return self._expire_locked(command_id, reason)

    def _expire_due_locked(self) -> None:
        now = time.time()
        for command in list(self.queue):
            if command["deadline_at"] <= now:
                self._expire_locked(command["command_id"], "tempo limite da Bridge excedido")
        if self.active_command and self.active_deadline <= now:
            self._expire_locked(self.active_command, "tempo limite da Bridge excedido")

    def poll_command(self, wait_s: float) -> Optional[Dict[str, Any]]:
        deadline = time.monotonic() + min(wait_s, LONGPOLL_MAX_S)
        with self.cond:
            while True:
                self._expire_due_locked()
                # execução serial: nada novo enquanto um comando estiver ativo
                if self.active_command is None and self.queue:
                    command = self.queue.pop(0)
                    if not self.jobs.transition(command["command_id"], {"queued"}, "running"):
                        continue
                    self.active_command = command["command_id"]
                    self.active_since = time.time()
                    self.active_deadline = command["deadline_at"]
                    self.last_poll = time.time()
                    return command
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.last_poll = time.time()
                    return None
                self.cond.wait(timeout=min(remaining, 0.5))

    def submit_result(self, payload: Dict[str, Any]) -> bool:
        if validate_result_envelope(payload):
            return False
        cid = payload["command_id"]
        with self.cond:
            self._expire_due_locked()
            if self.active_command != cid:
                return False
            job = self.jobs.get(cid)
            if not job or job["status"] != "running":
                return False
            if payload["status"] in TERMINAL_STATUSES:
                if not self._finish_locked(payload):
                    return False
            else:
                self._remember_locked(payload)
            if payload.get("scene_revision") is not None:
                self.last_revision = payload["scene_revision"]
            self.cond.notify_all()
        return True

    def status(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "ok": True,
                "bridge": "universal-blender-mcp/bridge",
                "uptime_s": round(time.time() - self.started_at, 1),
                "queue_depth": len(self.queue),
                "active_command": self.active_command,
                "last_revision": self.last_revision,
                "blender_connected": self.blender_connected(),
                "blender": self.blender,
                "job_stats": self.jobs.stats(),
            }

    def watchdog_tick(self) -> None:
        """Expira comandos enfileirados ou ativos sem estender seu deadline."""
        with self.cond:
            self._expire_due_locked()


class BridgeHandler(BaseHTTPRequestHandler):
    server_version = "UniversalBlenderBridge/0.1"
    state: BridgeState = None  # type: ignore[assignment]

    # -- infra ----------------------------------------------------------------

    def log_message(self, fmt, *args) -> None:  # silencia stdout por requisição
        log.debug(fmt, *args)

    def _send(self, code: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization") or ""
        token = self.headers.get("X-Bridge-Token")
        if not token and header.startswith("Bearer "):
            token = header[7:]
        return self.state.check_token(token)

    # -- rotas ---------------------------------------------------------------

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path == "/status":
            return self._send(200, self.state.status())
        if not self._authorized():
            return self._send(401, {"ok": False, "error": "token inválido ou ausente"})
        if path == "/health":
            return self._send(200, self.state.status())
        if path == "/blender/info":
            if self.state.blender is None:
                return self._send(404, {"ok": False, "error": "Blender não registrado"})
            return self._send(200, {"ok": True, "blender": self.state.blender,
                                    "connected": self.state.blender_connected()})
        if path == "/poll":
            qs = parse_qs(parsed.query)
            wait = float((qs.get("wait") or ["1"])[0])
            command = self.state.poll_command(wait)
            return self._send(200, {"ok": True, "command": command})
        if path.startswith("/commands/"):
            cid = unquote(path.split("/commands/", 1)[1])
            with self.state.cond:
                job = self.state.jobs.get(cid)
                if not job:
                    return self._send(404, {"ok": False, "error": "comando desconhecido"})
                result = copy.deepcopy(job.get("result") or self.state.results.get(cid))
            return self._send(200, {"ok": True, "job": job, "result": result})
        return self._send(404, {"ok": False, "error": f"rota desconhecida: {path}"})

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        if path == "/register":
            if not self._authorized():
                return self._send(401, {"ok": False, "error": "token inválido ou ausente"})
            body = self._body()
            with self.state.cond:
                self.state.blender = {
                    "blender_id": body.get("blender_id") or f"blender-{uuid.uuid4().hex[:8]}",
                    "blender_version": body.get("blender_version"),
                    "python_version": body.get("python_version"),
                    "platform": body.get("platform"),
                    "capabilities": body.get("capabilities", []),
                    "registered_at": time.time(),
                }
                self.state.last_poll = time.time()
            return self._send(200, {"ok": True, "blender_id": self.state.blender["blender_id"]})
        if not self._authorized():
            return self._send(401, {"ok": False, "error": "token inválido ou ausente"})
        if path == "/commands":
            return self._handle_submit()
        if path == "/results":
            payload = self._body()
            ok = self.state.submit_result(payload)
            return self._send(200 if ok else 400, {"ok": ok})
        if path.startswith("/commands/") and path.endswith("/expire"):
            cid = unquote(path[len("/commands/"):-len("/expire")])
            reason = self._body().get("reason")
            if (not cid.strip() or "/" in cid
                    or not isinstance(reason, str) or not reason.strip()):
                return self._send(400, {"ok": False, "error": "command_id e reason inválidos"})
            expired = self.state.expire(cid, reason)
            return self._send(200, {"ok": True, "command_id": cid, "expired": expired})
        if path.startswith("/commands/") and path.endswith("/cancel"):
            cid = path[len("/commands/"):-len("/cancel")]
            outcome = self.state.cancel(cid)
            return self._send(200, {"ok": True, **outcome})
        return self._send(404, {"ok": False, "error": f"rota desconhecida: {path}"})

    def _handle_submit(self):
        command = self._body()
        if not command.get("operation"):
            return self._send(400, {"ok": False, "error": "campo 'operation' obrigatório"})
        command.setdefault("command_id", f"cmd-{uuid.uuid4().hex[:12]}")
        command.setdefault("arguments", {})
        command.setdefault("dry_run", False)

        try:
            outcome = self.state.enqueue(command)
        except ValueError as exc:
            return self._send(400, {"ok": False, "error": str(exc)})
        code = 200 if outcome["status"] != "rejected" else 503
        return self._send(code, {"ok": outcome["status"] != "rejected", **outcome})


class BridgeServer:
    """Wrapper da Bridge: sobe em thread própria e pode rodar standalone."""

    def __init__(self, host: str, port: int, token: str, jobs: JobManager,
                 queue_max: int = 16, command_timeout_s: float = 120.0,
                 heartbeat_timeout_s: float = 15.0):
        self.state = BridgeState(token, jobs, queue_max, command_timeout_s,
                                 heartbeat_timeout_s)

        handler = type("BoundBridgeHandler", (BridgeHandler,), {"state": self.state})
        self.httpd = ThreadingHTTPServer((host, port), handler)
        self.httpd.daemon_threads = True
        self.host, self.port = self.httpd.server_address[:2]
        self._thread: Optional[threading.Thread] = None
        self._watchdog_stop = threading.Event()
        self._watchdog: Optional[threading.Thread] = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self, daemon: bool = True) -> str:
        self._thread = threading.Thread(target=self.httpd.serve_forever,
                                        kwargs={"poll_interval": 0.1},
                                        name="bridge-http", daemon=daemon)
        self._thread.start()
        self._watchdog = threading.Thread(target=self._watchdog_loop, daemon=daemon)
        self._watchdog.start()
        log.info("Bridge ouvindo em %s", self.url)
        return self.url

    def _watchdog_loop(self) -> None:
        while not self._watchdog_stop.wait(2.0):
            try:
                self.state.watchdog_tick()
            except Exception:  # noqa: BLE001 — watchdog nunca pode derrubar a bridge
                log.exception("watchdog falhou")

    def stop(self) -> None:
        self._watchdog_stop.set()
        self.httpd.shutdown()
        self.httpd.server_close()


def run_bridge(settings) -> BridgeServer:
    """Cria e sobe a Bridge com os settings do projeto (bloqueia com .serve())."""
    jobs = JobManager(settings.jobs_db)
    server = BridgeServer(
        settings.bridge_host, settings.bridge_port, settings.bridge_token(),
        jobs, settings.queue_max, settings.command_timeout_s,
        settings.heartbeat_timeout_s,
    )
    server.start(daemon=False)
    print(f"[bridge] ouvindo em {server.url} (token em {settings.token_file})")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.stop()
    return server
