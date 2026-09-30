# Security

## Wallet

- Private keys are encrypted on disk (`wallet.json`, scrypt + AES-GCM).
- Your **password is never stored** in the wallet file.
- Backup = encrypted `wallet.json` **and** the password (both required to recover).
- Use **Settings → Download wallet backup** in Desktop, or copy `wallet.json` manually.
- Never commit `wallet.json`, seed phrases, or passwords to git.

## Node / network

- Mainnet P2P listens on TCP **8333**.
- Only expose required ports. Do not publish operator LAN URLs or private keys in issues/PRs.

## Reporting

If you find a vulnerability in consensus, P2P, or wallet crypto, contact the maintainers privately before opening a public issue.
