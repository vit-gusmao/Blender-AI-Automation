# Encoding policy: ASCII (Windows PowerShell 5.1 and PowerShell 7)
# Universal Blender MCP - service launcher
[CmdletBinding()]
param(
    [ValidateSet("stdio", "http", "streamable-http")]
    [string]$Transport = "stdio",
    [switch]$BridgeOnly,
    [switch]$Gateway,
    [switch]$Inspect,
    [switch]$DevelopmentFallback,
    [string]$PythonExecutable
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot

function Invoke-Native {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$ArgumentList = @()
    )
    & $FilePath @ArgumentList
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "Native command '$FilePath' failed with exit code $exitCode."
    }
}

if ($PythonExecutable) {
    $PyExe = (Resolve-Path -LiteralPath $PythonExecutable).Path
}
else {
    $venvPython = Join-Path $Root ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
        $PyExe = $venvPython
    }
    else {
        $pythonCommand = Get-Command python -ErrorAction Stop
        $PyExe = $pythonCommand.Source
    }
}

$env:BLENDER_MCP_ROOT = $Root
if ($DevelopmentFallback) {
    $sourcePath = Join-Path $Root "mcp_server\src"
    if ($env:PYTHONPATH) {
        $env:PYTHONPATH = $sourcePath + [IO.Path]::PathSeparator + $env:PYTHONPATH
    }
    else {
        $env:PYTHONPATH = $sourcePath
    }
}
else {
    Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    Invoke-Native -FilePath $PyExe -ArgumentList @(
        "-c", "import universal_blender_mcp; print(universal_blender_mcp.__file__)"
    )
}

Push-Location $Root
try {
    if ($Gateway) {
        Invoke-Native -FilePath $PyExe -ArgumentList @("-m", "agent_gateway")
    }
    elseif ($Inspect) {
        Invoke-Native -FilePath "npx" -ArgumentList @(
            "@modelcontextprotocol/inspector", $PyExe, "-m", "universal_blender_mcp"
        )
    }
    elseif ($BridgeOnly) {
        Invoke-Native -FilePath $PyExe -ArgumentList @(
            "-m", "universal_blender_mcp", "bridge"
        )
    }
    else {
        Invoke-Native -FilePath $PyExe -ArgumentList @(
            "-m", "universal_blender_mcp", "serve", "--transport", $Transport
        )
    }
}
finally {
    Pop-Location
}

