# SPDX-License-Identifier: MIT
"""Smoke test EXECUTADO DENTRO do Blender 3.6 (--background).

Executado por tests/blender_background/test_blender_smoke.py via:
  blender --background --factory-startup --python blender_smoke.py -- --report X

Valida de ponta a ponta, dentro do Blender real:
Bridge (HTTP, thread) → bridge_client (poll) → CommandQueue →
dispatcher.tick (simula o bpy.app.timers da thread principal) → envelope.

Escreve um relatório JSON e sai com código de erro.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
ARGS = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
STATE_ROOT = Path(next(
    (arg.split("=", 1)[1] for arg in ARGS
     if arg.startswith("--state-root=")),
    tempfile.mkdtemp(prefix="mcp_smoke_state_"),
)).resolve()
STATE_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["BLENDER_MCP_ROOT"] = str(STATE_ROOT)
sys.path.insert(0, str(REPO / "blender_addon"))
sys.path.insert(0, str(REPO / "mcp_server" / "src"))

import bpy  # noqa: E402

from universal_blender_mcp.bridge_server import BridgeServer  # noqa: E402
from universal_blender_mcp.job_manager import JobManager   # noqa: E402
from universal_blender_mcp.schemas import build_command  # noqa: E402

import universal_mcp.dispatcher as dispatcher  # noqa: E402
from universal_mcp import bridge_client  # noqa: E402

CHECKS = []


def check(name, fn):
    started = time.time()
    try:
        detail = fn()
        assert detail is not False, f"{name} retornou False sem falhar"
        CHECKS.append({"name": name, "ok": True,
                       "detail": detail if detail is not None else "",
                       "s": round(time.time() - started, 2)})
        print(f"[SMOKE] PASS {name} ({CHECKS[-1]['s']}s)")
        return True
    except Exception as exc:  # noqa: BLE001
        import traceback

        CHECKS.append({"name": name, "ok": False, "detail": str(exc),
                       "traceback": traceback.format_exc()[-1500:]})
        print(f"[SMOKE] FAIL {name}: {exc}")
        return False


class Harness:
    def __init__(self, state_root):
        import addon_utils

        self.state_root = Path(state_root).resolve()
        self.storage_root = self.state_root / "storage"
        self.storage_root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.storage_root / "jobs.db"
        assert not self.db_path.exists(), (
            f"smoke state reused an existing database: {self.db_path}")
        self.jobs = JobManager(self.db_path)
        assert self.jobs.list() == [], "temporary jobs database was not empty"
        self.initial_job_count = 0
        self.token = "smoke-token"
        self.server = BridgeServer("127.0.0.1", 0, self.token, self.jobs,
                                   queue_max=8, command_timeout_s=180)
        self.server.start(daemon=True)
        self.url = self.server.url
        addon_utils.enable("universal_mcp", default_set=True,
                           persistent=False)
        self.addon_enabled = True
        prefs = bpy.context.preferences.addons["universal_mcp"].preferences
        prefs.bridge_host = "127.0.0.1"
        prefs.bridge_port = self.server.port
        prefs.bridge_token = self.token
        prefs.poll_wait_s = 0.2
        prefs.storage_dir = str(self.storage_root)
        connect_result = bpy.ops.universal_mcp.connect()
        assert connect_result == {"FINISHED"}, connect_result
        self.client = bridge_client.get_client()
        assert self.client is not None
        deadline = time.monotonic() + 10
        while self.client.blender_id is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert self.client.blender_id is not None, self.client.last_error
        registration = self.server.state.blender
        assert registration is not None
        assert registration["blender_version"] == bpy.app.version_string
        assert registration["python_version"] == sys.version.split()[0]
        assert registration["capabilities"] == sorted(
            dispatcher.operation_names())
        self.registration = dict(registration)

    def _request(self, method, path, payload=None, deadline_at=None):
        if deadline_at is None:
            deadline_at = time.monotonic() + 20
        remaining = deadline_at - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"deadline elapsed before {method} {path}")
        data = json.dumps(payload, default=str).encode() if payload else None
        req = urllib.request.Request(self.url + path, data=data, method=method)
        req.add_header("X-Bridge-Token", self.token)
        if data:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=max(0.05, remaining)) as resp:
            return json.loads(resp.read().decode())

    def run_op(self, operation, args=None, timeout=60, expect_success=True):
        deadline = time.monotonic() + timeout
        command = build_command(operation, args or {})
        command["authorization"] = {"level": "advanced", "approved": True}
        out = self._request("POST", "/commands", command,
                            deadline_at=deadline)
        assert out.get("ok"), out
        while time.monotonic() < deadline:
            dispatcher.tick(self.client.queue)  # thread principal (§5.1)
            info = self._request(
                "GET", f"/commands/{command['command_id']}",
                deadline_at=deadline)
            status = (info.get("job") or {}).get("status")
            if status in ("completed", "failed", "cancelled"):
                result = info.get("result")
                assert result, f"sem resultado: {info}"
                if expect_success:
                    assert result.get("success"), f"{operation}: {result}"
                return result
            time.sleep(0.02)
        raise TimeoutError(f"{operation} não completou em {timeout}s")

    def stop(self):
        import addon_utils

        try:
            if bridge_client.get_client() is not None:
                result = bpy.ops.universal_mcp.disconnect()
                assert result == {"FINISHED"}, result
            assert not self.client.is_alive(), "network client did not join"
        finally:
            self.server.stop()
            self.jobs.close()
            if getattr(self, "addon_enabled", False):
                addon_utils.disable("universal_mcp", default_set=True)
                self.addon_enabled = False


def main(report_path: str) -> int:
    CHECKS.clear()
    harness = Harness(STATE_ROOT)
    artifacts = STATE_ROOT / "artifacts"

    def scene_fingerprint():
        return tuple(sorted(
            (obj.name, obj.type, tuple(obj.location), tuple(obj.scale),
             getattr(obj.data, "name", None))
            for obj in bpy.context.scene.objects))

    try:
        assert not artifacts.exists(), (
            f"smoke reused artifact state: {artifacts}")
        artifacts.mkdir(parents=True)

        def _registration():
            info = harness._request("GET", "/blender/info")
            assert info["connected"] is True
            assert info["blender"] == harness.registration
            assert info["blender"]["capabilities"] == sorted(
                dispatcher.operation_names())
            return {"blender_id": info["blender"]["blender_id"],
                    "capability_count": len(info["blender"]["capabilities"])}
        check("addon registration via /register", _registration)

        def _ping():
            data = harness.run_op("blender_ping")["data"]
            assert data["pong"] is True
            assert data["blender_version"] == "3.6.0"
            assert data["background"] is True
            assert data["scene_revision"] == 0
            assert data["object_count"] == 3
            return data
        check("blender_ping", _ping)

        def _version():
            data = harness.run_op("blender_get_version")["data"]
            assert data["blender_version"] == "3.6.0"
            assert data["is_blender_36"] is True
            assert data["python_version"].startswith("3.10.")
            assert data["background"] is True
            return data
        check("blender_get_version", _version)

        def _scene_info():
            before = scene_fingerprint()
            data = harness.run_op("scene_get_info")["data"]
            assert data["scene_name"] == "Scene"
            assert data["object_count"] == 3
            assert data["objects_by_type"] == {
                "MESH": 1, "LIGHT": 1, "CAMERA": 1}
            assert data["unit_system"] == "METRIC"
            assert scene_fingerprint() == before
            return data
        check("scene_get_info read-only", _scene_info)

        def _scene_list():
            before = scene_fingerprint()
            data = harness.run_op(
                "scene_list_objects", {"limit": 10})["data"]
            assert len(data["objects"]) == 3
            assert {obj["name"] for obj in data["objects"]} == {
                "Cube", "Camera", "Light"}
            assert scene_fingerprint() == before
            return data
        check("scene_list_objects read-only", _scene_list)

        def _create_cube():
            data = harness.run_op("object_create", {
                "type": "CUBE", "name": "MCP_Smoke_Cube",
                "location": [0, 0, 0]})["data"]
            assert data == {
                "object_name": "MCP_Smoke_Cube", "object_type": "MESH",
                "reused": False, "location": [0.0, 0.0, 0.0],
                "collection": None}
            assert len(bpy.context.scene.objects) == 4
            return data
        check("object_create cube exact", _create_cube)

        def _reuse_cube():
            before = scene_fingerprint()
            data = harness.run_op("object_create", {
                "type": "CUBE", "name": "MCP_Smoke_Cube"})["data"]
            assert data["reused"] is True
            assert data["object_name"] == "MCP_Smoke_Cube"
            assert data["object_type"] == "MESH"
            assert scene_fingerprint() == before
            return data
        check("object_create idempotent no scene mutation", _reuse_cube)

        def _transform():
            data = harness.run_op("object_transform", {
                "name": "MCP_Smoke_Cube", "location": [1, 2, 3],
                "scale": [2, 2, 2]})["data"]
            assert data["object_name"] == "MCP_Smoke_Cube"
            assert data["location"] == [1.0, 2.0, 3.0]
            assert data["scale"] == [2.0, 2.0, 2.0]
            return data
        check("object_transform exact", _transform)

        def _material_create():
            data = harness.run_op("material_create", {
                "name": "MCP_Metal", "metallic": 1.0,
                "roughness": 0.2})["data"]
            assert data == {"material": "MCP_Metal", "reused": False}
            return data
        check("material_create exact", _material_create)

        def _material_assign():
            data = harness.run_op("material_assign", {
                "object": "MCP_Smoke_Cube",
                "material": "MCP_Metal"})["data"]
            assert data == {"object": "MCP_Smoke_Cube",
                            "material": "MCP_Metal", "slot": 0}
            return data
        check("material_assign exact", _material_assign)

        def _mesh_validate():
            data = harness.run_op("mesh_validate", {
                "object": "MCP_Smoke_Cube"})["data"]
            assert data == {"object": "MCP_Smoke_Cube",
                            "had_errors": False, "vertices": 8,
                            "edges": 12, "faces": 6}
            return data
        check("mesh_validate exact", _mesh_validate)

        def _subdivide():
            data = harness.run_op("mesh_edit", {
                "object": "MCP_Smoke_Cube", "operations": [{
                    "op": "SUBDIVIDE_EDGES", "params": {"cuts": 1}}]
            })["data"]
            assert data["operations"] == [{
                "op": "SUBDIVIDE_EDGES", "subdivided": True}]
            assert data["mesh"] == {"vertices": 26, "faces": 24}
            return data
        check("mesh_edit SUBDIVIDE_EDGES exact", _subdivide)

        def _object_info():
            data = harness.run_op("object_get_info", {
                "name": "MCP_Smoke_Cube"})["data"]
            assert data["name"] == "MCP_Smoke_Cube"
            assert data["type"] == "MESH"
            assert data["transforms"]["location"] == [1.0, 2.0, 3.0]
            assert data["transforms"]["scale"] == [2.0, 2.0, 2.0]
            assert data["transforms"]["rotation_mode"] == "XYZ"
            assert data["mesh"] == {"vertices": 26, "edges": 48,
                                    "faces": 24, "uv_layers": 1}
            assert data["material_slots"] == [
                {"slot": 0, "material": "MCP_Metal"}]
            return data
        check("object_get_info exact", _object_info)

        def _python_ok():
            out = harness.run_op("python_execute_blender", {
                "code": "import bpy\nresult = {'objects': len(bpy.data.objects)}"})
            assert out["success"], out
            assert out["data"]["result"] == {"objects": 4}
            assert out["data"]["timeout_enforced"] is False
            return out["data"]
        check("python_execute_blender (permitido)", _python_ok)

        def _python_blocked():
            out = harness.run_op("python_execute_blender", {
                "code": "import subprocess\nresult = subprocess.listcmd"},
                expect_success=False)
            assert out["success"] is False
            assert out["error"]["type"] == "PolicyBlocked", out["error"]
            assert out["error"]["findings"] == ["subprocess"]
            return out["error"]
        check("python_execute_blender (padrão bloqueado)", _python_blocked)

        def _scene_validate():
            before = scene_fingerprint()
            data = harness.run_op("scene_validate", {"fix": False})["data"]
            assert data["valid"] is True
            assert data["issue_count"] == 0
            assert data["issues"] == []
            assert data["fix"] is False
            assert data["fixed"] == []
            assert scene_fingerprint() == before
            return data
        check("scene_validate read-only exact", _scene_validate)

        def _boolean_difference():
            created = harness.run_op("object_create", {
                "type": "CUBE", "name": "MCP_Boolean_Operand",
                "location": [1, 2, 3]})["data"]
            assert created["object_type"] == "MESH"
            data = harness.run_op("mesh_boolean", {
                "object": "MCP_Smoke_Cube",
                "operand": "MCP_Boolean_Operand",
                "operation": "DIFFERENCE"})["data"]
            assert data["object"] == "MCP_Smoke_Cube"
            assert data["operation"] == "DIFFERENCE"
            assert data["operand_deleted"] is True
            assert "MCP_Boolean_Operand" not in bpy.data.objects
            return data
        check("mesh_boolean DIFFERENCE enum exact", _boolean_difference)

        def _render():
            light = harness.run_op("light_create", {
                "type": "AREA", "name": "MCP_Light",
                "location": [3, -3, 4], "energy": 500})["data"]
            assert light["type"] == "AREA"
            harness.run_op("camera_configure", {})
            configured = harness.run_op("render_configure", {
                "engine": "CYCLES", "resolution_x": 120, "resolution_y": 90,
                "samples": 4, "filepath": str(artifacts / "smoke.png"),
                "file_format": "PNG"})["data"]
            assert configured["engine"] == "CYCLES"
            assert configured["resolution"] == [120, 90]
            assert bpy.context.scene.render.image_settings.file_format == "PNG"
            out = harness.run_op("render_preview", {"percentage": 100},
                                 timeout=120)
            assert out["success"], out
            path = artifacts / "smoke.png"
            assert path.is_file(), "render não gerou arquivo"
            assert path.stat().st_size > 8
            assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
            return {"path": str(path), "bytes": path.stat().st_size,
                    "engine": configured["engine"],
                    "resolution": configured["resolution"]}
        check("render_preview (Cycles 120x90)", _render)

        def _export():
            path = artifacts / "smoke.obj"
            out = harness.run_op("asset_export", {
                "path": str(path), "format": "OBJ",
                "objects": ["MCP_Smoke_Cube"]})
            assert out["success"], out
            assert out["data"] == {"exported": str(path), "format": "OBJ",
                                   "objects": ["MCP_Smoke_Cube"]}
            assert path.is_file() and path.stat().st_size > 0
            assert "MCP_Smoke_Cube" in path.read_text("utf-8")
            return out["data"]
        check("asset_export OBJ", _export)

        def _save():
            path = artifacts / "smoke_project.blend"
            out = harness.run_op("project_save", {"path": str(path)})
            assert out["success"], out
            assert out["data"] == {"saved": str(path), "packed": False}
            assert path.is_file() and path.stat().st_size > 0
            return out["data"]
        check("project_save", _save)

        def _checkpoint():
            directory = artifacts / "checkpoints"
            data = harness.run_op("project_checkpoint", {
                "label": "smoke", "directory": str(directory)})["data"]
            path = Path(data["checkpoint"])
            assert path.parent == directory
            assert path.suffix == ".blend"
            assert path.is_file() and path.stat().st_size > 0
            assert data["label"] == "smoke"
            assert data["source"] == str(artifacts / "smoke_project.blend")
            assert data["checkpoints_dir"] == str(directory)
            return data
        check("project_checkpoint exact", _checkpoint)

        def _jobs_persisted():
            jobs = harness.jobs.list(limit=100)
            assert harness.initial_job_count == 0
            assert harness.db_path.is_file()
            assert harness.db_path.parent == STATE_ROOT / "storage"
            assert len(jobs) == 24
            statuses = [job["status"] for job in jobs]
            assert statuses.count("completed") == 23
            assert statuses.count("failed") == 1
            failed = next(job for job in jobs if job["status"] == "failed")
            assert failed["operation"] == "python_execute_blender"
            assert failed["error"]["type"] == "PolicyBlocked"
            return {"database": str(harness.db_path), "job_count": len(jobs),
                    "completed": 23, "failed": 1,
                    "initial_job_count": harness.initial_job_count}
        check("jobs persisted in isolated SQLite", _jobs_persisted)
    finally:
        harness.stop()

    report = {
        "blender": bpy.app.version_string,
        "python": sys.version.split()[0],
        "background": bpy.app.background,
        "state_root": str(STATE_ROOT),
        "jobs_db": str(harness.db_path),
        "artifacts_root": str(artifacts),
        "checks": CHECKS,
        "passed": sum(1 for c in CHECKS if c["ok"]),
        "failed": sum(1 for c in CHECKS if not c["ok"]),
    }
    Path(report_path).write_text(json.dumps(report, indent=1), "utf-8")
    print(f"[SMOKE] {report['passed']} passaram, {report['failed']} falharam")
    print(f"[SMOKE] relatório: {report_path}")
    return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":
    report = next((a.split("=", 1)[1] for a in ARGS
                   if a.startswith("--report=")), str(HERE / "smoke_report.json"))
    try:
        code = main(report)
    except BaseException:  # Blender pode mascarar exceções com exit code zero
        import traceback
        traceback.print_exc()
        code = 1
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
    os._exit(code)  # garante código de saída no Blender --background
