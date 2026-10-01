# Build MHCOIN Core Desktop on Windows from THIS checkout only.
# Prefer: powershell -File packaging\build_windows.ps1  (venv + hash gates)
# Direct:  powershell -File packaging\build.ps1
#
# Outputs:
#   dist\release\MHCOIN-Core-<ver>-windows-x86_64.exe
#   dist\release\MHCOIN-Core-<ver>-windows-x86_64.zip
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
if (-not $Root) { $Root = (Resolve-Path "$PSScriptRoot\..").Path }
Set-Location $Root

# Prefer active venv created by build_windows.ps1
$Py = "python"
if (Test-Path "$Root\.venv\Scripts\python.exe") {
    $Py = "$Root\.venv\Scripts\python.exe"
}

$Version = & $Py -c "from mhcoin import __version__; print(__version__)"
$RelBase = "MHCOIN-Core-$Version-windows-x86_64"
Write-Host "=== MHCOIN Core Desktop build $Version (Windows) ==="
Write-Host "Python: $Py"

& $Py -m pip install -U pip wheel setuptools
& $Py -m pip install -e ".[desktop]"
& $Py -m pip install "appdirs>=1.4.4" "pywebview>=5.0" "pyinstaller>=6.0"
& $Py -c "import webview, appdirs; print('pywebview', getattr(webview,'__version__','?'), 'appdirs OK')"

if (Test-Path "build\MHCOIN-Core") { Remove-Item -Recurse -Force "build\MHCOIN-Core" }
if (Test-Path "dist\MHCOIN-Core") { Remove-Item -Recurse -Force "dist\MHCOIN-Core" }
if (Test-Path "dist\MHCOIN-Core.exe") { Remove-Item -Force "dist\MHCOIN-Core.exe" }

New-Item -ItemType Directory -Force -Path "dist\release" | Out-Null

# 1) onedir via canonical spec (parity with macOS packaging/mhcoin_core.spec)
& $Py -m PyInstaller packaging\mhcoin_core.spec --noconfirm --clean
if (-not (Test-Path "dist\MHCOIN-Core\MHCOIN-Core.exe")) {
    throw "onedir build missing dist\MHCOIN-Core\MHCOIN-Core.exe"
}

# Guard: never ship developer chain/wallet inside onedir payload
$Bad = Get-ChildItem -Path "dist\MHCOIN-Core" -Recurse -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -in @("chain.sqlite","utxo.sqlite","wallet.json","peers.sqlite","bans.json") }
if ($Bad) {
    throw "Refusing release: data files leaked into onedir:`n$($Bad.FullName -join "`n")"
}

# 2) onefile for end users — same entry as Mac, full mhcoin collect
$IconIco = "packaging\icons\mhcoin.ico"
if (-not (Test-Path $IconIco)) { $IconIco = "mhcoin\desktop\assets\mhcoin.ico" }
$IconArgs = @()
if (Test-Path $IconIco) { $IconArgs = @("--icon", $IconIco) }

$AssetArgs = @()
foreach ($a in @(
  "mhcoin.png", "mhcoin-256.png", "mhcoin-64.png", "mhcoin-32.png",
  "mhcoin.ico", "mhcoin-logo.svg"
)) {
  $p = "mhcoin\desktop\assets\$a"
  if (Test-Path $p) {
    $AssetArgs += @("--add-data", "${p};mhcoin/desktop/assets")
  }
}

$Hidden = @(
  "mhcoin",
  "mhcoin.desktop",
  "mhcoin.desktop.webui",
  "mhcoin.desktop.controller",
  "mhcoin.desktop.app",
  "mhcoin.desktop.seeds",
  "mhcoin.desktop.prefs",
  "mhcoin.consensus",
  "mhcoin.consensus.params",
  "mhcoin.consensus.difficulty",
  "mhcoin.consensus.proof_of_work",
  "mhcoin.consensus.chain_work",
  "mhcoin.consensus.block_reward",
  "mhcoin.network",
  "mhcoin.network.seeds",
  "mhcoin.network.p2p",
  "mhcoin.node.runtime",
  "mhcoin.mining.miner",
  "mhcoin.blockchain.chain",
  "mhcoin.blockchain.genesis",
  "webview",
  "appdirs"
)
$HiddenArgs = @()
foreach ($h in $Hidden) { $HiddenArgs += @("--hidden-import", $h) }

& $Py -m PyInstaller `
  --noconfirm --clean --onefile --windowed `
  --name "MHCOIN-Core" `
  --paths "." `
  --collect-submodules mhcoin `
  --collect-all webview `
  @HiddenArgs `
  @AssetArgs `
  --exclude-module PySide6 --exclude-module PyQt5 --exclude-module PyQt6 `
  @IconArgs `
  packaging\entry_desktop.py

if (-not (Test-Path "dist\MHCOIN-Core.exe")) {
    throw "onefile build missing dist\MHCOIN-Core.exe"
}

$ExeOut = "dist\release\$RelBase.exe"
Copy-Item -Force "dist\MHCOIN-Core.exe" $ExeOut

$Stage = "dist\release\$RelBase-onedir"
if (Test-Path $Stage) { Remove-Item -Recurse -Force $Stage }
Copy-Item -Recurse "dist\MHCOIN-Core" $Stage
$Zip = "dist\release\$RelBase.zip"
if (Test-Path $Zip) { Remove-Item -Force $Zip }
Compress-Archive -Path "$Stage\*" -DestinationPath $Zip

Write-Host "Windows exe: $ExeOut"
Write-Host "Windows zip: $Zip"
Get-ChildItem dist\release | Format-Table Name, Length, LastWriteTime
