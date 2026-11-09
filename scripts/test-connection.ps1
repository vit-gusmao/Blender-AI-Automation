# Encoding policy: ASCII (Windows PowerShell 5.1 and PowerShell 7)
# Universal Blender MCP - deterministic end-to-end connection probe
[CmdletBinding()]
param(
    [string]$BridgeUrl = "http://127.0.0.1:8765",
    [ValidateRange(1, 300)][int]$TimeoutSeconds = 15,
    [string]$PythonExecutable
)

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
$Failures = 0

if ($PythonExecutable) {
    $PyExe = (Resolve-Path -LiteralPath $PythonExecutable).Path
}
else {
    $venvPython = Join-Path $Root ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
        $PyExe = $venvPython
    }
    else {
        $PyExe = (Get-Command python -ErrorAction Stop).Source
    }
}

$env:BLENDER_MCP_ROOT = $Root
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue

function New-McpProbeRoot {
    $uri = [Uri]$BridgeUrl
    if (-not $uri.IsAbsoluteUri -or $uri.Scheme -ne "http") {
        throw "BridgeUrl must be an absolute http URL."
    }
    if (($uri.AbsolutePath -and $uri.AbsolutePath -ne "/") -or $uri.Query -or $uri.Fragment) {
        throw "BridgeUrl must contain only scheme, host, and port."
    }

    $sourceToken = Join-Path $Root "storage\bridge_token"
    if (-not (Test-Path -LiteralPath $sourceToken -PathType Leaf)) {
        throw "Bridge token does not exist: $sourceToken"
    }

    $probeRoot = Join-Path ([IO.Path]::GetTempPath()) (
        "universal-mcp-probe-" + [Guid]::NewGuid().ToString("N")
    )
    $configDir = Join-Path $probeRoot "config"
    $storageDir = Join-Path $probeRoot "storage"
    New-Item -ItemType Directory -Path $configDir, $storageDir | Out-Null
    $probeToken = Join-Path $storageDir "bridge_token"
    Copy-Item -LiteralPath $sourceToken -Destination $probeToken

    foreach ($name in @("policies.yaml", "providers.yaml")) {
        $source = Join-Path (Join-Path $Root "config") $name
        if (Test-Path -LiteralPath $source -PathType Leaf) {
            Copy-Item -LiteralPath $source -Destination (Join-Path $configDir $name)
        }
    }

    $tokenForToml = $probeToken -replace "\\", "/"
    $lines = @(
        "[bridge]",
        "host = `"$($uri.Host)`"",
        "port = $($uri.Port)",
        "token_file = `"$tokenForToml`"",
        "command_timeout_s = $TimeoutSeconds",
        "heartbeat_timeout_s = $TimeoutSeconds",
        "",
        "[server]",
        "autostart_bridge = false"
    )
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllText(
        (Join-Path $configDir "app.toml"),
        (($lines -join [Environment]::NewLine) + [Environment]::NewLine),
        $utf8NoBom
    )
    return $probeRoot
}

function Invoke-PythonStep {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Code,
        [string[]]$ArgumentList = @()
    )

    Write-Host "[$Name]" -ForegroundColor Cyan
    $snippet = [System.IO.Path]::GetTempFileName()
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($snippet, $Code, $utf8NoBom)
    try {
        & $PyExe $snippet @ArgumentList
        $exitCode = $LASTEXITCODE
    }
    finally {
        Remove-Item -LiteralPath $snippet -Force -ErrorAction SilentlyContinue
    }
    if ($exitCode -ne 0) {
        $script:Failures += 1
        Write-Host "FAILED (exit code $exitCode)" -ForegroundColor Red
    }
    else {
        Write-Host "OK" -ForegroundColor Green
    }
}

$compileCode = @'
import runpy
import sys
runpy.run_path(sys.argv[1], run_name="__main__")
'@

$bridgeCode = @'
import json
import sys
import urllib.request

url = sys.argv[1].rstrip("/") + "/status"
with urllib.request.urlopen(url, timeout=float(sys.argv[2])) as response:
    payload = json.load(response)
if payload.get("ok") is not True:
    raise SystemExit("bridge status did not report ok=true")
print("uptime_s:", payload.get("uptime_s"), "queue:", payload.get("queue_depth"))
'@

