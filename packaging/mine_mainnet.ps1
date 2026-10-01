# MHCOIN solo miner via PowerShell (same chain as Desktop).
# Stop Desktop mining first. Usage: .\packaging\mine_mainnet.ps1 [mhc1…]
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if (-not $env:MHCOIN_NETWORK) { $env:MHCOIN_NETWORK = "mainnet" }
$env:PYTHONPATH = if ($env:PYTHONPATH) { "$Root;$env:PYTHONPATH" } else { $Root }

$Py = "python"
$VenvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (Test-Path $VenvPy) { $Py = $VenvPy }

$Addr = $args[0]
if (-not $Addr) { $Addr = $env:MHCOIN_MINER_ADDRESS }
if (-not $Addr) {
  Write-Host "MHCOIN terminal miner — network=$($env:MHCOIN_NETWORK)"
  Write-Host "Data: $env:USERPROFILE\.mhcoin\$($env:MHCOIN_NETWORK)"
  Write-Host "Copy address from Desktop → Receive, then paste below.`n"
  $Addr = Read-Host "Reward address (mhc1...)"
}
$Addr = ($Addr -replace "\s+", "")
if (-not $Addr) { throw "No address — abort." }

Write-Host "`nStarting solo miner…"
Write-Host "  network: $($env:MHCOIN_NETWORK)"
Write-Host "  address: $Addr"
Write-Host "  stop:    Ctrl+C`n"

& $Py -m mhcoin.cli mining start --network $env:MHCOIN_NETWORK --address $Addr
