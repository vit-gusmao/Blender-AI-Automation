"""Segurança: runtimes externos — autorização e isolamento (§8.1)."""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import external_runtime.process_runner as process_runner_module
from external_runtime.python_runner import PythonRunner
from external_runtime.process_runner import ProcessRunner
from external_runtime.runtime_registry import RuntimeRegistry
from external_runtime.workspace_manager import WorkspaceManager
from universal_blender_mcp.authorization import load_policy
from universal_blender_mcp.tool_registry import ServerContext, ToolRegistry
from universal_blender_mcp.tools import python_execution


UNSAFE_APPROVAL = "I_ACKNOWLEDGE_UNSANDBOXED_HOST_EXECUTION"


def _registry():
    return RuntimeRegistry({
        "python": {"executable": "python", "allowed": True},
        "denied": {"executable": "python", "allowed": False},
        "ghost": {"executable": "definitely-not-an-exe-xyz", "allowed": True},
    })


def test_denied_runtime_rejected(tmp_path):
    runner = ProcessRunner(_registry(), WorkspaceManager(tmp_path))
    out = runner.run("denied", ["-c", "print(1)"])
    assert out["success"] is False
    assert out["error"]["type"] == "PermissionError"


def test_unknown_runtime_rejected(tmp_path):
    runner = ProcessRunner(_registry(), WorkspaceManager(tmp_path))
    out = runner.run("powershell", ["-c", "echo x"])
    assert out["error"]["type"] == "KeyError"


def test_missing_executable_reported(tmp_path):
    runner = ProcessRunner(_registry(), WorkspaceManager(tmp_path))
    out = runner.run("ghost", ["--version"])
    assert out["success"] is False
    assert out["error"]["type"] == "RuntimeError"


def test_real_python_run_captured(tmp_path):
    runner = ProcessRunner(_registry(), WorkspaceManager(tmp_path))
    out = runner.run("python", ["-c", "print('hello-mcp')"])
    assert out["success"] is True
    assert out["returncode"] == 0
    assert "hello-mcp" in out["stdout"]


def test_cwd_confined_to_workspace(tmp_path):
    ws = WorkspaceManager(tmp_path / "ws")
    runner = ProcessRunner(_registry(), ws)
    task = ws.create_task_dir("t")
    out = runner.run("python", ["-c", "import os; print(os.getcwd())"],
                     cwd=task)
    assert out["success"] is True
    assert str(task) in out["stdout"].replace("\\\\", "\\")
    # fora do workspace → recusado
    out2 = runner.run("python", ["-c", "print(1)"], cwd=tmp_path)
    assert out2["success"] is False
    assert out2["error"]["type"] == "PermissionError"


def test_nonzero_exit_is_structured_not_exception(tmp_path):
    runner = ProcessRunner(_registry(), WorkspaceManager(tmp_path))
    out = runner.run("python", ["-c", "import sys; sys.exit(3)"])
    assert out["success"] is False
    assert out["returncode"] == 3
    assert "stderr" in out


@pytest.mark.parametrize("path_kind", ["absolute", "relative"])
def test_python_runner_rejects_script_outside_workspace(tmp_path, path_kind):
    ws = WorkspaceManager(tmp_path / "workspace")
    outside = tmp_path / "outside.py"
    marker = tmp_path / f"ran-{path_kind}.txt"
    outside.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n",
        encoding="utf-8",
    )
    runner = PythonRunner(ProcessRunner(_registry(), ws))
    requested = outside if path_kind == "absolute" else "../outside.py"

    out = runner.run_script(requested)

    assert out["success"] is False
    assert out["error"]["type"] == "PermissionError"
    assert not marker.exists()


def test_external_tool_rejects_script_path_outside_workspace(settings):
    outside = settings.external_workspace.parent / "outside.py"
    marker = settings.external_workspace.parent / "executed.txt"
    outside.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n",
        encoding="utf-8",
    )
    ctx = ServerContext(
        settings=settings, bridge=None, jobs=None,
        policy=SimpleNamespace(
            external_backend=None,
            allow_unsafe_host_execution=True,
            unsafe_approval_token=UNSAFE_APPROVAL,
        ),
        audit=None,
    )
    registry = ToolRegistry()
    python_execution.register(ctx, registry)

    result = asyncio.run(registry.get("python_execute_external").handler({
        "runtime": "python",
        "script_path": "../outside.py",
        "unsafe_approval_token": UNSAFE_APPROVAL,
    }))

    assert result["success"] is False
    assert result["error"]["type"] == "PermissionError"
    assert not marker.exists()