$blenderCode = @'
import json
import pathlib
import sys
import urllib.request

token = pathlib.Path(sys.argv[2]).read_text(encoding="utf-8").strip()
request = urllib.request.Request(sys.argv[1].rstrip("/") + "/blender/info")
request.add_header("X-Bridge-Token", token)
with urllib.request.urlopen(request, timeout=float(sys.argv[3])) as response:
    payload = json.load(response)
if payload.get("connected") is not True:
    raise SystemExit("Blender addon is not registered")
blender = payload.get("blender") or {}
print("blender:", blender.get("blender_version"), "id:", blender.get("blender_id"))
'@

$mcpCode = @'
import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

EXPECTED_TOOLS = {
    "addon_operator_invoke", "animation_action", "animation_fcurve",
    "animation_keyframe", "animation_nla", "armature_create", "asset_export",
    "asset_import", "blender_get_capabilities", "blender_get_version",
    "blender_ping", "bone_edit", "camera_configure", "job_cancel",
    "job_get_result", "job_get_status", "light_configure", "light_create",
    "material_assign", "material_create", "material_set_property",
    "mesh_apply_modifier", "mesh_boolean", "mesh_edit", "mesh_validate",
    "nodes_connect", "nodes_create", "nodes_list", "nodes_set_input",
    "object_compare", "object_create", "object_delete", "object_duplicate",
    "object_get_info", "object_rename", "object_transform",
    "physics_configure", "project_checkpoint", "project_open", "project_save",
    "python_execute_blender", "python_execute_external", "render_configure",
    "render_final", "render_inspect", "render_preview", "rig_assign_weights",
    "scene_get_info", "scene_get_selection", "scene_list_objects",
    "scene_validate", "simulation_bake", "texture_assign", "texture_load",
    "uv_unwrap",
}

async def probe():
    child_env = dict(os.environ)
    child_env["BLENDER_MCP_ROOT"] = sys.argv[1]
    child_env.pop("PYTHONPATH", None)
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "universal_blender_mcp", "serve"],
        env=child_env,
    )
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            listed = await session.list_tools()
            actual = {tool.name for tool in listed.tools}
            if actual != EXPECTED_TOOLS:
                missing = sorted(EXPECTED_TOOLS - actual)
                extra = sorted(actual - EXPECTED_TOOLS)
                raise RuntimeError(f"catalog mismatch: missing={missing}, extra={extra}")
            called = await session.call_tool("blender_ping", {})
            if called.is_error or not called.content:
                raise RuntimeError("blender_ping returned an MCP error")
            envelope = json.loads(called.content[0].text)
            data = envelope.get("data") or {}
            if envelope.get("success") is not True or data.get("pong") is not True:
                raise RuntimeError(f"blender_ping failed: {envelope}")
            print(f"catalog: {len(actual)} exact tools")
            print(f"blender_ping: pong={data['pong']}")
            print(f"MCP bridge endpoint: {sys.argv[3]}")

timeout = float(sys.argv[2])
asyncio.run(asyncio.wait_for(probe(), timeout=timeout))
'@

$McpProbeRoot = New-McpProbeRoot
try {
    Invoke-PythonStep -Name "1. Project compilation" -Code $compileCode -ArgumentList @(
        (Join-Path $Root "scripts\check_compile.py")
    )
    Invoke-PythonStep -Name "2. Bridge status" -Code $bridgeCode -ArgumentList @(
        $BridgeUrl, [string]$TimeoutSeconds
    )
    Invoke-PythonStep -Name "3. Blender registration" -Code $blenderCode -ArgumentList @(
        $BridgeUrl, (Join-Path $Root "storage\bridge_token"), [string]$TimeoutSeconds
    )
    Invoke-PythonStep -Name "4. MCP initialize/tools-list/tools-call" -Code $mcpCode -ArgumentList @(
        $McpProbeRoot, [string]$TimeoutSeconds, $BridgeUrl
    )
}
finally {
    Remove-Item -LiteralPath $McpProbeRoot -Recurse -Force -ErrorAction SilentlyContinue
}

if ($Failures -eq 0) {
    Write-Host "All connection checks passed." -ForegroundColor Green
}
else {
    Write-Host "$Failures step(s) failed." -ForegroundColor Red
}
exit $Failures

