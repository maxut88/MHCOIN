# MHCOIN — User FAQ (upgrade · backup · mining · peers)

One page for Desktop / CLI operators on **mainnet**.

Current software: see [GitHub Releases](https://github.com/maxut88/MHCOIN/releases/latest).

---

## Upgrade (e.g. 0.3.7.3 → 0.4.1.x)

1. **Back up** `wallet.json` first (copy the file, or Desktop → Settings → Download wallet backup).
2. Install the new Desktop build (same OS).
3. Open the **same** data folder (`~/.mhcoin/mainnet/` — Windows: `%USERPROFILE%\.mhcoin\mainnet\`).
4. Storage migrates automatically on open:
   - `utxo.sqlite` → `chainstate/` (LMDB)
   - block bytes → `blocks/blk*.dat`
   - peers → `peers.dat`
5. Unlock with the **same password**. BIP39 seed (if the wallet was created/restored from words) stays encrypted in `wallet.json`.

Do **not** copy another machine’s chain database — each node validates from genesis.

---

## Backup & recovery

| What | How |
|------|-----|
| Encrypted wallet file | Settings → **Download wallet backup** (or copy `wallet.json`) |
| BIP39 seed | Create shows words **once** · Settings → **Show Recovery Seed** (password) |
| Restore on a new PC | Welcome → **Restore from Seed** · or `mhcoin wallet restore --mnemonic "…"` |
| Password | Never stored in the backup — keep it separately |

Seed phrase = full control of funds. Store offline. Anyone with seed + empty passphrase can restore the key.

Legacy wallets (pre-BIP39 key import) have **no** mnemonic — only the encrypted key / backup file + password.

---

## Mining FAQ

**Desktop:** Mining tab → payout `mhc1…` → **Start mining**.  
Log should say `CPU single-lane PoW (0.3.7.3-style)` on current builds.

**CLI:**

```bash
mhcoin mining start --network mainnet --address mhc1…
```

Rules:

- One writer per data folder — do **not** run Desktop mining and CLI mining on the same `~/.mhcoin/mainnet` at once.
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

- New wallets: BIP39 + path **`m/84'/0'/0'/0/0`** → one receive `mhc1…`.
- Desktop: Create / Restore / Show seed / file backup — **done**.
- Multiple receive addresses (`…/0/1`, `…/0/2`, …) — **not in UI yet** (single active address).

---

## See also

- [WALLET.md](WALLET.md) · [STORAGE.md](STORAGE.md) · [DISCOVERY.md](DISCOVERY.md) · [DESKTOP.md](DESKTOP.md) · [USER_QUICKSTART.md](USER_QUICKSTART.md)