def test_runner_uses_runtime_resolved_path_after_alias_changes(tmp_path):
    registry = RuntimeRegistry({
        "stable": {"executable": sys.executable, "allowed": True},
    })
    resolved = registry.get("stable").resolved_path
    registry.get("stable").executable = str(tmp_path / "swapped-python.exe")
    runner = ProcessRunner(registry, WorkspaceManager(tmp_path / "ws"))

    out = runner.run("stable", ["-c", "print('resolved-runtime')"])

    assert out["success"] is True
    assert out["command"][0] == resolved
    assert "resolved-runtime" in out["stdout"]


def test_executable_allowlist_checks_canonical_resolved_path(tmp_path):
    registry = RuntimeRegistry({
        "python": {"executable": sys.executable, "allowed": True},
    })
    runner = ProcessRunner(
        registry,
        WorkspaceManager(tmp_path / "ws"),
        executables_allowlist=[tmp_path / "different-python.exe"],
    )

    out = runner.run("python", ["-c", "print('must-not-run')"])

    assert out["success"] is False
    assert out["error"]["type"] == "PermissionError"


def test_independent_runners_share_project_concurrency_limit(tmp_path):
    ws1 = WorkspaceManager(tmp_path / "ws")
    ws2 = WorkspaceManager(tmp_path / "ws")
    runner1 = ProcessRunner(_registry(), ws1, max_processes=1)
    runner2 = ProcessRunner(_registry(), ws2, max_processes=1)
    ready = ws1.root / "first-ready.txt"
    first_result = {}

    def run_first():
        first_result.update(runner1.run(
            "python",
            ["-c", ("from pathlib import Path; import time; "
                    f"Path({str(ready)!r}).write_text('ready'); "
                    "time.sleep(0.8)")],
        ))

    thread = threading.Thread(target=run_first)
    thread.start()
    deadline = time.monotonic() + 5
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ready.exists(), first_result

    started = time.monotonic()
    second = runner2.run("python", ["-c", "print('second')"])
    elapsed = time.monotonic() - started
    thread.join(timeout=5)

    assert first_result["success"] is True
    assert second["success"] is True
    assert elapsed >= 0.5


def test_streaming_output_limit_counts_encoded_bytes(tmp_path):
    runner = ProcessRunner(
        _registry(), WorkspaceManager(tmp_path / "ws"), max_output_bytes=101)

    out = runner.run("python", ["-c", "print('á' * 10000, end='')"])

    assert out["success"] is True
    assert len(out["stdout"].encode("utf-8")) <= 101
    assert out["stdout"]


def test_default_environment_does_not_inherit_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("RPA_PHASE3_API_TOKEN", "must-not-leak")
    runner = ProcessRunner(_registry(), WorkspaceManager(tmp_path / "ws"))

    out = runner.run(
        "python",
        ["-c", ("import os; "
                "print(os.environ.get('RPA_PHASE3_API_TOKEN', 'absent'))")],
    )

    assert out["success"] is True
    assert out["stdout"].strip() == "absent"


def test_per_call_timeouts_do_not_race(tmp_path):
    runner = ProcessRunner(
        _registry(), WorkspaceManager(tmp_path / "ws"), max_processes=2,
        timeout_s=5)
    results = {}

    def short_timeout():
        results["short"] = runner.run(
            "python", ["-c", "import time; time.sleep(0.6)"],
            timeout_s=0.15)

    def long_timeout():
        results["long"] = runner.run(
            "python", ["-c", "import time; time.sleep(0.35)"],
            timeout_s=2)

    threads = [threading.Thread(target=short_timeout),
               threading.Thread(target=long_timeout)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert results["short"]["error"]["type"] == "Timeout"
    assert results["long"]["success"] is True


def _pid_exists(pid):
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, check=False,
        )
        return str(pid) in result.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _force_kill_tree(pid):
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, check=False)
    else:
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass


