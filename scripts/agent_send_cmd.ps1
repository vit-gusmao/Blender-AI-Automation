# Encoding policy: ASCII (Windows PowerShell 5.1 and PowerShell 7)
param(
    [Parameter(Mandatory=$true)][string]$Operation,
    [Parameter(Mandatory=$false)][string]$ArgumentsJson = '{}',
    [int]$TimeoutSec = 120
)
$ErrorActionPreference = 'Stop'
$token = (Get-Content 'C:\Blender\RPA\storage\bridge_token' -TotalCount 1).Trim()
$headers = @{ 'X-Bridge-Token' = $token }
$bodyObj = @{ operation = $Operation; arguments = ($ArgumentsJson | ConvertFrom-Json) }
if ($Operation -eq 'python_execute_blender') {
    $bodyObj.authorization = @{ level = 'advanced'; approved = $true }
}
$body = $bodyObj | ConvertTo-Json -Depth 20 -Compress
$bodyBytes = [System.Text.Encoding]::UTF8.GetBytes($body)
$r = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/commands' -Method Post -Headers $headers -ContentType 'application/json; charset=utf-8' -Body $bodyBytes
$cid = $r.command_id
$deadline = (Get-Date).AddSeconds($TimeoutSec)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 300
    $j = Invoke-RestMethod -Uri "http://127.0.0.1:8765/commands/$cid" -Method Get -Headers $headers
    $st = $j.job.status
    if ($st -in @('completed','failed')) {
        $j.job | ConvertTo-Json -Depth 30 -Compress
        return
    }
}
Write-Output "{\"error\":\"timeout after ${TimeoutSec}s\",\"command_id\":\"$cid\"}"
