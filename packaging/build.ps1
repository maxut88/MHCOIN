# Build MHCOIN Core Desktop on Windows
# → MHCOIN-Core-<ver>-windows-x86_64.exe (onefile) + companion zip of onedir
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
if (-not $Root) { $Root = (Resolve-Path "$PSScriptRoot\..").Path }
Set-Location $Root

$Version = python -c "from mhcoin import __version__; print(__version__)"
$RelBase = "MHCOIN-Core-$Version-windows-x86_64"
Write-Host "=== MHCOIN Core Desktop build $Version (Windows) ==="

python -m pip install -U pip wheel setuptools pyinstaller
python -m pip install -r requirements.txt
python -m pip install "pywebview>=5.0"
python -c "import webview; print('pywebview OK')"

if (Test-Path "build\MHCOIN-Core") { Remove-Item -Recurse -Force "build\MHCOIN-Core" }
if (Test-Path "dist\MHCOIN-Core") { Remove-Item -Recurse -Force "dist\MHCOIN-Core" }
if (Test-Path "dist\MHCOIN-Core.exe") { Remove-Item -Force "dist\MHCOIN-Core.exe" }

# onedir (preferred layout; also used for zip)
python -m PyInstaller packaging\mhcoin_core.spec --noconfirm --clean

New-Item -ItemType Directory -Force -Path "dist\release" | Out-Null

# onefile release .exe for end users
$IconIco = "packaging\icons\mhcoin.ico"
if (-not (Test-Path $IconIco)) { $IconIco = "mhcoin\desktop\assets\mhcoin.ico" }
$IconArgs = @()
if (Test-Path $IconIco) { $IconArgs = @("--icon", $IconIco) }

# Bundle same branded assets as macOS (ico + png + svg logo)
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

python -m PyInstaller `
  --noconfirm --clean --onefile --windowed `
  --name "MHCOIN-Core" `
  --paths "." `
  --hidden-import mhcoin.desktop.webui `
  --hidden-import mhcoin.desktop.controller `
  --hidden-import mhcoin.desktop.seeds `
  --hidden-import mhcoin.desktop.prefs `
  --hidden-import mhcoin.desktop.app `
  --hidden-import webview `
  --collect-all webview `
  @AssetArgs `
  --exclude-module PySide6 --exclude-module PyQt5 --exclude-module PyQt6 `
  @IconArgs `
  packaging\entry_desktop.py

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
Get-ChildItem dist\release
