# MHCOIN Transactions (Milestone 2 — STEP 1)

## Units

All amounts are **integers** in the smallest unit:

```
1 MHC = 100_000_000 base units
1.25 MHC = 125_000_000 base units
```

Floating point is never used for consensus or wallet money math.

## Structure

```
Transaction
  version:   uint32
  inputs:    TxInput[]
  outputs:   TxOutput[]
  locktime:  uint32

TxInput
  prev_txid:   32 bytes
  prev_vout:   uint32
  script_sig:  bytes
  sequence:    uint32

TxOutput
  value:          uint64 (base units)
  script_pubkey:  bytes
```

P2PKH `script_pubkey` (current local protocol):

```
0x00 || pubkey_hash20
```

## Canonical serialization

Little-endian integers. Compact-size varints for lengths and counts.

```
version | varint(n_in) | inputs... | varint(n_out) | outputs... | locktime
```

Public API:

```python
from mhcoin.transaction import serialize_transaction, deserialize_transaction

raw = serialize_transaction(tx)
tx2 = deserialize_transaction(raw)
```

Rules:

- Same logical transaction → identical bytes
- No JSON / pickle / dict order
- Trailing bytes rejected on deserialize

## TXID

```
TXID = HASH256(serialize_transaction(tx))
     = SHA256(SHA256(canonical_bytes))
```

Internal: 32 bytes. External: lowercase hex via `txid_hex()`.

Changing any committed field (version, inputs, outputs, locktime, scripts) changes the TXID.

## Not in STEP 1

Signing / SIGHASH, UTXO spend checks, mempool, and blocks are later steps.
Existing code for those modules must not be treated as finalized protocol until their steps pass review.
