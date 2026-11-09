"""Behavior and compatibility checks for the operational PowerShell scripts."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
CLIENTS = REPO / "config" / "clients"
POWERSHELL = shutil.which("powershell")
PWSH = shutil.which("pwsh")


def _run_ps(script: Path, *args: str, cwd: Path | None = None,
            timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    if not POWERSHELL:
        pytest.skip("Windows PowerShell is not available")
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
         "Bypass", "-File", str(script), *map(str, args)],
        cwd=cwd or script.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _copy_installer_fixture(tmp_path: Path) -> Path:
    root = tmp_path / "repo with apostrophe's space"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPTS / "install.ps1", root / "scripts" / "install.ps1")
    shutil.copy2(SCRIPTS / "check_compile.py",
                 root / "scripts" / "check_compile.py")
    shutil.copytree(REPO / "mcp_server", root / "mcp_server")
    shutil.copytree(REPO / "config", root / "config")
    shutil.copytree(REPO / "prompts", root / "prompts")
    return root


def _client_command(data: dict) -> str:
    if "command" in data:
        return data["command"]
    return data["mcpServers"]["blender-universal"]["command"]


def test_installer_uses_argument_safe_fail_fast_native_invocation():
    """A string-built command or warning-only pip failure must be rejected."""
    source = (SCRIPTS / "install.ps1").read_text(encoding="utf-8-sig")
    assert "function Invoke-Native" in source
    assert "[scriptblock]::Create" not in source
    assert "pip reportou erro" not in source
    assert "install -e do mcp_server falhou" not in source
    assert '\"-m\", \"universal_blender_mcp\", \"inspect\"' in source


def test_start_server_uses_installed_package_unless_fallback_is_explicit():
    source = (SCRIPTS / "start-server.ps1").read_text(encoding="utf-8-sig")
    assert "DevelopmentFallback" in source
    assert "PYTHONPATH" in source
    assert "Invoke-Native" in source


def test_client_configs_reference_an_existing_python_interpreter():
    for path in sorted(CLIENTS.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        command = Path(_client_command(data))
        assert command.is_file(), f"{path.name}: missing interpreter {command}"
        assert data.get("args", ["-m", "universal_blender_mcp"])[-1] == (
            "universal_blender_mcp"
        )


def test_installer_fails_on_native_pip_error(tmp_path: Path):
    root = _copy_installer_fixture(tmp_path)
    fake_python = root / "python-fails.cmd"
    fake_python.write_text(
        "@echo off\n"
        "if \"%1\"==\"-c\" (echo 3.15.0& exit /b 0)\n"
        "if \"%1\"==\"-m\" if \"%2\"==\"pip\" exit /b 23\n"
        "exit /b 0\n",
        encoding="ascii",
    )
    result = _run_ps(
        root / "scripts" / "install.ps1",
        "-SkipVenv", "-SkipTests", "-PythonExecutable", str(fake_python),
        timeout=30,
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "exit code 23" in output
    assert "Instalação concluída" not in output


def test_skipvenv_installs_importable_package_and_updates_clients(tmp_path: Path):
    root = _copy_installer_fixture(tmp_path)
    venv = tmp_path / "selected python"
    subprocess.run(
        [sys.executable, "-m", "venv", "--system-site-packages", str(venv)],
        check=True,
    )
    python = venv / "Scripts" / "python.exe"
    result = _run_ps(
        root / "scripts" / "install.ps1",
        "-SkipVenv", "-SkipTests", "-PythonExecutable", str(python),
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["BLENDER_MCP_ROOT"] = str(root)
    inspected = subprocess.run(
        [python, "-m", "universal_blender_mcp", "inspect"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert inspected.returncode == 0, inspected.stdout + inspected.stderr
    assert json.loads(inspected.stdout)["count"] >= 55

    for path in sorted((root / "config" / "clients").glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        assert Path(_client_command(data)).resolve() == python.resolve()


def test_installer_rejects_invalid_python_found_only_in_isolated_copy(
        tmp_path: Path):
    root = _copy_installer_fixture(tmp_path)
    broken = root / "broken_only_in_copy.py"
    broken.write_text("def broken(:\n", encoding="ascii")
    venv = tmp_path / "isolated validation python"
    subprocess.run(
        [sys.executable, "-m", "venv", "--system-site-packages", str(venv)],
        check=True,
    )
    python = venv / "Scripts" / "python.exe"
    result = _run_ps(
        root / "scripts" / "install.ps1",
        "-SkipVenv", "-SkipTests", "-PythonExecutable", str(python),
        timeout=120,
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "broken_only_in_copy.py" in output


def test_connection_script_uses_real_mcp_probe_and_exact_catalog():
    """The probe must initialize MCP, compare all tools, and call ping."""
    source = (SCRIPTS / "test-connection.ps1").read_text(
        encoding="utf-8-sig"
    )
    assert "sys_exit" not in source
    assert ".Replace(" not in source
    assert "import sys" in source
    assert "EXPECTED_TOOLS" in source
    assert "session.initialize" in source
    assert "session.list_tools" in source
    assert 'session.call_tool("blender_ping"' in source
    assert "asyncio.wait_for" in source


@pytest.fixture(scope="session")
def installed_test_python(tmp_path_factory: pytest.TempPathFactory) -> Path:
    venv = tmp_path_factory.mktemp("installed-mcp") / "venv"
    subprocess.run(
        [sys.executable, "-m", "venv", "--system-site-packages", str(venv)],
        check=True,
    )
    python = venv / "Scripts" / "python.exe"
    installed = subprocess.run(
        [python, "-m", "pip", "install", "-e",
         str(REPO / "mcp_server")],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr
    return python


def _prepare_connection_root(tmp_root: Path, bridge_env: dict) -> Path:
    scripts = tmp_root / "scripts"
    scripts.mkdir(exist_ok=True)
    shutil.copy2(SCRIPTS / "test-connection.ps1",
                 scripts / "test-connection.ps1")
    shutil.copy2(SCRIPTS / "check_compile.py", scripts / "check_compile.py")
    config = tmp_root / "config" / "app.toml"
    text = config.read_text(encoding="utf-8")
    assert bridge_env["server"].port != 9
    text = text.replace("port = 8765", "port = 9", 1)
    config.write_text(text, encoding="utf-8")
    token = tmp_root / "storage" / "bridge_token"
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text(bridge_env["token"], encoding="utf-8")
    return scripts / "test-connection.ps1"


def test_connection_script_calls_live_fake_blender(
        tmp_root: Path, bridge_env: dict, installed_test_python: Path):
    script = _prepare_connection_root(tmp_root, bridge_env)
    result = _run_ps(
        script,
        "-BridgeUrl", bridge_env["url"],
        "-PythonExecutable", str(installed_test_python),
        "-TimeoutSeconds", "15",
        timeout=45,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "MCP initialize/tools-list/tools-call" in output
    assert "blender_ping: pong=True" in output
    assert f"MCP bridge endpoint: {bridge_env['url']}" in output
    assert bridge_env["fake"].executed.count("blender_ping") == 1


def test_connection_script_returns_nonzero_when_bridge_probe_fails(
        tmp_root: Path, bridge_env: dict, installed_test_python: Path):
    script = _prepare_connection_root(tmp_root, bridge_env)
    result = _run_ps(
        script,
        "-BridgeUrl", "http://127.0.0.1:9",
        "-PythonExecutable", str(installed_test_python),
        "-TimeoutSeconds", "2",
        timeout=30,
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "step(s) failed" in output


def test_connection_script_returns_nonzero_when_compile_probe_fails(
        tmp_root: Path, bridge_env: dict, installed_test_python: Path):
    script = _prepare_connection_root(tmp_root, bridge_env)
    broken = tmp_root / "broken_only_in_copy.py"
    broken.write_text("def broken(:\n", encoding="ascii")
    result = _run_ps(
        script,
        "-BridgeUrl", bridge_env["url"],
        "-PythonExecutable", str(installed_test_python),
        "-TimeoutSeconds", "15",
        timeout=45,
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "broken_only_in_copy.py" in output


def test_connection_script_returns_nonzero_for_catalog_mismatch(
        tmp_root: Path, bridge_env: dict, installed_test_python: Path):
    script = _prepare_connection_root(tmp_root, bridge_env)
    source = script.read_text(encoding="utf-8-sig")
    source = source.replace(
        '"uv_unwrap",\n}',
        '"uv_unwrap", "not_a_real_tool",\n}',
        1,
    )
    script.write_text(source, encoding="ascii")
    result = _run_ps(
        script,
        "-BridgeUrl", bridge_env["url"],
        "-PythonExecutable", str(installed_test_python),
        "-TimeoutSeconds", "15",
        timeout=45,
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "catalog mismatch" in output


def test_connection_script_returns_nonzero_for_false_ping(
        tmp_root: Path, bridge_env: dict, installed_test_python: Path):
    bridge_env["fake"].handlers["blender_ping"] = lambda _args: {"pong": False}
    script = _prepare_connection_root(tmp_root, bridge_env)
    result = _run_ps(
        script,
        "-BridgeUrl", bridge_env["url"],
        "-PythonExecutable", str(installed_test_python),
        "-TimeoutSeconds", "15",
        timeout=45,
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "blender_ping failed" in output


def test_package_failure_restores_callers_working_directory(tmp_path: Path):
    root = tmp_path / "package repo"
    scripts = root / "scripts"
    addon = root / "blender_addon" / "universal_mcp"
    scripts.mkdir(parents=True)
    addon.mkdir(parents=True)
    shutil.copy2(SCRIPTS / "package-addon.ps1",
                 scripts / "package-addon.ps1")
    (addon / "__init__.py").write_text(
        'bl_info = {"version": (0, 1, 0)}\n', encoding="utf-8"
    )
    wrapper = tmp_path / "invoke-package-failure.ps1"
    wrapper.write_text(
        "param([string]$PackageScript, [string]$Expected)\n"
        "$ErrorActionPreference = 'Stop'\n"
        "function Compress-Archive { throw 'simulated archive failure' }\n"
        "Set-Location -LiteralPath $Expected\n"
        "$before = (Get-Location).Path\n"
        "try { & $PackageScript } catch { Write-Host $_.Exception.Message }\n"
        "$after = (Get-Location).Path\n"
        "if ($after -ne $before) { Write-Error \"cwd changed: $before -> $after\"; exit 9 }\n"
        "Write-Host 'cwd-restored'\n",
        encoding="utf-8-sig",
    )
    result = _run_ps(wrapper, "-PackageScript", str(scripts / "package-addon.ps1"),
                     "-Expected", str(tmp_path), timeout=30)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "simulated archive failure" in output
    assert "cwd-restored" in output


def test_package_script_reports_token_and_addon_setup():
    source = (SCRIPTS / "package-addon.ps1").read_text(encoding="utf-8-sig")
    assert "finally" in source
    assert "bridge_token" in source
    assert "Universal MCP" in source


def test_diagnostics_use_current_sdk_and_shared_client():
    sources = {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(SCRIPTS.glob("diag_*.py"))
    }
    assert "CallToolRequestParams" in sources["diag_mcp_calltool.py"]
    assert "CallToolRequestParam" not in sources["diag_mcp_calltool.py"].replace(
        "CallToolRequestParams", ""
    )
    assert "mcp.server._methods" not in sources["diag_methods.py"]
    assert "import mcp_types" not in sources["diag_mcp_types.py"]
    for name in ("diag_mcp_raw.py", "diag_mcp_stderr.py"):
        assert "mcp_diag_client" in sources[name]


def test_diagnostic_client_timeout_terminates_silent_process():
    sys.path.insert(0, str(SCRIPTS))
    try:
        from mcp_diag_client import MCPDiagnosticClient, MCPDiagnosticTimeout
    finally:
        sys.path.remove(str(SCRIPTS))

    client = MCPDiagnosticClient(
        command=sys.executable,
        args=["-c", "import time; time.sleep(60)"],
        timeout=0.25,
    )
    client.start()
    process = client.process
    assert process is not None
    with pytest.raises(MCPDiagnosticTimeout):
        client.request("tools/list")
    client.close()
    assert process.poll() is not None


@pytest.mark.parametrize("name", [
    "diag_mcp_calltool.py",
    "diag_methods.py",
    "diag_mcp_types.py",
    "diag_mcp_raw.py",
    "diag_mcp_stderr.py",
])
def test_each_diagnostic_finishes_within_deadline(
        name: str, installed_test_python: Path, tmp_root: Path):
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["BLENDER_MCP_ROOT"] = str(tmp_root)
    result = subprocess.run(
        [installed_test_python, SCRIPTS / name, "--timeout", "5"],
        cwd=tmp_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_all_powershell_sources_are_ascii_and_parse_with_default_decoding():
    """Both documented hosts must parse files using their default decoder."""
    paths = sorted(SCRIPTS.glob("*.ps1"))
    assert paths
    for path in paths:
        raw = path.read_bytes()
        assert raw.isascii(), f"{path.name} is not ASCII-compatible"
        first_line = raw.splitlines()[0].decode("ascii")
        assert "Encoding policy: ASCII" in first_line

    hosts = [host for host in (POWERSHELL, PWSH) if host]
    assert hosts, "Neither Windows PowerShell nor PowerShell 7 is available"
    parser = (
        "$errors=$null; $tokens=$null; "
        "[System.Management.Automation.Language.Parser]::ParseFile("
        "$env:PS_PARSE_TARGET, [ref]$tokens, [ref]$errors) | Out-Null; "
        "if ($errors.Count) { $errors | ForEach-Object { Write-Error $_ }; exit 1 }"
    )
    for host in hosts:
        for path in paths:
            env = os.environ.copy()
            env["PS_PARSE_TARGET"] = str(path)
            result = subprocess.run(
                [host, "-NoProfile", "-NonInteractive", "-Command", parser],
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            assert result.returncode == 0, (
                f"{Path(host).name} failed to parse {path.name}: "
                f"{result.stdout}{result.stderr}"
            )

