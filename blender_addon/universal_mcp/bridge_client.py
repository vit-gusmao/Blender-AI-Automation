# SPDX-License-Identifier: MIT
"""Cliente da Bridge rodando em thread dedicada (rede fora da thread principal).

Regras (§5.1): esta thread NUNCA toca bpy. Ela registra a instância,
faz long-poll de comandos, entrega à CommandQueue e devolve resultados.
O executor (bpy.app.timers, thread principal) consome a fila.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
import urllib.parse
from typing import Any, Dict, List, Optional

from .command_queue import CommandQueue

_HTTP_TIMEOUT = 15.0  # > long-poll máximo da Bridge (5s) + folga


def _log(msg: str) -> None:
    print(f"[universal_mcp] {msg}")


class _ResultDeliveryError(RuntimeError):
    def __init__(self, message, permanent=False):
        super().__init__(message)
        self.permanent = permanent


class BridgeClient(threading.Thread):
    """Poller autenticado contra a Bridge (camada 3 → camada 4)."""

    def __init__(self, host: str, port: int, token: str,
                 queue: Optional[CommandQueue] = None, wait_s: float = 1.0,
                 registration_payload: Optional[Dict[str, Any]] = None,
                 result_max_attempts: int = 8):
        super().__init__(name="universal-mcp-bridge", daemon=True)
        self.base_url = f"http://{host}:{port}"
        self.token = token
        self.queue = queue or CommandQueue()
        self.wait_s = wait_s
        self.stop_event = threading.Event()
        self.connected = False
        self.blender_id: Optional[str] = None
        self.last_error: Optional[str] = None
        self.last_poll = 0.0
        self.commands_received = 0
        self.results_sent = 0
        self._lock = threading.Lock()
        self.registration_payload = dict(registration_payload or {})
        self._registered = False
        self.result_max_attempts = max(1, int(result_max_attempts))

    # -- HTTP -------------------------------------------------------------------

    def _request(self, method: str, path: str,
                 payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        data = (json.dumps(payload, default=str).encode("utf-8")
                if payload is not None else None)
        req = urllib.request.Request(f"{self.base_url}{path}", data=data,
                                     method=method)
        req.add_header("Content-Type", "application/json; charset=utf-8")
        req.add_header("X-Bridge-Token", self.token)
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            out = json.loads(resp.read().decode("utf-8"))
            if not isinstance(out, dict):
                raise ValueError("Bridge response must be a JSON object")
            return out

    # -- ciclo --------------------------------------------------------------------

    def register(self) -> bool:
        try:
            out = self._request("POST", "/register", self.registration_payload)
            if not isinstance(out, dict) or not out.get("blender_id"):
                raise ValueError("registration missing blender_id")
            with self._lock:
                self.blender_id = out.get("blender_id")
                self.connected = True
                self._registered = True
                self.last_error = None
            _log(f"registrado na Bridge {self.base_url} como {self.blender_id}")
            return True
        except (urllib.error.URLError, OSError, ValueError) as exc:
            with self._lock:
                self.connected = False
                self.last_error = str(exc)
            return False

    def stop(self) -> None:
        self.stop_event.set()

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "url": self.base_url,
                "connected": self.connected,
                "blender_id": self.blender_id,
                "last_error": self.last_error,
                "commands_received": self.commands_received,
                "results_sent": self.results_sent,
                "queue_pending": self.queue.pending,
                "outbox": dict(self.queue.result_diagnostics),
            }

    # -- thread principal ----------------------------------------------------------

    def run(self) -> None:
        backoff = 1.0
        while not self.stop_event.is_set():
            try:
                if not self._registered:
                    if not self.register():
                        self.stop_event.wait(backoff)
                        backoff = min(backoff * 2, 15.0)
                        continue
                self._drain_results()
                out = self._request("GET", f"/poll?wait={self.wait_s}")
                if not isinstance(out, dict):
                    raise ValueError("poll response must be a JSON object")
                with self._lock:
                    self.connected = True
                    self.last_error = None
                    self.last_poll = time.time()
                command = out.get("command")
                if command:
                    if not isinstance(command, dict):
                        raise ValueError("poll command must be a JSON object")
                    if self.queue.push_command(command):
                        self.commands_received += 1
                    else:  # fila local cheia → rejeita de volta como falha
                        self.queue.push_result({
                            "command_id": command.get("command_id"),
                            "status": "failed", "success": False,
                            "data": None, "scene_revision": None,
                            "error": {"type": "AddonQueueFull",
                                      "message": "fila do addon cheia"},
                            "final": True,
                        })
                self._drain_results()
                backoff = 1.0
            except urllib.error.HTTPError as exc:
                if exc.code == 401:
                    with self._lock:
                        self.last_error = "token rejeitado pela Bridge (401)"
                    _log(self.last_error)
                    self.stop_event.wait(5.0)
                else:
                    self._fail(str(exc), backoff)
                    backoff = min(backoff * 2, 15.0)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                self._fail(str(exc), backoff)
                backoff = min(backoff * 2, 15.0)
        with self._lock:
            self.connected = False

    def _fail(self, message: str, backoff: float) -> None:
        with self._lock:
            self.connected = False
            self._registered = False
            self.last_error = message
        self.stop_event.wait(backoff)

    def _drain_results(self, now=None) -> None:
        now = time.monotonic() if now is None else now
        for envelope in self.queue.ready_results(max_items=8, now=now):
            try:
                self._post_result(envelope)
            except _ResultDeliveryError as exc:
                attempts = self.queue.result_attempts(envelope) + 1
                delay = min(2 ** min(attempts - 1, 4), 15.0)
                self.queue.requeue_result(envelope, str(exc), ready_at=now + delay)
                if exc.permanent or attempts >= self.result_max_attempts:
                    self.queue.dead_letter_result(envelope, str(exc))
                    _log(f"resultado preservado em dead-letter: {envelope.get('command_id')}: {exc}")
                continue
            self.queue.ack_result(envelope)

    def _post_result(self, envelope: Dict[str, Any]) -> bool:
        try:
            out = self._request("POST", "/results", envelope)
            if not isinstance(out, dict) or not (out.get("ok") is True or out.get("accepted") is True):
                raise ValueError("result acknowledgement rejected")
            self.results_sent += 1
            return True
        except (urllib.error.URLError, OSError, ValueError) as exc:
            # An accepted POST may lose its response. Reconcile with durable
            # command state before retrying against a non-idempotent endpoint.
            state = self._reconcile_result(envelope)
            if state == "accepted":
                self.results_sent += 1
                return True
            if state == "conflict":
                raise _ResultDeliveryError(f"result conflict with persisted command: {exc}", True) from exc
            permanent = isinstance(exc, urllib.error.HTTPError) and (
                400 <= exc.code < 500 and exc.code not in (401, 408, 409, 429))
            raise _ResultDeliveryError(str(exc), permanent) from exc

    def _reconcile_result(self, envelope):
        cid = envelope.get("command_id")
        if not isinstance(cid, str) or not cid:
            return "unknown"
        try:
            # Match the actual JSON representation sent over HTTP (tuples and
            # other default=str values may differ from their in-process form).
            envelope = json.loads(json.dumps(envelope, default=str))
            out = self._request("GET", "/commands/" + urllib.parse.quote(cid, safe=""))
            if not isinstance(out, dict):
                return "unknown"
            job = out.get("job") or {}
            stored = out.get("result") or (job.get("result") if isinstance(job, dict) else None)
            if isinstance(stored, dict):
                keys = ("command_id", "status", "success", "data", "scene_revision", "error")
                if (all(stored.get(key) == envelope.get(key) for key in keys)
                        and stored.get("final", stored.get("status") != "running") ==
                        envelope.get("final", envelope.get("status") != "running")):
                    return "accepted"
                if stored.get("status") in ("completed", "failed", "cancelled"):
                    return "conflict"
            return "unknown"
        except (urllib.error.URLError, OSError, ValueError):
            return "unknown"


# ---------------------------------------------------------------------------
# Instância ativa do addon (gerenciada por operators)
# ---------------------------------------------------------------------------

_client: Optional[BridgeClient] = None
_retained_queue: Optional[CommandQueue] = None


def get_client() -> Optional[BridgeClient]:
    return _client


def start_client(prefs) -> BridgeClient:
    global _client
    from . import dispatcher
    from .compatibility.blender_36 import version_info
    if not stop_client():
        raise RuntimeError("previous Bridge client has not stopped")
    payload = version_info()
    payload["capabilities"] = sorted(dispatcher.operation_names())
    client = BridgeClient(
        prefs.bridge_host, int(prefs.bridge_port), prefs.bridge_token,
        queue=_retained_queue, registration_payload=payload,
        wait_s=float(getattr(prefs, "poll_wait_s", 1.0)),
    )
    client.start()
    _client = client
    return client


def stop_client(join_timeout: float = _HTTP_TIMEOUT + 1) -> bool:
    global _client, _retained_queue
    if _client is not None:
        _client.stop()
        if _client.is_alive():
            _client.join(timeout=join_timeout)
        if _client.is_alive():
            return False
        _retained_queue = _client.queue
        _client = None
    return True
