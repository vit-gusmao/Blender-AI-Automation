# Encoding policy: ASCII (Windows PowerShell 5.1 and PowerShell 7)
# Universal Blender MCP - instalacao do ambiente externo
# Uso: powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
[CmdletBinding()]
param(
    [switch]$SkipVenv,
    [switch]$SkipTests,
    [string]$PythonExecutable
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot

function Invoke-Native {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$ArgumentList = @(),
        [switch]$CaptureOutput
    )

    if ($CaptureOutput) {
        $output = & $FilePath @ArgumentList
        $exitCode = $LASTEXITCODE
        if ($exitCode -ne 0) {
            throw "Native command '$FilePath' failed with exit code $exitCode."
        }
        return $output
    }

    & $FilePath @ArgumentList
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "Native command '$FilePath' failed with exit code $exitCode."
    }
}

function Test-PythonCandidate {
    param([string]$FilePath, [string[]]$PrefixArguments = @())

    try {
        $arguments = @($PrefixArguments) + @(
            "-c", "import sys; print(sys.version.split()[0]); raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
        )
        $version = Invoke-Native -FilePath $FilePath -ArgumentList $arguments -CaptureOutput
        Write-Host "Python candidate: $FilePath $($PrefixArguments -join ' ') -> $version"
        return $true
    }
    catch {
        return $false
    }
}

function Resolve-BasePython {
    if ($PythonExecutable) {
        $resolved = (Resolve-Path -LiteralPath $PythonExecutable).Path
        if (-not (Test-PythonCandidate -FilePath $resolved)) {
            throw "PythonExecutable must be Python >= 3.10: $resolved"
        }
        return @{ FilePath = $resolved; Arguments = @() }
    }

    $candidates = @(
        @{ FilePath = "py"; Arguments = @("-3.12") },
        @{ FilePath = "py"; Arguments = @("-3.11") },
        @{ FilePath = "python"; Arguments = @() }
    )
    foreach ($candidate in $candidates) {
        if (-not (Get-Command $candidate.FilePath -ErrorAction SilentlyContinue)) {
            continue
        }
        if (Test-PythonCandidate -FilePath $candidate.FilePath -PrefixArguments $candidate.Arguments) {
            return $candidate
        }
    }
    throw "Python >= 3.10 was not found. Install a supported external Python."
}

function Write-ClientConfigs {
    param([string]$Interpreter)

    if (-not (Test-Path -LiteralPath $Interpreter -PathType Leaf)) {
        throw "Client interpreter does not exist: $Interpreter"
    }
    $clientDir = Join-Path $Root "config\clients"
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    foreach ($path in Get-ChildItem -LiteralPath $clientDir -Filter "*.json" -File) {
        $data = Get-Content -LiteralPath $path.FullName -Raw | ConvertFrom-Json
        if ($data.PSObject.Properties.Name -contains "command") {
            $data.command = $Interpreter
        }
        elseif ($data.PSObject.Properties.Name -contains "mcpServers") {
            $data.mcpServers."blender-universal".command = $Interpreter
        }

        if ($data.PSObject.Properties.Name -contains "register_command") {
            $prefix = $path.BaseName
            if ($prefix -eq "codex") {
                $data.register_command = "codex mcp add blender-universal -- `"$Interpreter`" -m universal_blender_mcp"
            }
            elseif ($prefix -eq "claude") {
                $data.register_command = "claude mcp add --transport stdio blender-universal -- `"$Interpreter`" -m universal_blender_mcp"
            }
            elseif ($prefix -eq "opencode") {
                $data.register_command = "opencode mcp add blender-universal -- `"$Interpreter`" -m universal_blender_mcp"
            }
        }
        $json = $data | ConvertTo-Json -Depth 10
        [System.IO.File]::WriteAllText($path.FullName, $json + [Environment]::NewLine, $utf8NoBom)
    }
}

Write-Host "== Universal Blender MCP - install at $Root ==" -ForegroundColor Cyan
$base = Resolve-BasePython
$PyExe = Join-Path $Root ".venv\Scripts\python.exe"

if ($SkipVenv) {
    if ($base.Arguments.Count -ne 0) {
        throw "-SkipVenv requires a directly executable Python. Use -PythonExecutable with its full path."
    }
    $PyExe = $base.FilePath
}
elseif (-not (Test-Path -LiteralPath $PyExe -PathType Leaf)) {
    Write-Host "Creating isolated venv at $Root\.venv"
    Invoke-Native -FilePath $base.FilePath -ArgumentList (@($base.Arguments) + @("-m", "venv", (Join-Path $Root ".venv")))
}

$PyExe = (Resolve-Path -LiteralPath $PyExe).Path
Write-Host "External Python: $PyExe"

Write-Host "== Installing editable server package and dependencies ==" -ForegroundColor Cyan
Invoke-Native -FilePath $PyExe -ArgumentList @(
    "-m", "pip", "install", "-e", ((Join-Path $Root "mcp_server") + "[dev]")
)

$dirs = @(
    "storage\projects", "storage\checkpoints", "storage\renders",
    "storage\logs", "storage\external_workspace", "dist"
)
foreach ($relative in $dirs) {
    $path = Join-Path $Root $relative
    if (-not (Test-Path -LiteralPath $path)) {
        New-Item -ItemType Directory -Path $path | Out-Null
    }
}

Write-ClientConfigs -Interpreter $PyExe

$oldPythonPath = $env:PYTHONPATH
try {
    Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    Invoke-Native -FilePath $PyExe -ArgumentList @("-m", "universal_blender_mcp", "inspect")
    Invoke-Native -FilePath $PyExe -ArgumentList @((Join-Path $Root "scripts\check_compile.py"))
    if (-not $SkipTests) {
        Invoke-Native -FilePath $PyExe -ArgumentList @(
            "-m", "pytest",
            (Join-Path $Root "tests\unit"),
            (Join-Path $Root "tests\security"),
            (Join-Path $Root "tests\compatibility"), "-q"
        )
    }
}
finally {
    if ($null -eq $oldPythonPath) {
        Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    }
    else {
        $env:PYTHONPATH = $oldPythonPath
    }
}

Write-Host "Installation completed." -ForegroundColor Green
Write-Host "Client configurations now use: $PyExe"

