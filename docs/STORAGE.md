# MHCOIN datadir storage

Layout under `--data-dir` (per network):

```text
<data-dir>/
  peers.dat          # peer address book (JSON); migrates legacy peers.sqlite
  bans.json          # misbehavior / bans
  chain.sqlite       # block index + undo + tip meta (schema_version=3)
  blocks/
    blk00000.dat     # append-only raw blocks (Bitcoin-style)
    blk00001.dat     # …
  chainstate/        # LMDB UTXO set (Bitcoin chainstate role); migrates legacy utxo.sqlite
  wallet.json        # wallet (unchanged by chain storage migrations)
```

## Schema versions (`chain.sqlite` meta `schema_version`)

| Version | Meaning |
|---------|---------|
| 1 | Legacy height-keyed `blocks` table |
| 2 | `block_index` + `block_undo`; raw block bytes embedded in sqlite |
| 3 | Same index; raw bytes in `blocks/blk*.dat` via `file_id` / `data_pos` / `data_len` |

Opening a datadir runs idempotent migrations (v1→index, embedded raw→flat files,
`utxo.sqlite`→`chainstate/`).

## UTXO (`chainstate/`)

LMDB keys (same layout Bitcoin uses conceptually for coins):

| Key | Value |
|-----|-------|
| `C` + txid(32) + vout_u32be | value_u64be \| height_u32be \| coinbase_u8 \| script_pubkey |
| `M` + utf-8 meta key | utf-8 meta value |

LMDB is used instead of LevelDB so Desktop builds ship cleanly on macOS/Windows
(binary wheels). Role matches Bitcoin `chainstate/`: durable UTXO KV + cache.

In-memory cache mirrors the set for fast validation at current scale; durable
writes use atomic LMDB transactions (per block apply / disconnect).

## Readers

- `Blockchain` — full node read/write (index + flat files + chainstate).
- `ReadOnlyChain` — WAL `mode=ro` on `chain.sqlite` + read-only `blocks/`
  (Desktop history / explorer); must not take the node disk lock.
