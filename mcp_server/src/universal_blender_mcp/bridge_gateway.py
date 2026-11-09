"""Cliente da Bridge usado pelas ferramentas MCP (camada 3 → camada 4).

Envia comandos, aguarda resultado (polling) e devolve o envelope
padronizado. HTTP stdlib rodando em thread para o lado async.
"""
from __future__ import annotations

import asyncio
import copy
import http.client
import io
import ipaddress
import json
import logging
import multiprocessing
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional
from urllib.parse import quote, urlsplit

from .schemas import ResultEnvelope, build_command

log = logging.getLogger(__name__)


class BridgeUnavailable(Exception):
    pass


class _DeadlineElapsed(BridgeUnavailable):
    pass


def _resolve_in_process(host, port, sender, resolver):
    with sender:
        try:
            sender.send((True, resolver(host, port, 0, socket.SOCK_STREAM)))
        except Exception as exc:
            sender.send((False, str(exc)))


def _resolve_addresses(host, port, expires):
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        if address.version == 4:
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (str(address), port))]
        host, _, scope = str(address).partition("%")
        scope_id = (int(scope) if scope.isdigit() else socket.if_nametoindex(scope)) if scope else 0
        return [(socket.AF_INET6, socket.SOCK_STREAM, 0, "", (host, port, 0, scope_id))]

    # getaddrinfo has no cancellable timeout in stdlib. Give hostname resolution
    # a killable process and always reap it; never abandon a blocked DNS thread.
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_resolve_in_process,
                              args=(host, port, sender, socket.getaddrinfo),
                              name="bridge-resolver", daemon=True)
    try:
        if time.monotonic() >= expires:
            raise _DeadlineElapsed("deadline durante resolução da Bridge")
        process.start()
        sender.close()
        remaining = expires - time.monotonic()
        if remaining <= 0 or not receiver.poll(remaining):
            raise _DeadlineElapsed("deadline durante resolução da Bridge")
        try:
            ok, result = receiver.recv()
        except EOFError as exc:
            raise OSError("resolução da Bridge interrompida") from exc
        if not ok:
            raise OSError(result)
        return result
    finally:
        receiver.close()
        sender.close()
        if process.pid is not None:
            if process.is_alive():
                process.terminate()
            process.join()
            process.close()


def _connect_with_deadline(address, expires, source_address=None):
    addresses = _resolve_addresses(address[0], address[1], expires)
    error = None
    for family, kind, protocol, _, destination in addresses:
        remaining = expires - time.monotonic()
        if remaining <= 0:
            raise _DeadlineElapsed("deadline durante conexão à Bridge")
        sock = socket.socket(family, kind, protocol)
        try:
            sock.settimeout(remaining)
            if source_address:
                sock.bind(source_address)
            sock.connect(destination)
            remaining = expires - time.monotonic()
            if remaining <= 0:
                raise _DeadlineElapsed("deadline durante conexão à Bridge")
            sock.settimeout(remaining)
            return sock
        except OSError as exc:
            sock.close()
            error = exc
        except BaseException:
            sock.close()
            raise
    raise error or OSError("nenhum endereço disponível para a Bridge")


class _DeadlineReader(io.RawIOBase):
    def __init__(self, transport):
        self.transport = transport

    def readable(self):
        return True

    def readinto(self, buffer):
        self.transport._set_remaining_timeout()
        return self.transport.socket.recv_into(buffer)

    def close(self):
        if not self.closed:
            self.transport._release_reader()
        super().close()


class _DeadlineSocket:
    """Socket file reads recalculate the absolute budget even inside headers."""

    def __init__(self, sock, expires: float):
        self.socket = sock
        self.expires = expires
        self.readers = 0
        self.closed = False

    def _set_remaining_timeout(self):
        remaining = self.expires - time.monotonic()
        if remaining <= 0:
            raise _DeadlineElapsed("deadline da requisição excedido")
        self.socket.settimeout(remaining)

    def sendall(self, data):
        self._set_remaining_timeout()
        self.socket.sendall(data)

    def makefile(self, mode):
        self.readers += 1
        return io.BufferedReader(_DeadlineReader(self))

    def _release_reader(self):
        self.readers -= 1
        if self.closed and not self.readers:
            self.socket.close()

    def close(self):
        # HTTPConnection can close its socket before returning an HTTPResponse.
        # Keep it usable until the response's file is also closed.
        self.closed = True
        if not self.readers:
            self.socket.close()


