# MHCOIN Core Desktop — Windows launcher (same UI as macOS)
# Usage (from repo root):
#   powershell -ExecutionPolicy Bypass -File packaging\run_desktop.ps1
# Optional:
#   $env:MHCOIN_CONNECT = "176.38.3.168:8333"
#   $env:MHCOIN_NETWORK = "mainnet"

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
if (-not $Root) { $Root = (Resolve-Path "$PSScriptRoot\..").Path }
Set-Location $Root

$Py = $null
if (Test-Path "$Root\.venv\Scripts\python.exe") {
    $Py = "$Root\.venv\Scripts\python.exe"
} else {
    Write-Host "Creating .venv …"
    py -3 -m venv .venv
    if (-not (Test-Path "$Root\.venv\Scripts\python.exe")) {
        python -m venv .venv
    }
    $Py = "$Root\.venv\Scripts\python.exe"
}

& $Py -m pip install -U pip wheel setuptools | Out-Null
& $Py -m pip install -r requirements.txt
& $Py -m pip install "pywebview>=5.0"

try {
    & $Py -c "import webview; print('pywebview', getattr(webview,'__version__','?'))"
} catch {
    Write-Host "ERROR: pywebview failed to import. Install WebView2 Runtime from Microsoft, then retry."
    exit 1
}

$env:PYTHONPATH = $Root
if (-not $env:MHCOIN_NETWORK) { $env:MHCOIN_NETWORK = "mainnet" }
if (-not $env:MHCOIN_CONNECT) { $env:MHCOIN_CONNECT = "176.38.3.168:8333" }
Remove-Item Env:MHCOIN_DATA -ErrorAction SilentlyContinue
Remove-Item Env:MHCOIN_DESKTOP_BROWSER -ErrorAction SilentlyContinue

Write-Host "MHCOIN Core Desktop (Windows)"
Write-Host "  network=$env:MHCOIN_NETWORK  connect=$env:MHCOIN_CONNECT"
Write-Host "  data=%USERPROFILE%\.mhcoin\$($env:MHCOIN_NETWORK)"
& $Py -m mhcoin.desktop --network $env:MHCOIN_NETWORK
