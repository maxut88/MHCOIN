"""Legacy address balance stub.

Desktop/CLI balances use the live UTXO set via the wallet/controller paths.
This module remains for a minimal BalanceInfo shape and tests.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class BalanceInfo:
    confirmed: int  # in smallest units
    unconfirmed: int


def get_balance(_address: str) -> BalanceInfo:
    """Stub: returns zero. Prefer wallet UTXO balance APIs."""
    return BalanceInfo(confirmed=0, unconfirmed=0)


def get_history(_address: str) -> list[dict]:
    return []
