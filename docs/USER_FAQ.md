# MHCOIN — User FAQ (upgrade · backup · mining · peers)

One page for Desktop / CLI operators on **mainnet**.

Current software: see [GitHub Releases](https://github.com/maxut88/MHCOIN/releases/latest).

---

## Upgrade (e.g. → 0.4.2.0)

**0.4.2.0 is a hard fork** (protocol **v2**, epoch difficulty from height **1500**). Every node and miner must upgrade **before** height 1500.

1. **Back up** `wallet.json` first (copy the file, or Desktop → Settings → Download wallet backup).
2. Install the new Desktop build (same OS), or `git pull` + `pip install -e .` on `main`.
3. Open the **same** data folder (`~/.mhcoin/mainnet/` — Windows: `%USERPROFILE%\.mhcoin\mainnet\`).
4. Unlock with the **same password**. Confirm the node status shows **protocol 2**.
5. Older 0.4.1.x builds cannot peer with 0.4.2.0.

Do **not** copy another machine’s chain database — each node validates from genesis.

---

## Backup & recovery

| What | How |
|------|-----|
| Encrypted wallet file | Settings → **Download wallet backup** (or copy `wallet.json`) |
| BIP39 seed | Create shows words **once** · Settings → **Show Recovery Seed** (password) |
| Private key (WIF) | Welcome / Settings → **Import Private Key (WIF)** · Settings → **Show Private Key (WIF)** for the active address |
| Restore on a new PC | Welcome → **Restore from Seed** · or `mhcoin wallet restore --mnemonic "…"` |
| Password | Never stored in the backup — keep it separately |

Seed phrase = full control of funds. Store offline. Anyone with seed + empty passphrase can restore the key.

**WIF** = one address key (Electrum/Sparrow-style). Import accepts compressed WIF or 64-char hex. Show WIF needs the wallet password and only reveals the **active** address. Anyone with the WIF can spend that address.

Legacy / imported-key wallets have **no** mnemonic — use WIF export or the encrypted backup file + password.

---

## Mining FAQ

**Desktop:** Mining tab → payout `mhc1…` → **Start mining**.  
Log should show live H/s (single-lane PoW). Newer builds use a faster in-place HASH256 loop.

**CLI (solo):**

```bash
mhcoin mining start --network mainnet --address mhc1…
```

**CLI (pool)** — see [POOL.md](POOL.md):

```bash
mhcoin mining pool-start --url POOL_HOST:3333 --address mhc1…
```

Rules:

- One writer per data folder — do **not** run Desktop mining and CLI solo mining on the same `~/.mhcoin/mainnet` at once.
- Pool mining does not write your local chain (safe alongside Desktop).
- Hashrate in the Mining tab / log is the local estimate (H/s).
- Explorer **Peers → H/S** comes from P2P `STATUS` (advisory, ~10 s). Prefer the Mining tab for your own rate.
- Mac vs Windows on the same hardware often differs (~40–65%) — expected.
- Block finds are probabilistic (~10 min target on average for the **network**, not per miner).

---

## Peers / bootstrap

Fresh nodes need **at least one** reachable peer:

1. Built-in seed(s) in `mhcoin/network/seeds.py` (first contact only — not consensus).
2. Then `peers.dat` + ADDR gossip grow the mesh.
3. Overrides:

```bash
export MHCOIN_CONNECT=host1:8333,host2:8333
# optional DNS names when published:
export MHCOIN_DNS_SEEDS=seed.example.com
```

Today mainnet ships with a **small** hardcoded seed list. More independent seeds / DNS names should be added as operators come online. If the only seed is offline and you have an empty `peers.dat`, set `MHCOIN_CONNECT` to a known peer.

---

## Wallet addresses (today)

- New wallets: BIP39 + BIP84 account `m/84'/0'/0'` → receive `…/0/n`, change `…/1/n`.
- Desktop **Receive** → **New address** / **Use** / **Copy**; Overview balance is **account-wide**.
- **Restore** gap-scans used addresses (default gap 20). Send uses the **change** chain.
- **xpub** export/import for watch-only. Imported single keys cannot derive more addresses.
- Create / Restore / Show seed / file backup — **done**.

---

## See also

- [WALLET.md](WALLET.md) · [STORAGE.md](STORAGE.md) · [DISCOVERY.md](DISCOVERY.md) · [DESKTOP.md](DESKTOP.md) · [USER_QUICKSTART.md](USER_QUICKSTART.md)
