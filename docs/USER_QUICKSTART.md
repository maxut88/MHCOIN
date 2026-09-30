# CLI quickstart

Five commands:

```text
wallet create → wallet address → mining start → wallet balance → wallet send
```

Practice on **localnet** first.

## Install

```bash
cd MHCOIN
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip setuptools wheel
pip install -r requirements.txt
# optional: pip install -e .
export PYTHONPATH="$PWD"
```

`mhcoin` below means `python3 -m mhcoin.cli` (or the installed `mhcoin` entry point).

## Localnet

```bash
export MHCOIN_NETWORK=localnet
export MHCOIN_DATA="$HOME/.mhcoin/localnet"

mhcoin wallet create
mhcoin wallet address
mhcoin mining start --address mhc1YOUR_ADDRESS --blocks 1
mhcoin wallet balance
mhcoin wallet send mhc1OTHER… 10
```

## Mainnet

```bash
export MHCOIN_NETWORK=mainnet
export MHCOIN_DATA="$HOME/.mhcoin/mainnet"

mhcoin genesis verify --network mainnet
mhcoin wallet create
mhcoin node start --network mainnet --connect 176.38.3.168:8333
mhcoin mining start --address mhc1YOUR_ADDRESS
```

## Notes

- Private keys stay encrypted on disk; unlock/send needs your password.
- Mining only needs your address in the coinbase (no password).
- Desktop GUI: `docs/DESKTOP.md`.
