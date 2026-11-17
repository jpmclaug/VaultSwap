# =========================================================================
# VaultSwap - Stop Local Server Script
# =========================================================================

$PSScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Definition
$Port = 5055
$envPath = Join-Path $PSScriptRoot ".env"
if (Test-Path $envPath) {
    $envPortLine = Get-Content $envPath | Where-Object { $_ -match "^PORT\s*=\s*(\d+)" }
    if ($envPortLine -match "^PORT\s*=\s*(\d+)") {
        $Port = [int]$Matches[1]
    }
}

$conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
foreach ($c in $conns) {
    Write-Host " [VaultSwap] Stopping PID $($c.OwningProcess) on port $Port..." -ForegroundColor Yellow
    Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue
}

$pidFile = Join-Path $PSScriptRoot ".vaultswap.pid"
if (Test-Path $pidFile) {
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
}
Write-Host " [VaultSwap] Stopped." -ForegroundColor Green
