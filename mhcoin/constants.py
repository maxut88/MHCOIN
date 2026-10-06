"""Consensus and network constants.

Monetary policy and PoW model are defined in mhcoin.consensus.params.
This module re-exports for backward compatibility with existing imports.
"""

from mhcoin.consensus.params import (  # noqa: F401
    BLOCK_VERSION,
    DECIMALS,
    HALVING_INTERVAL,
    INITIAL_BLOCK_REWARD_COINS,
    INITIAL_BLOCK_SUBSIDY,
    LEDGER_MODEL,
    MAX_BLOCK_SIZE,
    MAX_FUTURE_BLOCK_TIME,
    MAX_SUPPLY_COINS,
    MAX_SUPPLY_SATOSHIS,
    MAX_TXS_PER_BLOCK,
    POW_ALGORITHM,
    PROTOCOL_VERSION,
    SATOSHI_PER_COIN,
    SOFTWARE_VERSION,
    SUPPORTED_BLOCK_VERSIONS,
    TARGET_BLOCK_TIME_SECONDS,
    TX_VERSION,
    USER_AGENT,
    get_network_params,
)

DEFAULT_ADDRESS_HRP = "mhc"
WITNESS_VERSION_PUBKEY_HASH = 0

# Coinbase marker
NULL_TXID = b"\x00" * 32
COINBASE_VOUT = 0xFFFFFFFF
MAX_SEQUENCE = 0xFFFFFFFF

# ScriptPubKey: version byte + 20-byte pubkey hash
SCRIPT_P2PKH_VERSION = 0x00

# Signing
SIGHASH_ALL = 0x01

# Regtest aliases (legacy imports)
REGTEST_NBITS = get_network_params("regtest").genesis_bits
REGTEST_GENESIS_TIMESTAMP = get_network_params("regtest").genesis_timestamp
REGTEST_GENESIS_PUBKEY_HASH = get_network_params("regtest").genesis_pubkey_hash
REGTEST_GENESIS_NONCE = get_network_params("regtest").genesis_nonce
REGTEST_GENESIS_HASH_HEX = get_network_params("regtest").genesis_hash_hex
REGTEST_GENESIS_MERKLE_HEX = get_network_params("regtest").genesis_merkle_hex

# Mainnet frozen aliases
MAINNET_NBITS = get_network_params("mainnet").genesis_bits
MAINNET_GENESIS_TIMESTAMP = get_network_params("mainnet").genesis_timestamp
MAINNET_GENESIS_HASH_HEX = get_network_params("mainnet").genesis_hash_hex
MAINNET_GENESIS_MERKLE_HEX = get_network_params("mainnet").genesis_merkle_hex
MAINNET_GENESIS_NONCE = get_network_params("mainnet").genesis_nonce

# Default fee for wallet send (policy — not consensus-critical)
DEFAULT_FEE_SATOSHIS = 1000

# Network maps (authoritative in mhcoin.consensus.params)
from mhcoin.consensus.params import (  # noqa: E402
    DEFAULT_P2P_PORT,
    NETWORK_MAGIC,
)
