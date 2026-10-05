# MHCOIN Wallet

## Address format

- Bech32 with human-readable part **`mhc`**
- Display form: **`mhc1…`** (witness v0, 20-byte pubkey hash)
- Checksum: Bech32 BCH code (same algorithm family as BIP-173, **different HRP and network** from Bitcoin)

## Key derivation (Bitcoin-style)

New wallets use **BIP39** + **BIP32**:

```
BIP39 mnemonic (12 or 24 English words)
    → PBKDF2 seed (64 bytes)
    → BIP32 path m/84'/0'/0'/0/n   (n = 0, 1, 2, … external receive)
    → secp256k1 private key (32 bytes)
    → compressed public key (33 bytes)
    → HASH160(pubkey)
    → Bech32 encode (hrp=mhc, version=0)
    → MHC address
```

The path follows BIP84’s external receive chain so recovery matches common Bitcoin HD
layouts; MHCOIN still encodes **`mhc1…`** addresses. Desktop **Receive → New address**
derives the next unused `n` under the same account (`account_id` in `wallet.json`).

You can also **import** a raw key:

- 64-char hex, or
- Bitcoin-compatible **WIF** (compressed, version `0x80`)

## Storage

File: `wallet.json` under the network data directory (`~/.mhcoin/<network>/`).

- Private keys stored as **scrypt + AES-GCM** blobs per wallet entry
- BIP39 mnemonic (when created/restored from seed) is stored **encrypted** the same way
- Unlock password via CLI prompt or `MHCOIN_WALLET_PASSWORD` (automation only)
- Desktop **Backup** still copies this encrypted file — keep the password separately

Legacy wallets created before BIP39 remain unlockable; they simply have no mnemonic.

## CLI

| Command | Description |
|---------|-------------|
| `mhcoin wallet create` | New HD wallet — prints BIP39 seed **once** + address |
| `mhcoin wallet create --words 24` | 24-word mnemonic |
| `mhcoin wallet restore` | Restore from BIP39 words |
| `mhcoin wallet import-key` | Import WIF or hex private key |
| `mhcoin wallet export-seed` | Show stored mnemonic (password required) |
| `mhcoin wallet export-key` | Export active key as compressed WIF |
| `mhcoin wallet address` | Default address |
| `mhcoin wallet balance` | Confirmed balance from UTXO |
| `mhcoin wallet history` | Placeholder / chain-dependent |

### Examples

```bash
# Create (write the seed on paper, then clear the screen)
mhcoin wallet create --network mainnet

# Restore on another machine
mhcoin wallet restore --network mainnet --mnemonic "word1 word2 ... word12"

# Import a WIF/hex key
mhcoin wallet import-key --network mainnet --key 'Kx...'
```

## Desktop

Welcome / Settings:

- **Create New Wallet** — BIP39 HD wallet; shows recovery seed once (+ password details)
- **Restore from Seed** — enter 12/24 BIP39 words + password
- **Show Recovery Seed** (Settings) — reveal stored mnemonic (password required)
- Unlock / Backup still use encrypted `wallet.json` + password

**Receive** tab:

- Lists HD receive addresses for the active account
- **New address** — next `m/84'/0'/0'/0/n` (wallet must be unlocked; seed required)
- **Use** — make that address active for balance / mining / send (keeps session unlock)

Same data directory as CLI (`~/.mhcoin/<network>/`).