class BridgeGateway:
    def __init__(self, base_url: str, token: str, command_timeout_s: float = 120.0,
                 poll_interval_s: float = 0.15, request_timeout_s: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.command_timeout_s = command_timeout_s
        self.poll_interval_s = poll_interval_s
        self.request_timeout_s = request_timeout_s

    # -- HTTP cru -------------------------------------------------------------

    def _request(self, method: str, path: str,
                 payload: Optional[Dict[str, Any]] = None, *,
                 deadline_at: Optional[float] = None) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json; charset=utf-8")
        req.add_header("Authorization", f"Bearer {self.token}")
        timeout = self.request_timeout_s
        if deadline_at is not None:
            timeout = min(timeout, deadline_at - time.time())
        if timeout <= 0:
            raise _DeadlineElapsed("deadline da requisição excedido")
        expires = time.monotonic() + timeout
        body = self._exchange(req, timeout, expires, path)
        if time.monotonic() >= expires:
            raise _DeadlineElapsed("deadline da requisição excedido")
        return body

    @staticmethod
    def _read_body(response, expires: float) -> bytes:
        chunks = []
        while True:
            if time.monotonic() >= expires:
                raise _DeadlineElapsed("deadline da requisição excedido")
            # read1 performs at most one underlying read; read() can wait for
            # the entire body indefinitely while bytes continue to trickle in.
            chunk = response.read1(65536)
            if time.monotonic() >= expires:
                raise _DeadlineElapsed("deadline da requisição excedido")
            if not chunk:
                if getattr(response, "length", None) not in (None, 0):
                    raise http.client.IncompleteRead(b"".join(chunks), response.length)
                return b"".join(chunks)
            chunks.append(chunk)

    def _exchange(self, req, timeout: float, expires: float, path: str) -> Dict[str, Any]:
        url = urlsplit(req.full_url)
        if url.scheme not in ("http", "https") or not url.hostname:
            raise BridgeUnavailable("URL da Bridge deve usar HTTP ou HTTPS")
        connection_type = (http.client.HTTPSConnection if url.scheme == "https"
                           else http.client.HTTPConnection)
        connection = connection_type(url.hostname, url.port, timeout=timeout)
        connection._create_connection = lambda address, timeout, source_address=None: (
            _connect_with_deadline(address, expires, source_address))
        try:
            connection.connect()
            connection.sock = _DeadlineSocket(connection.sock, expires)
            target = url.path or "/"
            if url.query:
                target += "?" + url.query
            connection.request(req.method, target, body=req.data,
                               headers=dict(req.header_items()))
            with connection.getresponse() as resp:
                raw = self._read_body(resp, expires)
                if resp.status >= 400:
                    try:
                        body = json.loads(raw.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        body = {}
                    error = body.get("error", resp.reason) if isinstance(body, dict) else resp.reason
                    raise BridgeUnavailable(f"HTTP {resp.status} em {path}: {error}")
                body = json.loads(raw.decode("utf-8"))
                if not isinstance(body, dict):
                    raise BridgeUnavailable("resposta da Bridge deve ser objeto JSON")
                return body
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError,
                http.client.HTTPException) as exc:
            raise BridgeUnavailable(f"Bridge inacessível em {self.base_url}: {exc}") from exc
        except (ValueError, UnicodeDecodeError) as exc:
            raise BridgeUnavailable("resposta JSON inválida da Bridge") from exc
        finally:
            connection.close()

    # -- API -------------------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        status = self._request("GET", "/status")
        if status.get("bridge") != "universal-blender-mcp/bridge":
            raise BridgeUnavailable("serviço remoto não é universal-blender-mcp/bridge")
        return status

    def blender_info(self) -> Dict[str, Any]:
        return self._request("GET", "/blender/info")

    def submit(self, command: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", "/commands", command,
                             deadline_at=command.get("deadline_at"))

    def get(self, command_id: str, *, deadline_at: Optional[float] = None) -> Dict[str, Any]:
        return self._request("GET", f"/commands/{quote(command_id, safe='')}",
                             deadline_at=deadline_at)

    def cancel(self, command_id: str) -> Dict[str, Any]:
        return self._request("POST", f"/commands/{command_id}/cancel", {})

    # -- execução de alto nível --------------------------------------------------

    def _timeout_result(self, command_id: str) -> Dict[str, Any]:
        reason = "tempo limite aguardando resultado"
        # Reconciliation has its own bounded transport budget after execution's
        # deadline. The submitted deadline remains unchanged if this request fails.
        try:
            self._request("POST", f"/commands/{quote(command_id, safe='')}/expire",
                          {"reason": reason},
                          deadline_at=time.time() + min(self.request_timeout_s, 1.0))
        except BridgeUnavailable:
            log.warning("não foi possível reconciliar timeout de %s", command_id)
        return ResultEnvelope.fail(command_id, "Timeout", reason).to_dict()

    def wait_for_result(self, command_id: str, timeout_s: Optional[float] = None, *,
                        deadline_at: Optional[float] = None) -> Dict[str, Any]:
        if deadline_at is None:
            deadline_at = time.time() + (self.command_timeout_s if timeout_s is None else timeout_s)
        while time.time() < deadline_at:
            try:
                info = self.get(command_id, deadline_at=deadline_at)
            except BridgeUnavailable:
                if time.time() >= deadline_at:
                    return self._timeout_result(command_id)
                raise
            if time.time() >= deadline_at:
                return self._timeout_result(command_id)
            job = info.get("job") or {}
            if job.get("status") in ("completed", "failed", "cancelled"):
                result = job.get("result") or info.get("result")
                if result and result.get("status") in ("completed", "failed", "cancelled"):
                    return copy.deepcopy(result)
                if job["status"] == "completed":
                    return ResultEnvelope.fail(command_id, "ResultUnavailable",
                                               "job concluído sem resultado persistido").to_dict()
                return {
                    "command_id": command_id, "status": job["status"],
                    "success": False,
                    "data": None, "scene_revision": job.get("scene_revision"),
                    "error": copy.deepcopy(job.get("error")) or (
                        {"type": job["status"].title(),
                         "message": f"job terminou como {job['status']} sem resultado"}),
                }
            remaining = deadline_at - time.time()
            if remaining > 0:
                time.sleep(min(self.poll_interval_s, remaining))
        return self._timeout_result(command_id)

    def execute_sync(self, operation: str, arguments: Optional[Dict[str, Any]] = None,
                     **kwargs) -> Dict[str, Any]:
        command = build_command(operation, arguments, **kwargs)
        command_id = command["command_id"]
        timeout = command.get("timeout_s")
        command["deadline_at"] = command["created_at"] + (
            self.command_timeout_s if timeout is None else timeout)
        deadline_at = command["deadline_at"]
        try:
            outcome = self.submit(command)
            if outcome.get("idempotent_replay"):
                command_id = outcome["command_id"]
            if time.time() >= deadline_at:
                return self._timeout_result(command_id)
            if not outcome.get("ok"):
                return {
                    "command_id": command_id, "status": "failed",
                    "success": False, "data": None, "scene_revision": None,
                    "error": {"type": "BridgeError",
                              "message": outcome.get("error") or "submissão recusada"},
                }
            if outcome.get("idempotent_replay"):
                command_id = outcome["command_id"]
                result = outcome.get("result")
                if (outcome.get("status") in ("completed", "failed", "cancelled")
                        and result and result.get("status") in ("completed", "failed", "cancelled")):
                    return copy.deepcopy(result)
            return self.wait_for_result(command_id, deadline_at=deadline_at)
        except BridgeUnavailable as exc:
            if time.time() >= deadline_at:
                return self._timeout_result(command_id)
            return ResultEnvelope.fail(command_id, "BridgeUnavailable", str(exc)).to_dict()

    async def execute(self, operation: str, arguments: Optional[Dict[str, Any]] = None,
                      **kwargs) -> Dict[str, Any]:
        return await asyncio.to_thread(self.execute_sync, operation, arguments, **kwargs)

    # -- ciclo de vida ----------------------------------------------------------

    def is_reachable(self) -> bool:
        try:
            self.status()
            return True
        except BridgeUnavailable:
            return False
