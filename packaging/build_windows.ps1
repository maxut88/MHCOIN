# MHCOIN Core Desktop — Windows rebuild from CURRENT canonical source only.
#
# Usage (on a real Windows machine, from the MHCOIN repo root or by path):
#   powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
#
# Optional:
#   $env:MHCOIN_EXPECTED_COMMIT = "<full sha>"   # pin a release commit
#   $env:MHCOIN_SKIP_COMMIT_CHECK = "1"          # dirty local trees / WIP
#   $env:MHCOIN_SKIP_UI_HASH_CHECK = "1"         # only while intentionally editing UI
#
# This script NEVER downloads MHCOIN_desktop_rc.tgz / mainnet-sync.zip.
# It builds only from the checkout you run it in. Genesis/protocol come from
# mhcoin.consensus.params (bundled via PyInstaller) — never from stale download ZIPs.

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
if (-not $Root) { $Root = (Resolve-Path "$PSScriptRoot\..").Path }
Set-Location $Root

# Prefer active venv for early Python probes (same as build.ps1).
$PyProbe = "python"
if (Test-Path "$Root\.venv\Scripts\python.exe") {
    $PyProbe = "$Root\.venv\Scripts\python.exe"
}

# Default: no commit pin (build whatever is checked out). Set MHCOIN_EXPECTED_COMMIT to pin.
$ExpectedCommit = if ($env:MHCOIN_EXPECTED_COMMIT) {
    $env:MHCOIN_EXPECTED_COMMIT.Trim().ToLowerInvariant()
} else {
    ""
}

# UI hash gate — refreshed for 0.4.2.0 / genesis v2 tree. Override with SKIP if editing UI.
$ExpectedUi = @{
    "mhcoin\desktop\webui.py"      = "e073693de40c02152b27e238b52d70ae81f555d0ee3f934337a78414c5af4c58"
    "mhcoin\desktop\app.py"        = "fd316c49fe0ef90d392a699c68ed72044126464abf1ab5b3e2553123e96a4004"
    "mhcoin\desktop\controller.py" = "5d408725dbf230ff5adf9247b932d4e4f4d9b6a36215da2fce426d72b650be4a"
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
if ($env:MHCOIN_SKIP_COMMIT_CHECK -ne "1" -and $ExpectedCommit) {
    if (-not $Head.StartsWith($ExpectedCommit.Substring(0, [Math]::Min(7, $ExpectedCommit.Length))) -and
        $Head -ne $ExpectedCommit) {
        throw "Refusing build: HEAD=$Head expected prefix of $ExpectedCommit. Set MHCOIN_SKIP_COMMIT_CHECK=1 only if intentional."
    }
} elseif (-not $ExpectedCommit) {
    Write-Host "NOTE: no MHCOIN_EXPECTED_COMMIT pin — building current checkout."
}

# --- Canonical UI hashes ---
if ($env:MHCOIN_SKIP_UI_HASH_CHECK -eq "1") {
    Write-Host "NOTE: MHCOIN_SKIP_UI_HASH_CHECK=1 — skipping UI hash gate."
} else {
    foreach ($rel in $ExpectedUi.Keys) {
        $path = Join-Path $Root $rel
        if (-not (Test-Path $path)) { throw "Missing canonical UI file: $rel" }
        $got = Get-FileSha256 $path
        $want = $ExpectedUi[$rel]
        if ($got -ne $want) {
            throw "UI hash mismatch for $rel`n  got:  $got`n  want: $want`nRefuse build — Windows must match Mac canonical UI. Set MHCOIN_SKIP_UI_HASH_CHECK=1 only if intentional."
        }
        Write-Host "UI OK $rel"
    }
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

# Provenance from live params.py (never hardcode old genesis / protocol).
$CanonJson = & $Py -c @"
import json
from mhcoin import __version__
from mhcoin.consensus.params import PROTOCOL_VERSION, get_network_params
from mhcoin.audit.rc1 import consensus_source_fingerprint
p = get_network_params('mainnet')
fp = consensus_source_fingerprint()
print(json.dumps({
    'version': __version__,
    'protocol_version': PROTOCOL_VERSION,
    'genesis': p.genesis_hash_hex,
    'genesis_bits': hex(p.genesis_bits),
    'genesis_nonce': p.genesis_nonce,
    'genesis_timestamp': p.genesis_timestamp,
    'genesis_merkle': p.genesis_merkle_hex,
    'coinbase_extra': p.genesis_coinbase_extra.decode('utf-8', 'replace'),
    'fingerprint': fp.get('fingerprint') if isinstance(fp, dict) else fp,
}))
"@
if ($LASTEXITCODE -ne 0 -or -not $CanonJson) {
    throw "Failed to read canonical mainnet params from Python (params.py)."
}
$Canon = $CanonJson | ConvertFrom-Json

$Meta = [ordered]@{
    built_utc        = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    source_commit    = $Head
    expected_commit  = $(if ($ExpectedCommit) { $ExpectedCommit } else { $null })
    version          = $Version
    protocol_version = $Canon.protocol_version
    entry            = "packaging/entry_desktop.py"
    ui               = $ExpectedUi
    artifact_exe     = $ExeOut
    artifact_zip     = $(if (Test-Path $ZipOut) { $ZipOut } else { $null })
    sha256_exe       = $ExeSha
    size_exe         = $ExeSize
    public_seed      = "176.38.3.168:8333"
    genesis          = $Canon.genesis
    genesis_bits     = $Canon.genesis_bits
    genesis_nonce    = $Canon.genesis_nonce
    genesis_timestamp = $Canon.genesis_timestamp
    genesis_merkle   = $Canon.genesis_merkle
    coinbase_extra   = $Canon.coinbase_extra
    fingerprint      = $Canon.fingerprint
    network          = "mainnet genesis v2 / protocol v3"
}
$MetaPath = Join-Path $Root "dist\release\$RelBase.build.json"
$Meta | ConvertTo-Json -Depth 6 | Set-Content -Encoding utf8 $MetaPath

Write-Host ""
Write-Host "=== BUILD OK ==="
Write-Host "EXE:  $ExeOut"
Write-Host "SIZE: $ExeSize"
Write-Host "SHA256: $ExeSha"
Write-Host "META: $MetaPath"
Write-Host "GENESIS: $($Canon.genesis)"
Write-Host "PROTOCOL: $($Canon.protocol_version)"
Write-Host ""
Write-Host "NEXT: run the EXE on Windows mainnet, sync to seed 176.38.3.168:8333,"
Write-Host "      confirm tip starts at genesis $($Canon.genesis.Substring(0,16))… then replace download server copy."
Write-Host "STATUS: READY_FOR_WINDOWS_RUNTIME_TEST (this host produced the artifact)."
)
