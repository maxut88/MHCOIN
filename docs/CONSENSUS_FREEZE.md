# MHCOIN Consensus Freeze (RC1)

After Stage 9, MHCOIN treats consensus-critical code as **frozen for Release Candidate**.

```
                 MHCOIN
                    │
           ┌────────┴────────┐
           │                 │
     Consensus code       Node software
           │                 │
        FREEZE              may evolve
```

## Frozen surface

Any change to the following is a **potential consensus change**, not a routine bugfix:

| Area | Paths / symbols |
|------|-----------------|
| Monetary policy | `MAX_SUPPLY_*`, subsidy, `HALVING_INTERVAL` |
| PoW | `HASH256`, bits/target, genesis PoW |
| Genesis | all `NetworkParams` genesis fields + magic |
| Serialization | tx/block header encoding |
| Validation | `blockchain/validation.py`, script/sig rules |
| UTXO | apply/undo, coinbase maturity policy if consensus |
| Fork choice | cumulative chain work selection |
| Protocol bounds | `PROTOCOL_VERSION` / min / max supported |

Authoritative parameter module: `mhcoin/consensus/params.py`.

Fingerprint of consensus sources (compare across machines):

```bash
PYTHONPATH=. python3 -m mhcoin.audit fingerprint
PYTHONPATH=. python3 -m mhcoin.audit identity
```

Identical trees → identical `fingerprint`. Divergent fingerprint ⇒ do not claim the same RC build.

## Allowed without consensus bump

Node policy / non-consensus evolution (still review carefully):

- Ban scores, rate limits, AddrDB size
- Reconnect intervals, outbound target
- Logging, CLI UX, wallet encryption
- Docs, tests, audit tooling

When in doubt, treat as consensus and bump `PROTOCOL_VERSION` / document in release notes.

## Process

1. Propose change + label `consensus` or `policy`.
2. If consensus: dual review, update `docs/CONSENSUS.md`, bump protocol bounds if peers must diverge from old rules.
3. Re-run `python3 -m mhcoin.audit rc1` and full pytest.
4. Never edit mainnet genesis constants “to fix a bug” after freeze without an explicit hard-fork decision.

**Mainnet remains NOT LAUNCHED** until `docs/MAINNET_CHECKLIST.md` and `docs/RC1_AUDIT.md` are complete.
