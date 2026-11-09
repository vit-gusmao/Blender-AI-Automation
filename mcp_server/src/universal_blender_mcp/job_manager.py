"""Persistência de jobs em SQLite (storage/jobs.db) — §9.4 do plano."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    command_id TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    arguments TEXT,
    result TEXT,
    error TEXT,
    requested_by TEXT,
    idempotency_key TEXT,
    scene_revision INTEGER,
    attempts INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_jobs_idem ON jobs(idempotency_key);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
"""


class JobManager:
    """Registro de jobs com estados queued/running/completed/failed/cancelled."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            with self._conn:
                self._conn.execute("BEGIN IMMEDIATE")
                # Keep every legacy row, reserving a duplicated key for its oldest job.
                self._conn.execute(
                    "UPDATE jobs AS current SET idempotency_key = NULL"
                    " WHERE idempotency_key IS NOT NULL AND rowid !="
                    " (SELECT rowid FROM jobs AS original"
                    " WHERE original.idempotency_key = current.idempotency_key"
                    " ORDER BY created_at, rowid LIMIT 1)"
                )
                self._conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_idem_unique"
                    " ON jobs(idempotency_key) WHERE idempotency_key IS NOT NULL"
                )

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        for key in ("arguments", "result", "error", "requested_by"):
            if d.get(key) is not None:
                try:
                    d[key] = json.loads(d[key])
                except (json.JSONDecodeError, TypeError):
                    pass
        return d

    # -- API ---------------------------------------------------------------

    def create(self, command: Dict[str, Any]) -> Dict[str, Any]:
        return self.create_or_get(command)[0]

    def create_or_get(self, command: Dict[str, Any]):
        now = time.time()
        with self._lock:
            with self._conn:
                self._conn.execute("BEGIN IMMEDIATE")
                cur = self._conn.execute(
                "INSERT OR IGNORE INTO jobs (command_id, operation, status, created_at,"
                " arguments, requested_by, idempotency_key, scene_revision)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (
                    command["command_id"], command["operation"], "queued", now,
                    json.dumps(command.get("arguments", {}), ensure_ascii=False),
                    json.dumps(command.get("requested_by", {}), ensure_ascii=False),
                    command.get("idempotency_key"),
                    command.get("scene_revision"),
                ),
                )
                created = cur.rowcount == 1
                key = command.get("idempotency_key")
                row = None
                if key is not None:
                    row = self._conn.execute(
                        "SELECT * FROM jobs WHERE idempotency_key = ?", (key,)
                    ).fetchone()
                if row is None:
                    row = self._conn.execute(
                        "SELECT * FROM jobs WHERE command_id = ?", (command["command_id"],)
                    ).fetchone()
        return self._row_to_dict(row), created

    def reserve_idempotency(self, command: Dict[str, Any]):
        return self.create_or_get(command)

    def get(self, command_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE command_id = ?", (command_id,)
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def find_by_idempotency(self, key: str) -> Optional[Dict[str, Any]]:
        if key is None:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE idempotency_key = ? ORDER BY created_at DESC LIMIT 1",
                (key,),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def set_status(self, command_id: str, status: str,
                   error: Optional[Dict[str, Any]] = None) -> bool:
        return self.transition(command_id, {"queued", "running"}, status, error=error)

    def transition(self, command_id: str, from_statuses, to_status: str,
                   error: Optional[Dict[str, Any]] = None,
                   result: Optional[Dict[str, Any]] = None) -> bool:
        allowed = {"queued", "running"}.intersection(from_statuses)
        if not allowed or to_status not in {"running", "completed", "failed", "cancelled"}:
            return False
        if to_status == "running":
            allowed.discard("running")
        if to_status == "cancelled":
            allowed.discard("running")
        if not allowed:
            return False
        col = "started_at" if to_status == "running" else "finished_at"
        placeholders = ",".join("?" for _ in allowed)
        with self._lock, self._conn:
            cur = self._conn.execute(
                f"UPDATE jobs SET status = ?, {col} = ?, error = ?, result = ?,"
                f" scene_revision = COALESCE(?, scene_revision) WHERE command_id = ?"
                f" AND status IN ({placeholders})",
                (to_status, time.time(), json.dumps(error, ensure_ascii=False) if error is not None else None,
                 json.dumps(result, ensure_ascii=False, default=str) if result is not None else None,
                 result.get("scene_revision") if result is not None else None,
                 command_id, *allowed),
            )
        return cur.rowcount > 0

    def set_result(self, command_id: str, result: Dict[str, Any]) -> bool:
        status = result.get("status", "completed")
        if status not in {"completed", "failed", "cancelled"}:
            return False
        return self.transition(command_id, {"queued", "running"}, status,
                               error=result.get("error"), result=result)

    def reconcile_interrupted(self) -> int:
        error = {"type": "Interrupted", "message": "Job interrupted by server restart"}
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE jobs SET status = 'failed', finished_at = ?, error = ?, result = NULL"
                " WHERE status IN ('queued', 'running')",
                (time.time(), json.dumps(error)),
            )
        return cur.rowcount

    def cancel(self, command_id: str) -> Dict[str, Any]:
        job = self.get(command_id)
        if not job:
            return {"cancelled": False, "reason": "job não encontrado"}
        if job["status"] in ("completed", "failed", "cancelled"):
            return {"cancelled": False, "reason": f"job já está {job['status']}"}
        if job["status"] == "running":
            return {"cancelled": False, "reason":
                    "job em execução: cancelamento cooperativo não disponível nesta versão"}
        if not self.transition(command_id, {"queued"}, "cancelled"):
            return {"cancelled": False, "reason": "job mudou de estado"}
        return {"cancelled": True, "command_id": command_id, "status": "cancelled"}

    def list(self, status: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        query = "SELECT * FROM jobs"
        params: List[Any] = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def stats(self) -> Dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
            ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    def close(self) -> None:
        with self._lock:
            self._conn.close()
