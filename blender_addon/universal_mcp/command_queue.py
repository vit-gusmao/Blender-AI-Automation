# SPDX-License-Identifier: MIT
"""Filas thread-safe entre a thread de rede e o executor na thread principal.

A thread de poll (rede) entrega comandos aqui; o timer bpy.app.timers
(thread principal, §5.1) consome, executa com bpy e devolve resultados
para a thread de post.
"""
from __future__ import annotations

import queue
import threading
import time
from collections import deque
from typing import Any, Dict, List, Optional


class CommandQueue:
    def __init__(self, maxsize: int = 64):
        self._incoming: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=maxsize)
        # Soft limit: Blender's main thread must never block and a finished
        # command must never disappear. Overflow is retained and observable.
        self._outgoing = deque()
        self._result_lock = threading.Lock()
        self._maxsize = maxsize
        self.result_diagnostics = {"overflow": 0, "retries": 0,
                                   "last_error": None, "dead_letters": 0}
        self._delivery = {}
        self.dead_letters = []

    # -- comandos vindos da Bridge ---------------------------------------------

    def push_command(self, command: Dict[str, Any]) -> bool:
        try:
            self._incoming.put_nowait(command)
            return True
        except queue.Full:
            return False

    def pop_commands(self, max_items: int = 1) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for _ in range(max_items):
            try:
                out.append(self._incoming.get_nowait())
            except queue.Empty:
                break
        return out

    # -- resultados devolvidos pelo executor -------------------------------------

    def push_result(self, envelope: Dict[str, Any]) -> None:
        with self._result_lock:
            if len(self._outgoing) >= self._maxsize:
                self.result_diagnostics["overflow"] += 1
            self._outgoing.append(envelope)
            self._delivery[id(envelope)] = {"attempts": 0, "ready_at": 0.0}

    def peek_result(self) -> Optional[Dict[str, Any]]:
        with self._result_lock:
            return self._outgoing[0] if self._outgoing else None

    def ack_result(self, envelope: Dict[str, Any]) -> bool:
        with self._result_lock:
            return self._remove_result(envelope)

    def _remove_result(self, envelope):
        for index, item in enumerate(self._outgoing):
            if item is envelope:
                del self._outgoing[index]
                self._delivery.pop(id(envelope), None)
                return True
        return False

    def ready_results(self, max_items=8, now=None):
        now = time.monotonic() if now is None else now
        with self._result_lock:
            return [item for item in self._outgoing
                    if self._delivery[id(item)]["ready_at"] <= now][:max_items]

    def result_attempts(self, envelope):
        with self._result_lock:
            return self._delivery.get(id(envelope), {}).get("attempts", 0)

    def requeue_result(self, envelope: Dict[str, Any], error: str = "",
                       ready_at: float = 0.0) -> int:
        # An unacknowledged item never left the outbox.
        with self._result_lock:
            info = self._delivery.get(id(envelope))
            if info is None:
                return 0
            info["attempts"] += 1
            info["ready_at"] = ready_at
            self.result_diagnostics["retries"] += 1
            self.result_diagnostics["last_error"] = error
            return info["attempts"]

    def dead_letter_result(self, envelope, error):
        with self._result_lock:
            attempts = self._delivery.get(id(envelope), {}).get("attempts", 0)
            if not self._remove_result(envelope):
                return False
            self.dead_letters.append({"envelope": envelope, "error": error,
                                      "attempts": attempts, "timestamp": time.time()})
            self.result_diagnostics["dead_letters"] += 1
            self.result_diagnostics["last_error"] = error
            return True

    def pop_results(self, max_items: int = 8) -> List[Dict[str, Any]]:
        # Compatibility view. Delivery code must explicitly acknowledge items.
        with self._result_lock:
            return list(self._outgoing)[:max_items]

    @property
    def pending(self) -> int:
        return self._incoming.qsize()
