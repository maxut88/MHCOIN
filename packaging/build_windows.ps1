# MHCOIN Core Desktop — Windows rebuild from CURRENT canonical source only.
#
# Usage (on a real Windows machine, from the MHCOIN repo root or by path):
#   powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
#
# Optional:
#   $env:MHCOIN_EXPECTED_COMMIT = "843e9053ba322efb27c865a759bc4e3b9c75e228"
#   $env:MHCOIN_SKIP_COMMIT_CHECK = "1"   # only for dirty local trees while debugging
#
# This script NEVER downloads MHCOIN_desktop_rc.tgz / mainnet-sync.zip.
# It builds only from the checkout you run it in (must be current HEAD / 843e905).

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
if (-not $Root) { $Root = (Resolve-Path "$PSScriptRoot\..").Path }
Set-Location $Root

$ExpectedCommit = if ($env:MHCOIN_EXPECTED_COMMIT) {
    $env:MHCOIN_EXPECTED_COMMIT.Trim().ToLowerInvariant()
} else {
    "843e9053ba322efb27c865a759bc4e3b9c75e228"
}

$ExpectedUi = @{
    "mhcoin\desktop\webui.py"      = "556c222b9a0e8e3662b7e1ad4be48034c05c966ace3f6a885cd1564f71474371"
    "mhcoin\desktop\app.py"        = "8a5a999670aedfe83753863643c1d7b208597ac1ed27f502108ad9d5ba118c89"
    "mhcoin\desktop\controller.py" = "592cd8f331eae003a4de23b9c2e743f2147380332c5dca3f6621376173e54d4e"
}

Write-Host "=== MHCOIN Windows rebuild (canonical source) ==="
Write-Host "Root: $Root"

function Get-FileSha256([string]$Path) {
    return (Get-FileHash -Algorithm SHA256 -Path $Path).Hash.ToLowerInvariant()
}

# --- Refuse stale packaging copies as source of truth ---
foreach ($bad in @(
    "rc1_ops\download\webui.py",
    "rc1_ops\download\app.py",
    "rc1_ops\download\controller.py",
    "rc1_ops\win\webui.py"
)) {
    if (Test-Path $bad) {
        Write-Host "NOTE: ignoring stale file $bad (not used for build)."
    }
}

# --- Commit check ---
$Head = $null
if (Get-Command git -ErrorAction SilentlyContinue) {
    $Head = (git rev-parse HEAD 2>$null).Trim().ToLowerInvariant()
}
if (-not $Head) {
    $marker = Join-Path $Root "SOURCE_COMMIT.txt"
    if (Test-Path $marker) {
        $Head = (Get-Content $marker -Raw).Trim().ToLowerInvariant()
    }
}
if (-not $Head) {
    throw "Cannot determine source commit (git HEAD / SOURCE_COMMIT.txt missing)."
}
Write-Host "Source commit: $Head"
if ($env:MHCOIN_SKIP_COMMIT_CHECK -ne "1") {
    if (-not $Head.StartsWith($ExpectedCommit.Substring(0, [Math]::Min(7, $ExpectedCommit.Length))) -and
        $Head -ne $ExpectedCommit) {
        throw "Refusing build: HEAD=$Head expected prefix of $ExpectedCommit. Set MHCOIN_SKIP_COMMIT_CHECK=1 only if intentional."
    }
}

# --- Canonical UI hashes ---
foreach ($rel in $ExpectedUi.Keys) {
    $path = Join-Path $Root $rel
    if (-not (Test-Path $path)) { throw "Missing canonical UI file: $rel" }
    $got = Get-FileSha256 $path
    $want = $ExpectedUi[$rel]
    if ($got -ne $want) {
        throw "UI hash mismatch for $rel`n  got:  $got`n  want: $want`nRefuse build — Windows must match Mac canonical UI."
    }
    Write-Host "UI OK $rel"
}

# --- Entry must point at canonical webui ---
$Entry = Join-Path $Root "packaging\entry_desktop.py"
$EntryText = Get-Content $Entry -Raw
if ($EntryText -notmatch "mhcoin\.desktop\.webui") {
    throw "packaging/entry_desktop.py must import mhcoin.desktop.webui"
}
if ($EntryText -notmatch "run_web_desktop") {
    throw "packaging/entry_desktop.py must call run_web_desktop"
}
Write-Host "Entry OK packaging\entry_desktop.py"

