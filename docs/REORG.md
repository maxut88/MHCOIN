# MHCOIN Stage 6 — Reorg & Fork Choice

## Overview

MHCOIN stores a **block tree**, not a single linked list. Every fully validated
block may be retained. Exactly one path from genesis is the **active chain**.
Selection is by **highest cumulative proof-of-work**, never by height alone.

```text
GENESIS
   |
   A
   |
   B
  / \
 C   C2
 |    |
 D    D2
 |
 E

Active (example): GENESIS→A→B→C→D→E
Side branch:      GENESIS→A→B→C2→D2
Common ancestor:  B
```

## Concepts

| Concept | Meaning |
|---------|---------|
| Block storage | Index in `block_index`; raw bytes in `blocks/blk*.dat` |
| Active chain | Genesis → tip selected by cumulative work |
| Active UTXO | UTXO set for the active chain only |
| Chain work | `parent_work + work_for_bits(block.bits)` (integer) |
| Mempool | Unconfirmed txs valid under the **active** UTXO |
| Orphans | Bounded pool of blocks whose parent is not yet known |

## Fork-choice rule

```text
if candidate_chain_work > active_chain_work:
    reorganize to candidate
elif candidate_chain_work < active_chain_work:
    keep active; store candidate as side chain
else:  # equal work
    keep active tip   # deterministic: avoid unnecessary reorgs
```

**Height is not authoritative.** A shorter chain with substantially more work
(harder `nBits`) wins over a longer low-work chain.

Peer-advertised `start_height` / tip claims are **hints only**.

## Block acceptance pipeline

```text
receive block
  → decode / structural checks
  → already known? → ignore duplicate
  → parent unknown? → orphan pool (+ GETDATA parent)
  → validate header + PoW + txs against **parent** UTXO state
  → store in block_index (side)
  → if extends tip → connect + undo + activate
  → else if work > active → atomic reorg
  → else keep as side chain
```

Side-chain blocks are validated against their parent chain state, not blindly
against the current tip UTXO.

## Common ancestor

`find_common_ancestor(tip_a, tip_b)` walks both tips to equal height, then steps
back until hashes match. Works for unequal heights.

## Reorg algorithm

1. Find common ancestor.
2. Disconnect old blocks (newest first) using **undo** records → restore UTXO.
3. Connect new blocks (oldest first) → apply UTXO + store undo.
4. Update tip / height / work / active markers.
5. Restore mempool candidates from disconnected non-coinbase txs.
6. Persist; on failure roll back to the previous active state.

Safety policy: `MAX_REORG_DEPTH` (default 100) — not consensus; oversized reorgs
are refused and the old tip remains.

## UTXO undo

Each connected active block has a versioned JSON undo blob:

- `spent`: full prior UTXO entries restored on disconnect
- `created`: outpoint keys removed on disconnect

Never reconstruct undo by guessing from raw txs alone at disconnect time.

## Mempool after reorg

1. Collect non-coinbase txs from disconnected blocks.
2. After new tip is active, re-validate each candidate.
3. Re-add if still valid; skip if confirmed on new chain, conflicting, or invalid.
4. **Coinbases never enter the mempool.**

## Orphans

| Limit | Default |
|-------|---------|
| `MAX_ORPHAN_BLOCKS` | 64 |
| `MAX_ORPHAN_BYTES` | 2_000_000 |

When a parent arrives, dependent orphans are retried.

## P2P / sync

- `INV(BLOCK)` / `GETDATA` / `BLOCK` may deliver side-chain blocks.
- `GETHEADERS` / `HEADERS` may describe a competing branch; first header need
  not extend the active tip if its parent is already known.
- After a successful reorg, the new tip is announced via INV (deduplicated).

## Persistence / restart

Schema v3: `block_index` (with `file_id` / `data_pos` / `data_len`),
`block_undo`, tip meta, and flat `blocks/blk*.dat`. Legacy height-keyed
`blocks` tables and sqlite-embedded `raw` blobs are migrated on open.
On UTXO/tip mismatch at startup, UTXO is rebuilt from the active chain.
UTXO lives in LMDB ``chainstate/`` (legacy ``utxo.sqlite`` is migrated).

## CLI

```bash
mhcoin node info --data-dir ...
mhcoin blockchain info --data-dir ...
mhcoin blockchain forks --data-dir ...
mhcoin block get <hash>   # works for side-chain blocks
```

## Fingerprints

Deterministic `utxo_fingerprint` / chain-state summary support multi-node
convergence checks (compare tips, work, and UTXO hash — not SQLite file bytes).
