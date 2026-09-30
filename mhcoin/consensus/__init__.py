"""Consensus package.

Import submodules directly (e.g. ``mhcoin.consensus.params``) to avoid
circular imports with ``mhcoin.constants``. Lazy re-exports below.
"""

from __future__ import annotations

__all__ = [
    "get_block_subsidy",
    "get_chain_work",
    "select_best_chain",
    "work_for_bits",
    "bits_to_target",
    "hash_meets_target",
    "mine_block",
    "verify_proof_of_work",
    "get_network_params",
    "PROTOCOL_VERSION",
    "SOFTWARE_VERSION",
]


def __getattr__(name: str):
    if name == "get_block_subsidy":
        from mhcoin.consensus.block_reward import get_block_subsidy

        return get_block_subsidy
    if name in ("get_chain_work", "select_best_chain", "work_for_bits"):
        from mhcoin.consensus import chain_work

        return getattr(chain_work, name)
    if name in ("bits_to_target", "hash_meets_target"):
        from mhcoin.consensus import difficulty

        return getattr(difficulty, name)
    if name in ("mine_block", "verify_proof_of_work"):
        from mhcoin.consensus import proof_of_work

        return getattr(proof_of_work, name)
    if name in ("get_network_params", "PROTOCOL_VERSION", "SOFTWARE_VERSION"):
        from mhcoin.consensus import params

        return getattr(params, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
