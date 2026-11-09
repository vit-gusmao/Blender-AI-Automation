"""Unit: persistência de jobs em SQLite (§9.4)."""
from __future__ import annotations

import time
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from universal_blender_mcp.job_manager import JobManager
from universal_blender_mcp.schemas import build_command


def test_create_get_result_flow(tmp_path):
    jobs = JobManager(tmp_path / "jobs.db")
    command = build_command("object_create", {"type": "CUBE"},
                            command_id="cmd-1", idempotency_key="k1")
    job = jobs.create(command)
    assert job["status"] == "queued"
    assert job["operation"] == "object_create"

    assert jobs.set_status("cmd-1", "running")
    assert jobs.get("cmd-1")["status"] == "running"

    jobs.set_result("cmd-1", {"command_id": "cmd-1", "status": "completed",
                              "success": True, "data": {"x": 1},
                              "scene_revision": 2, "error": None})
    job = jobs.get("cmd-1")
    assert job["status"] == "completed"
    assert job["result"]["data"] == {"x": 1}


def test_idempotency_lookup(tmp_path):
    jobs = JobManager(tmp_path / "jobs.db")
    jobs.create(build_command("object_create", idempotency_key="idem-1"))
    found = jobs.find_by_idempotency("idem-1")
    assert found is not None and found["idempotency_key"] == "idem-1"
    assert jobs.find_by_idempotency("nope") is None


def test_cancel_only_pending(tmp_path):
    jobs = JobManager(tmp_path / "jobs.db")
    jobs.create(build_command("render_final", command_id="c1"))
    out = jobs.cancel("c1")
    assert out["cancelled"] is True
    assert jobs.get("c1")["status"] == "cancelled"
    # cancelar de novo → recusa
    assert jobs.cancel("c1")["cancelled"] is False
    # running → cancelamento cooperativo indisponível nesta versão
    jobs.create(build_command("render_final", command_id="c2"))
    jobs.set_status("c2", "running")
    assert jobs.cancel("c2")["cancelled"] is False


def test_list_and_stats(tmp_path):
    jobs = JobManager(tmp_path / "jobs.db")
    for i in range(3):
        jobs.create(build_command("blender_ping", command_id=f"c{i}"))
        jobs.set_status(f"c{i}", "completed")
    jobs.create(build_command("blender_ping", command_id="cX"))
    stats = jobs.stats()
    assert stats == {"completed": 3, "queued": 1}
    listed = jobs.list(status="completed")
    assert len(listed) == 3
    jobs.close()


def test_concurrent_idempotency_reserves_only_one_job(tmp_path):
    path = tmp_path / "jobs.db"
    managers = [JobManager(path), JobManager(path)]
    barrier = Barrier(2)

    def reserve(index):
        command = build_command("blender_ping", command_id=f"c{index}",
                                idempotency_key="shared")
        barrier.wait()
        return managers[index].create_or_get(command)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, range(2)))
    assert sum(created for _, created in results) == 1
    assert results[0][0]["command_id"] == results[1][0]["command_id"]
    assert len(managers[0].list()) == 1
    for manager in managers:
        manager.close()


def test_terminal_state_cannot_be_overwritten(tmp_path):
    jobs = JobManager(tmp_path / "jobs.db")
    jobs.create(build_command("blender_ping", command_id="c1"))
    assert jobs.cancel("c1")["cancelled"]
    assert not jobs.transition("c1", {"cancelled"}, "completed")
    assert not jobs.set_result("c1", {"status": "completed"})
    assert jobs.get("c1")["status"] == "cancelled"


def test_failed_result_persists_error_consistently(tmp_path):
    path = tmp_path / "jobs.db"
    jobs = JobManager(path)
    jobs.create(build_command("blender_ping", command_id="c1"))
    error = {"type": "Failure", "message": "bad"}
    result = {"status": "failed", "success": False, "error": error}
    assert jobs.set_result("c1", result)
    jobs.close()
    reopened = JobManager(path)
    job = reopened.get("c1")
    assert job["status"] == "failed"
    assert job["result"] == result
    assert job["error"] == error


def test_reconcile_interrupted_after_restart(tmp_path):
    path = tmp_path / "jobs.db"
    jobs = JobManager(path)
    for command_id in ("queued-job", "running-job", "done-job"):
        jobs.create(build_command("blender_ping", command_id=command_id))
    jobs.set_status("running-job", "running")
    jobs.set_status("done-job", "completed")
    jobs.close()
    reopened = JobManager(path)
    reopened.reconcile_interrupted()
    for command_id in ("queued-job", "running-job"):
        job = reopened.get(command_id)
        assert job["status"] == "failed"
        assert job["error"]["type"] == "Interrupted"
        assert job["finished_at"] is not None
    assert reopened.get("done-job")["status"] == "completed"


def test_migration_preserves_rows_with_duplicate_keys(tmp_path):
    path = tmp_path / "jobs.db"
    jobs = JobManager(path)
    jobs.close()
    with sqlite3.connect(path) as conn:
        conn.execute("DROP INDEX IF EXISTS idx_jobs_idem_unique")
        conn.executemany(
            "INSERT INTO jobs (command_id, operation, status, created_at, idempotency_key) VALUES (?, 'ping', 'completed', ?, 'duplicate')",
            [("first", 1), ("second", 2)],
        )
    jobs = JobManager(path)
    assert len(jobs.list()) == 2
    assert jobs.find_by_idempotency("duplicate")["command_id"] == "first"
    job, created = jobs.reserve_idempotency(build_command("blender_ping", idempotency_key="duplicate"))
    assert not created
    assert job["command_id"] == "first"
