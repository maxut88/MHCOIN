# Explorer — Coinbase maturity UX (prepared, not deployed)

Helper added in RC worktree: `mhcoin.explorer.decode.coinbase_maturity_label`.

| Label | Meaning |
|-------|---------|
| `maturity_inactive` | Mainnet activation still 0 — do not show false “immature” under consensus |
| `immature` | Activation live; CB not spendable at tip+1 |
| `mature` | Activation live; CB spendable at tip+1 |

**Next (needs owner approval):** wire label into address/UTXO and coinbase TX templates in `service.py`. Until then, explorers show confirmations only.

Do **not** deploy to production Explorer without approval.
