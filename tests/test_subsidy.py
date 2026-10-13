from mhcoin.consensus.block_reward import get_block_subsidy
from mhcoin.constants import INITIAL_BLOCK_SUBSIDY, MAX_SUPPLY_SATOSHIS, SATOSHI_PER_COIN


def test_subsidy_halving():
    assert get_block_subsidy(0) == INITIAL_BLOCK_SUBSIDY
    assert get_block_subsidy(209_999) == INITIAL_BLOCK_SUBSIDY
    assert get_block_subsidy(210_000) == INITIAL_BLOCK_SUBSIDY // 2
    assert get_block_subsidy(420_000) == INITIAL_BLOCK_SUBSIDY // 4


def test_total_supply_never_exceeds_cap():
    total = 0
    for era in range(64):
        reward = get_block_subsidy(era * 210_000)
        total += reward * 210_000
        if reward == 0:
            break
    assert total <= MAX_SUPPLY_SATOSHIS
    # Integer halvings truncate; total is just under 21e6 MHC (same effect as BTC-style schedule)
    assert total == 2_099_999_997_690_000
    assert total < 21_000_000 * SATOSHI_PER_COIN
