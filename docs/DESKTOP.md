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
```

If `pywebview` is missing, the UI opens in the system browser.
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