def test_timeout_terminates_child_and_grandchild(tmp_path):
    ws = WorkspaceManager(tmp_path / "ws")
    grandchild_pid = ws.root / "grandchild.pid"
    child_script = ws.root / "child.py"
    parent_script = ws.root / "parent.py"
    child_script.write_text(
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        "p = subprocess.Popen([sys.executable, '-c', "
        "'import time; time.sleep(30)'], stdin=subprocess.DEVNULL, "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        f"Path({str(grandchild_pid)!r}).write_text(str(p.pid))\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    parent_script.write_text(
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, {str(child_script)!r}], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
        "stderr=subprocess.DEVNULL)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    runner = ProcessRunner(_registry(), ws, timeout_s=0.8)

    out = runner.run("python", [str(parent_script)])
    deadline = time.monotonic() + 2
    while not grandchild_pid.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert grandchild_pid.exists()
    pid = int(grandchild_pid.read_text())
    try:
        assert out["error"]["type"] == "Timeout"
        deadline = time.monotonic() + 3
        while _pid_exists(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _pid_exists(pid)
    finally:
        if _pid_exists(pid):
            _force_kill_tree(pid)


def test_direct_runner_leases_cwd_during_execution(tmp_path):
    ws = WorkspaceManager(tmp_path / "ws")
    task_dir = ws.create_task_dir("direct")
    ready = task_dir / "ready.txt"
    runner = ProcessRunner(_registry(), ws)
    result = {}

    def execute():
        result.update(runner.run(
            "python",
            ["-c", ("from pathlib import Path; import time; "
                    f"Path({str(ready)!r}).write_text('ready'); "
                    "time.sleep(0.6)")],
            cwd=task_dir,
        ))

    thread = threading.Thread(target=execute)
    thread.start()
    deadline = time.monotonic() + 3
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ready.exists()

    assert ws.cleanup(keep_last=0) == 0
    assert task_dir.exists()
    thread.join(timeout=3)
    assert result["success"] is True
    assert ws.cleanup(keep_last=0) == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object race")
def test_timeout_kills_child_spawned_before_job_attachment(
        tmp_path, monkeypatch):
    ws = WorkspaceManager(tmp_path / "ws")
    child_pid_file = ws.root / "early-child.pid"
    parent_script = ws.root / "early-parent.py"
    parent_script.write_text(
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        "p = subprocess.Popen([sys.executable, '-c', "
        "'import time; time.sleep(30)'], stdin=subprocess.DEVNULL, "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        f"Path({str(child_pid_file)!r}).write_text(str(p.pid))\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    original_attach = process_runner_module.attach_process_tree

    def delayed_attach(process):
        time.sleep(0.3)
        original_attach(process)

    monkeypatch.setattr(
        process_runner_module, "attach_process_tree", delayed_attach)
    runner = ProcessRunner(_registry(), ws, timeout_s=0.5)

    out = runner.run("python", [str(parent_script)])
    assert child_pid_file.exists()
    pid = int(child_pid_file.read_text())
    try:
        assert out["error"]["type"] == "Timeout"
        deadline = time.monotonic() + 3
        while _pid_exists(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _pid_exists(pid)
    finally:
        if _pid_exists(pid):
            _force_kill_tree(pid)


def _external_tool(settings, policy, backends=None):
    ctx = ServerContext(settings=settings, bridge=None, jobs=None,
                        policy=policy, audit=None)
    if backends is not None:
        ctx.external_execution_backends = backends
    registry = ToolRegistry()
    python_execution.register(ctx, registry)
    return registry.get("python_execute_external")


def test_external_execution_defaults_to_sandbox_unavailable(settings):
    policy = load_policy(settings)
    tool = _external_tool(settings, policy)

    result = asyncio.run(tool.handler({
        "runtime": "python", "code": "print('must-not-run')",
    }))

    assert policy.external_backend is None
    assert policy.allow_unsafe_host_execution is False
    assert result["success"] is False
    assert result["error"]["type"] == "SandboxUnavailable"


def test_configured_sandbox_backend_is_invoked(settings):
    settings.policies_cfg["external_execution"]["backend"] = "test-sandbox"
    policy = load_policy(settings)
    calls = []

    async def backend(request):
        calls.append(request)
        return {"success": True, "stdout": "sandbox-ok", "stderr": ""}

    tool = _external_tool(
        settings, policy, backends={"test-sandbox": backend})
    result = asyncio.run(tool.handler({
        "runtime": "python", "code": "print('through-backend')",
    }))

    assert result["success"] is True
    assert result["data"]["execution_mode"] == "sandbox:test-sandbox"
    assert len(calls) == 1
    assert calls[0]["runtime_name"] == "python"
    assert calls[0]["runtime"] == str(Path(shutil.which("python")).resolve())
    assert Path(calls[0]["script_path"]).is_relative_to(
        settings.external_workspace)


@pytest.mark.parametrize(("runtime", "config", "error_type"), [
    ("denied", {"executable": "python", "allowed": False},
     "PermissionError"),
    ("ghost", {"executable": "definitely-not-an-exe-phase3",
               "allowed": True}, "RuntimeError"),
])
def test_sandbox_backend_rejects_unusable_runtime(
        settings, runtime, config, error_type):
    settings.external_runtimes[runtime] = config
    settings.policies_cfg["external_execution"]["backend"] = "test-sandbox"
    policy = load_policy(settings)
    calls = []

    async def backend(request):
        calls.append(request)
        return {"success": True}

    tool = _external_tool(
        settings, policy, backends={"test-sandbox": backend})
    script = settings.external_workspace / f"{runtime}.py"
    script.write_text("print('must-not-run')", encoding="utf-8")
    result = asyncio.run(tool.handler({
        "runtime": runtime, "script_path": str(script),
    }))

    assert result["success"] is False
    assert result["error"]["type"] == error_type
    assert calls == []


def test_sandbox_backend_honors_resolved_executable_allowlist(
        settings, tmp_path):
    settings.policies_cfg["external_execution"]["backend"] = "test-sandbox"
    settings.policies_cfg["external_execution"]["executables_allowlist"] = [
        str(tmp_path / "different-python.exe")]
    policy = load_policy(settings)
    calls = []

    async def backend(request):
        calls.append(request)
        return {"success": True}

    tool = _external_tool(
        settings, policy, backends={"test-sandbox": backend})
    result = asyncio.run(tool.handler({
        "runtime": "python", "code": "print('must-not-run')",
    }))

    assert result["success"] is False
    assert result["error"]["type"] == "PermissionError"
    assert calls == []


def test_unsafe_host_requires_policy_opt_in(settings):
    policy = load_policy(settings)
    tool = _external_tool(settings, policy)

    result = asyncio.run(tool.handler({
        "runtime": "python", "code": "print('blocked')",
        "unsafe_approval_token": UNSAFE_APPROVAL,
    }))

    assert result["success"] is False
    assert result["error"]["type"] == "SandboxUnavailable"


def test_unsafe_host_requires_approved_request(settings):
    settings.policies_cfg["external_execution"][
        "allow_unsafe_host_execution"] = True
    policy = load_policy(settings)
    tool = _external_tool(settings, policy)

    result = asyncio.run(tool.handler({
        "runtime": "python", "code": "print('blocked')",
    }))

    assert result["success"] is False
    assert result["error"]["type"] == "UnsafeApprovalRequired"


def test_unsafe_host_runs_only_with_opt_in_and_approval(settings):
    settings.policies_cfg["external_execution"][
        "allow_unsafe_host_execution"] = True
    policy = load_policy(settings)
    tool = _external_tool(settings, policy)

    result = asyncio.run(tool.handler({
        "runtime": "python", "code": "print('unsafe-approved')",
        "unsafe_approval_token": UNSAFE_APPROVAL,
    }))

    assert result["success"] is True
    assert result["data"]["execution_mode"] == "unsafe_host"
    assert result["data"]["stdout"].strip() == "unsafe-approved"


def test_large_stdin_obeys_absolute_timeout_and_cleans_writer(tmp_path):
    runner = ProcessRunner(
        _registry(), WorkspaceManager(tmp_path / "ws"), timeout_s=5)

    started = time.monotonic()
    result = runner.run(
        "python", ["-c", "import time; time.sleep(2)"],
        stdin_text="x" * 10_000_000, timeout_s=0.2,
    )
    elapsed = time.monotonic() - started

    assert result["success"] is False
    assert result["error"]["type"] == "Timeout"
    assert elapsed < 1.5
    assert runner.active == []
    assert not any(
        thread.name.startswith("external-runtime-stdin-")
        for thread in threading.enumerate())
