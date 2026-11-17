# =========================================================================
# VaultSwap // MTG Pod Binder & Meeple Snapshot - Start Script
# =========================================================================

$PSScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $PSScriptRoot

Write-Host ""
Write-Host " ========================================================" -ForegroundColor Cyan
Write-Host "   VAULTSWAP // MTG POD BINDER & MEEPLE SNAPSHOT" -ForegroundColor White
Write-Host "   Version: 1.0" -ForegroundColor DarkGray
Write-Host " ========================================================" -ForegroundColor Cyan
Write-Host ""

$Port = 5055
if (Test-Path ".env") {
    $envPortLine = Get-Content ".env" | Where-Object { $_ -match "^PORT\s*=\s*(\d+)" }
    if ($envPortLine -match "^PORT\s*=\s*(\d+)") {
        $Port = [int]$Matches[1]
    }
}

# Check if port is already occupied
$existingConn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($existingConn) {
    $procId = $existingConn.OwningProcess
    $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
    Write-Host " [!] Alert: Port $Port is already in use by process $($proc.ProcessName) (PID: $procId)." -ForegroundColor Yellow
    Write-Host " [*] Opening browser to active server: http://localhost:$Port" -ForegroundColor Cyan
    Start-Process "http://localhost:$Port"
    Write-Host " [*] Run .\stop_app.ps1 (or stop.bat) to terminate the existing server if needed." -ForegroundColor DarkGray
    Write-Host ""
    Read-Host "Press Enter to exit"
    exit 0
}

# Resolve Python executable (virtual environment first)
$pythonExe = "python"
$candidates = @(
    "$PSScriptRoot\venv\Scripts\python.exe",
    "$PSScriptRoot\.venv\Scripts\python.exe",
    "$PSScriptRoot\..\.venv\Scripts\python.exe",
    "$PSScriptRoot\..\venv\Scripts\python.exe"
)
foreach ($cand in $candidates) {
    if (Test-Path $cand) {
        $pythonExe = $cand
        Write-Host " [SYS:ENV] Utilizing virtual environment: $cand" -ForegroundColor DarkGray
        break
    }
}

$url = "http://localhost:$Port"
$pidFile = Join-Path $PSScriptRoot ".vaultswap.pid"
$PID | Out-File -FilePath $pidFile -Encoding utf8

Write-Host " [SYS:INIT] Starting VaultSwap Server on $url..." -ForegroundColor Cyan
Write-Host " [*] Press Ctrl+C in this window or run .\stop_app.ps1 to shut down." -ForegroundColor DarkGray
Write-Host ""

try {
    & $pythonExe app.py
}
catch {
    Write-Host ""
    Write-Host " [!] Error encountered while running VaultSwap: $_" -ForegroundColor Red
}
finally {
    if (Test-Path $pidFile) {
        Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
    }
}

Write-Host ""
Read-Host "Press Enter to exit"
