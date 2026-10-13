# MHCOIN Wallet

## Address format

- Bech32 with human-readable part **`mhc`**
- Display form: **`mhc1…`** (witness v0, 20-byte pubkey hash)
- Checksum: Bech32 BCH code (BIP-173 family; HRP ``mhc``)

## Key derivation (BIP84 HD)

New wallets use **BIP39** + **BIP32**:

```
BIP39 mnemonic (12 or 24 English words)
    → PBKDF2 seed (64 bytes)
    → Account m/84'/0'/0'
         ├─ receive  m/84'/0'/0'/0/n   (external — share these)
         └─ change   m/84'/0'/0'/1/n   (internal — used by Send)
    → secp256k1 key → HASH160 → Bech32 (hrp=mhc) → mhc1…
```

- Desktop **Receive → New address** derives the next unused external `n`.
- **Send** spends UTXOs from the whole account and returns change on the internal chain.
- **Overview balance** = sum of all receive + change UTXOs for the active account.
- **Restore** runs a gap-limit scan (default **20**) on receive and change against chain UTXOs.
- **Account xpub** (`m/84'/0'/0'`) can be exported for watch-only wallets.

You can also **import** a raw key (hex / WIF) — single address, no HD derive / xpub.

## Storage

File: `wallet.json` under `~/.mhcoin/<network>/`.

- Per-address encrypted private keys (scrypt + AES-GCM); empty for watch-only
- Encrypted BIP39 mnemonic on the account root
- `account_id` groups receive/change under one HD account
- Optional plaintext `account_xpub` on the root (safe to share for watch-only)
- Desktop **Backup** copies this file — keep the password separately

## CLI

| Command | Description |
|---------|-------------|
| `mhcoin wallet create` | New HD wallet — prints BIP39 seed **once** + address |
| `mhcoin wallet restore [--gap-limit 20]` | Restore + gap-scan receive/change |
| `mhcoin wallet new-address` | Next receive `…/0/n` |
| `mhcoin wallet list-addresses` | List receive addresses (* = active) |
| `mhcoin wallet export-xpub` | Account xpub |
| `mhcoin wallet import-xpub` | Watch-only account |
| `mhcoin wallet import-key` | Import WIF or hex |
| `mhcoin wallet export-seed` | Show mnemonic |
| `mhcoin wallet export-key` | Export active key as WIF |
| `mhcoin wallet address` | Active address |
| `mhcoin wallet balance` | HD **account** total (use `--single` for one address) |
| `mhcoin wallet send` | Send (change → internal chain) |

## Desktop

- **Create / Restore / Show Recovery Seed / Backup** — as before; restore uses gap-scan
- **Import Private Key (WIF)** — Welcome + Settings (WIF or 64-char hex; same as CLI `import-key`)
- **Show Private Key (WIF)** — Settings, for the **active** address (password + confirm; same as CLI `export-key`)
- **Receive** — list / New address / Use / Copy (external only; change hidden)
- **Overview balance** — whole HD account
- **Show Account xpub** / **Import xpub (watch-only)** in Settings
- Watch-only accounts can view balance/history but cannot send / export WIF

Same data directory as CLI (`~/.mhcoin/<network>/`).
