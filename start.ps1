param(
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Distro = "Ubuntu-24.04"
$Port = 8000
$AppUrl = "http://127.0.0.1:$Port"
$HealthUrl = "$AppUrl/api/health"
$WslVenvActivate = ".venv-wsl/bin/activate"
$PowerShellExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"

function Convert-ToWslPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$WindowsPath
    )

    $fullPath = (Resolve-Path $WindowsPath).Path
    $drive = $fullPath.Substring(0, 1).ToLowerInvariant()
    $rest = $fullPath.Substring(2).Replace("\", "/")
    return "/mnt/$drive$rest"
}

function Test-AppHealth {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $HealthUrl -TimeoutSec 2
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Open-App {
    if (-not $NoBrowser) {
        Start-Process $AppUrl | Out-Null
    }
}

function Start-BrowserWatcher {
    if ($NoBrowser) {
        return
    }

    $watchCommand = @"
for (`$i = 0; `$i -lt 60; `$i++) {
    try {
        `$response = Invoke-WebRequest -UseBasicParsing -Uri '$HealthUrl' -TimeoutSec 2
        if (`$response.StatusCode -eq 200) {
            Start-Process '$AppUrl' | Out-Null
            exit 0
        }
    } catch {
    }

    Start-Sleep -Seconds 1
}
"@

    Start-Process -FilePath $PowerShellExe -ArgumentList "-NoProfile", "-WindowStyle", "Hidden", "-Command", $watchCommand | Out-Null
}

if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
    throw "wsl.exe was not found. Please install and enable WSL first."
}

if (-not (Test-Path (Join-Path $ProjectRoot ".env"))) {
    throw ".env was not found. Please finish the local project configuration first."
}

$WslProjectRoot = Convert-ToWslPath -WindowsPath $ProjectRoot

if (Test-AppHealth) {
    Write-Host "Service is already running: $AppUrl"
    Open-App
    exit 0
}

$WslCommand = "cd '$WslProjectRoot' && . './$WslVenvActivate' && python main.py --serve-only --host 0.0.0.0 --port $Port"

Write-Host "Starting service..."
Write-Host "Keep this window open while the app is running."
Start-BrowserWatcher

& wsl.exe -d $Distro -- bash -lc $WslCommand
