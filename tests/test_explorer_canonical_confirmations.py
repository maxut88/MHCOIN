"""Explorer must not treat side-chain blocks as confirmed."""

from __future__ import annotations

from pathlib import Path

import pytest

from mhcoin.blockchain.chain import STATUS_ACTIVE, STATUS_SIDE
from mhcoin.blockchain.readonly_chain import ReadOnlyChain
from mhcoin.explorer import decode as D

MAINNET = Path("/data/MHCOIN/mainnet/node-8333")
DISPUTED = "73ec17ed6f94f0660c27d6fdd235ad77ae77729222da7ee20a94563d29000000"
CANON_1480 = "4013de7c8ba84a691dc0623457e1f31a5ae789aa4e6b411cef99890440000000"


def _hx(block) -> str:
    h = block.block_hash
    if callable(h):
        h = h()
    return h.hex()


@pytest.fixture(scope="module")
def chain() -> ReadOnlyChain:
    if not (MAINNET / "chain.sqlite").is_file():
        pytest.skip("mainnet chain.sqlite not available")
    return ReadOnlyChain(MAINNET)


def test_side_block_confirmations_zero(chain: ReadOnlyChain) -> None:
    tip = int(chain.height)
    disputed = chain.get_block_by_hash(bytes.fromhex(DISPUTED))
    assert disputed is not None
    assert chain.get_index_status(bytes.fromhex(DISPUTED)) == STATUS_SIDE
    assert D.confirmations(tip, 1480, on_best_chain=False) == 0
    assert D.block_on_best_chain(chain, disputed, 1480) is False
    summary = D.summarize_block(chain, disputed, height=1480, tip=tip)
    assert summary["confirmations"] == 0
    assert summary["on_best_chain"] is False
    assert summary["chain_status"] == "side"
    assert summary["next"] is None


def test_canonical_block_keeps_positive_confirmations(chain: ReadOnlyChain) -> None:
    tip = int(chain.height)
    canon = chain.get_block_by_height(1480)
    assert canon is not None
    assert _hx(canon) == CANON_1480
    assert chain.get_index_status(bytes.fromhex(CANON_1480)) == STATUS_ACTIVE
    summary = D.summarize_block(chain, canon, height=1480, tip=tip)
    assert summary["confirmations"] == tip - 1480 + 1
    assert summary["on_best_chain"] is True
    assert summary["chain_status"] == "active"
    if tip > 1480:
        assert summary["next"] == _hx(chain.get_block_by_height(1481))