# --- Refuse bundling live chain/wallet into the tree used as datas ---
$Forbidden = @(
    "chain.sqlite", "utxo.sqlite", "wallet.json", "peers.sqlite", "bans.json"
)
$Hits = @()
Get-ChildItem -Path $Root -Recurse -File -ErrorAction SilentlyContinue |
    Where-Object {
        $_.FullName -notmatch '\\.venv\\|\\dist\\|\\build\\|\\mainnet\\|\\\.git\\' -and
        ($Forbidden -contains $_.Name)
    } |
    ForEach-Object { $Hits += $_.FullName }
# Allow wallet/chain only under user-data-like dirs already excluded; warn if under packaging/
$PackHits = $Hits | Where-Object { $_ -match '\\packaging\\' -or $_ -match '\\mhcoin\\' }
if ($PackHits) {
    throw "Refusing build: forbidden data files under package tree:`n$($PackHits -join "`n")"
}

# --- venv ---
$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) {
    Write-Host "Creating .venv …"
    if (Get-Command py -ErrorAction SilentlyContinue) {
        py -3 -m venv .venv
    } else {
        python -m venv .venv
    }
}
if (-not (Test-Path $Py)) { throw "Failed to create .venv\Scripts\python.exe" }

& $Py -m pip install -U pip wheel setuptools
& $Py -m pip install -e ".[desktop]"
& $Py -m pip install "appdirs>=1.4.4" "pywebview>=5.0" "pyinstaller>=6.0"
& $Py -c "import webview, appdirs, cryptography, Crypto; print('deps OK', getattr(webview,'__version__','?'), appdirs.__file__)"

# Prefer venv interpreter for packaging\build.ps1 (it looks up .venv\Scripts\python.exe)
$env:PATH = "$(Join-Path $Root '.venv\Scripts');" + $env:PATH

# --- Build ---
Write-Host "Running packaging\build.ps1 …"
$BuildPs1 = Join-Path $Root "packaging\build.ps1"
& powershell -NoProfile -ExecutionPolicy Bypass -File $BuildPs1
if ($LASTEXITCODE -ne 0) { throw "build.ps1 failed with exit $LASTEXITCODE" }

$Version = & $Py -c "from mhcoin import __version__; print(__version__)"
$RelBase = "MHCOIN-Core-$Version-windows-x86_64"
$ExeOut = Join-Path $Root "dist\release\$RelBase.exe"
$ZipOut = Join-Path $Root "dist\release\$RelBase.zip"
if (-not (Test-Path $ExeOut)) { throw "Missing output EXE: $ExeOut" }

$ExeSha = Get-FileSha256 $ExeOut
$ExeSize = (Get-Item $ExeOut).Length
$ShaFile = "$ExeOut.sha256"
"$ExeSha  $RelBase.exe" | Set-Content -Encoding ascii $ShaFile

# Record build provenance next to artifact (not bundled into EXE)
$Meta = [ordered]@{
    built_utc        = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    source_commit    = $Head
    expected_commit  = $ExpectedCommit
    version          = $Version
    entry            = "packaging/entry_desktop.py"
    ui               = $ExpectedUi
    artifact_exe     = $ExeOut
    artifact_zip     = $(if (Test-Path $ZipOut) { $ZipOut } else { $null })
    sha256_exe       = $ExeSha
    size_exe         = $ExeSize
    public_seed      = "176.38.3.168:8333"
    genesis          = "62e078a7ca0dfeac4c6451e5852d5ec714c7aacbff4e48f1d8cc8aa9560a0000"
    fingerprint      = "21f256498747b313795b65c9e26f0dd1cb23ee762813bc0d593c2c513e787ae9"
    known_block_94   = "b87b83faba1b745211f10bad9ccde1de7dd46dd37b3554158cac60bc65000000"
    chainwork_94     = 493318281
    stale_download_exe_do_not_overwrite_until_runtime_pass = $true
}
$MetaPath = Join-Path $Root "dist\release\$RelBase.build.json"
$Meta | ConvertTo-Json -Depth 6 | Set-Content -Encoding utf8 $MetaPath

Write-Host ""
Write-Host "=== BUILD OK ==="
Write-Host "EXE:  $ExeOut"
Write-Host "SIZE: $ExeSize"
Write-Host "SHA256: $ExeSha"
Write-Host "META: $MetaPath"
Write-Host ""
Write-Host "NEXT: run the EXE on Windows mainnet, sync to seed 176.38.3.168:8333,"
Write-Host "      confirm height>=94 hash b87b83fa…65000000, then replace download server copy."
Write-Host "STATUS: READY_FOR_WINDOWS_RUNTIME_TEST (this host produced the artifact)."
)
