# MHCOIN Core Desktop

Same consensus core as the CLI, wrapped in a native window (`pywebview`).

## Users

| Platform | Package |
|----------|---------|
| Windows | `MHCOIN-Core-<ver>-windows-x86_64.exe` |
| macOS | `MHCOIN-Core-<ver>-macos.dmg` |
| Linux | `MHCOIN-Core-<ver>-x86_64.AppImage` |

Double-click → wallet, send/receive, mining, network. Data under `~/.mhcoin/`.

## Developers

```bash
pip install -e ".[desktop]"
python -m mhcoin.desktop --network mainnet
# or: --network localnet
# or: bash packaging/run_desktop.sh
```

### Native window vs web version (browser)

Both use the **same** UI (`mhcoin/desktop/webui.py`) and the same local HTTP API on `127.0.0.1` (default port `18765`). There is no separate public website build.

| Mode | How |
|------|-----|
| Mac / native window | default when `pywebview` is installed |
| Web version (browser) | `MHCOIN_DESKTOP_BROWSER=1` or missing `pywebview` |

**Open web version on Mac:**

```bash
cd ~/src/MHCOIN   # or wherever you synced the tree
source .venv/bin/activate
export MHCOIN_DESKTOP_BROWSER=1
bash packaging/run_desktop.sh
# Browser opens http://127.0.0.1:18765/ — Ctrl+C in Terminal stops the server.
```

If `pywebview` is missing, the UI opens in the system browser automatically.
Force browser: `MHCOIN_DESKTOP_BROWSER=1`.

## Build

```bash
bash packaging/build.sh                 # Linux / macOS
powershell -File packaging/build.ps1    # Windows
```

Output: `dist/release/`

## Backup

**Settings → Download wallet backup** saves encrypted `wallet.json`.
Keep your wallet password separately — it is not inside the file.

## Mining

- **Mining** tab → **Start mining** — live P2P (same tip as the network).
- **Instructions** — terminal commands for macOS / Linux / Windows.
- Do not mine from Desktop and Terminal against the same data directory at the same time.

