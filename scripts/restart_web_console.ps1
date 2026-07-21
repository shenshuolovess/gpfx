param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8000,
    [switch]$CheckOnly
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$server = Join-Path $projectRoot "src\web_console.py"

if (-not (Test-Path -LiteralPath $python)) {
    Write-Host "Python virtual environment was not found: $python" -ForegroundColor Red
    exit 1
}

if (-not (Test-Path -LiteralPath $server)) {
    Write-Host "Web console entry was not found: $server" -ForegroundColor Red
    exit 1
}

$listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)

if ($CheckOnly) {
    Write-Host "Restart script is valid. Listener count on port ${Port}: $($listeners.Count)"
    exit 0
}

foreach ($listener in $listeners) {
    $processId = $listener.OwningProcess
    $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $processId" -ErrorAction SilentlyContinue
    $commandLine = [string]$processInfo.CommandLine

    if ($commandLine -notmatch "(?i)web_console\.py") {
        Write-Host "Port $Port is occupied by another program (PID=$processId). It was not stopped." -ForegroundColor Red
        exit 2
    }

    Write-Host "Stopping the previous research console (PID=$processId)..."
    Stop-Process -Id $processId -Force
}

$deadline = (Get-Date).AddSeconds(10)
do {
    $remaining = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    if ($remaining.Count -eq 0) {
        break
    }
    Start-Sleep -Milliseconds 250
} while ((Get-Date) -lt $deadline)

if ($remaining.Count -gt 0) {
    Write-Host "Port $Port was not released after 10 seconds." -ForegroundColor Red
    exit 3
}

Write-Host "Starting the research console at http://127.0.0.1:${Port} ..." -ForegroundColor Green

$openBrowserCommand = "Start-Sleep -Seconds 2; Start-Process 'http://127.0.0.1:${Port}'"
Start-Process powershell.exe -WindowStyle Hidden -ArgumentList @(
    "-NoProfile",
    "-WindowStyle", "Hidden",
    "-Command", $openBrowserCommand
)

Set-Location -LiteralPath $projectRoot
& $python $server
exit $LASTEXITCODE
