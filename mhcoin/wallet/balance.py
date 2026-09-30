"""Balance and history — populated in later milestones (UTXO / chain)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class BalanceInfo:
    confirmed: int  # in smallest units
    unconfirmed: int


def get_balance(_address: str) -> BalanceInfo:
    """Milestone 1: no chain yet; balance is always zero."""
    return BalanceInfo(confirmed=0, unconfirmed=0)


def get_history(_address: str) -> list[dict]:
    return []
