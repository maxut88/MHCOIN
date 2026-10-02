# Security

## Wallet

- Private keys are encrypted on disk (`wallet.json`, scrypt + AES-GCM).
- The **password is never stored** in the wallet file.
- Backup = encrypted `wallet.json` **and** the password (both required to recover).
- Use **Settings → Download wallet backup** in Desktop, or copy `wallet.json` manually.
- Never commit `wallet.json`, seed phrases, passwords, or `.env` files to git.

## Node / network

- Mainnet P2P listens on TCP **8333**.
- Expose only required ports. Do not publish operator LAN URLs, private keys, or wallet files in issues/PRs.
- Bootstrap seed (`176.38.3.168:8333`) is for peer discovery only — always validate the chain yourself.

## Consensus

- Mainnet genesis and consensus fingerprint are **frozen**. Changing them without a coordinated upgrade forks the network.
- Report consensus / PoW / fingerprint mismatches with `mhcoin audit fingerprint` output.

## Reporting

If you find a vulnerability in consensus, P2P, or wallet crypto, contact the maintainers **privately** before opening a public issue.
