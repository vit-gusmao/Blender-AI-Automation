# Encoding policy: ASCII (Windows PowerShell 5.1 and PowerShell 7)
# Universal Blender MCP - package the Blender 3.6 addon
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$AddonDir = Join-Path $Root "blender_addon\universal_mcp"
$DistDir = Join-Path $Root "dist"

if (-not (Test-Path -LiteralPath $DistDir)) {
    New-Item -ItemType Directory -Path $DistDir | Out-Null
}
if (-not (Test-Path -LiteralPath (Join-Path $AddonDir "__init__.py"))) {
    throw "Addon not found at $AddonDir"
}

$init = Get-Content -LiteralPath (Join-Path $AddonDir "__init__.py") -Raw
if ($init -match '"version":\s*\((\d+),\s*(\d+),\s*(\d+)\)') {
    $Version = "$($Matches[1]).$($Matches[2]).$($Matches[3])"
}
else {
    $Version = "0.0.0"
}

Get-ChildItem -LiteralPath $AddonDir -Recurse -Directory -Filter "__pycache__" |
    Remove-Item -Recurse -Force

$Zip = Join-Path $DistDir "universal_mcp-$Version.zip"
if (Test-Path -LiteralPath $Zip) {
    Remove-Item -LiteralPath $Zip -Force
}

$Parent = Split-Path -Parent $AddonDir
Push-Location $Parent
try {
    Compress-Archive -Path "universal_mcp" -DestinationPath $Zip -CompressionLevel Optimal
}
finally {
    Pop-Location
}

Write-Host "Addon package: $Zip" -ForegroundColor Green
Write-Host "Install: Blender 3.6 > Edit > Preferences > Add-ons > Install; select the ZIP and enable 'Universal Blender MCP'."
Write-Host "Setup: in 3D View > N > Universal MCP, configure the shared project root and connect."
Write-Host "Token: use the same value stored by the server at storage\bridge_token; do not paste it into versioned files."

