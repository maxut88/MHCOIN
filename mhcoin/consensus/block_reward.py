"""Block subsidy and halving (integer satoshis only)."""

from __future__ import annotations

from mhcoin.constants import HALVING_INTERVAL, INITIAL_BLOCK_SUBSIDY, MAX_SUPPLY_SATOSHIS


def get_block_subsidy(height: int) -> int:
    if height < 0:
        raise ValueError("negative height")
    halvings = height // HALVING_INTERVAL
    if halvings >= 64:
        return 0
    subsidy = INITIAL_BLOCK_SUBSIDY >> halvings
    return subsidy


def assert_supply_cap(total_issued: int) -> None:
    if total_issued > MAX_SUPPLY_SATOSHIS:
        raise ValueError("supply exceeds 21,000,000 MHC")
