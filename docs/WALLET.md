# MHCOIN Wallet (Milestone 1)

## Address format

- Bech32 with human-readable part **`mhc`**
- Display form: **`mhc1…`** (witness v0, 20-byte pubkey hash)
- Checksum: Bech32 BCH code (same algorithm family as BIP-173, **different HRP and network** from Bitcoin)

## Key derivation

```
Private key (32 bytes, CSPRNG)
    → secp256k1 public key (compressed, 33 bytes)
    → HASH160(pubkey)
    → Bech32 encode (hrp=mhc, version=0)
    → MHC address
```

Private keys are **never** sent over P2P (P2P not implemented in M1).

## Storage

File: `wallet.json` under the network data directory.

- Private keys stored as **scrypt + AES-GCM** blobs per wallet entry
- Unlock password via CLI prompt or `MHCOIN_WALLET_PASSWORD` (automation only)

## CLI

| Command | Description |
|---------|-------------|
| `mhcoin wallet create` | New key + encrypted save |
| `mhcoin wallet address` | Default address |
| `mhcoin wallet balance` | Placeholder (0) until UTXO set exists |
| `mhcoin wallet history` | Placeholder until chain sync |
