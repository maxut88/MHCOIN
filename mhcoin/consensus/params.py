"""MHCOIN network parameters (Stage 9).

Consensus-critical values must match across all honest nodes of a network.
Policy/node-safety limits live elsewhere (network.constants) and are NOT
consensus-critical unless explicitly marked below.
"""

from __future__ import annotations

from dataclasses import dataclass

# Network identity maps live here (not in network.constants) to avoid
# import cycles: constants → params → network → constants.

NETWORK_MAGIC: dict[str, bytes] = {
    "mainnet": bytes.fromhex("4D48434E"),  # MHCN
    "testnet": bytes.fromhex("4D48544E"),  # MHTN
    "regtest": bytes.fromhex("4D48524E"),  # MHRN
    "localnet": bytes.fromhex("4D484C4E"),  # MHLN
}

DEFAULT_P2P_PORT: dict[str, int] = {
    "mainnet": 8333,
    "testnet": 18333,
    "regtest": 18444,
    "localnet": 18444,
}


# ---------------------------------------------------------------------------
# Shared monetary / PoW model (consensus-critical for ALL networks)
# ---------------------------------------------------------------------------

MAX_SUPPLY_COINS = 21_000_000
DECIMALS = 8
SATOSHI_PER_COIN = 10**DECIMALS
MAX_SUPPLY_SATOSHIS = MAX_SUPPLY_COINS * SATOSHI_PER_COIN

INITIAL_BLOCK_REWARD_COINS = 50
INITIAL_BLOCK_SUBSIDY = INITIAL_BLOCK_REWARD_COINS * SATOSHI_PER_COIN
HALVING_INTERVAL = 210_000
TARGET_BLOCK_TIME_SECONDS = 600

# PoW: double SHA-256 (HASH256) of the block header; UTXO accounting model
POW_ALGORITHM = "HASH256"
LEDGER_MODEL = "UTXO"

TX_VERSION = 1
BLOCK_VERSION = 1
SUPPORTED_BLOCK_VERSIONS = frozenset({BLOCK_VERSION})

MAX_BLOCK_SIZE = 1_000_000
MAX_TXS_PER_BLOCK = 10_000
MAX_FUTURE_BLOCK_TIME = 2 * 60 * 60

# ---------------------------------------------------------------------------
# Protocol versioning (wire compatibility — consensus-adjacent)
# ---------------------------------------------------------------------------

PROTOCOL_VERSION = 1
MIN_SUPPORTED_PROTOCOL_VERSION = 1
MAX_SUPPORTED_PROTOCOL_VERSION = 1
SOFTWARE_VERSION = "0.3.0"
USER_AGENT = f"/MHCOIN:{SOFTWARE_VERSION}/"

# Compatibility rule (Stage 9):
# - Peers with protocol_version < MIN_SUPPORTED or > MAX_SUPPORTED are rejected.
# - Raising MIN_SUPPORTED is a deliberate network upgrade (document in release notes).
# - Changing consensus rules without coordinated MIN/PROTOCOL bump risks a split.


@dataclass(frozen=True)
class NetworkParams:
    """Per-network identity. Cross-network peering must be impossible."""

    name: str
    magic: bytes
    default_port: int
    address_hrp: str
    # Genesis (consensus-critical; frozen after freeze)
    genesis_timestamp: int
    genesis_bits: int
    genesis_nonce: int
    genesis_pubkey_hash: bytes
    genesis_coinbase_extra: bytes
    genesis_hash_hex: str
    genesis_merkle_hex: str
    # Allow CLI `blockchain init` to mine a *different* wallet genesis?
    # Never for mainnet/testnet. Only regtest/localnet tooling.
    allow_custom_genesis_bootstrap: bool

    @property
    def genesis_hash(self) -> bytes:
        return bytes.fromhex(self.genesis_hash_hex)

    @property
    def genesis_merkle(self) -> bytes:
        return bytes.fromhex(self.genesis_merkle_hex)


# Burn/dev commitment used by frozen genesis coinbases (not a hot wallet).
_GENESIS_BURN_PKH = bytes.fromhex("00" * 19 + "01")

# Regtest / localnet — easy PoW, shared frozen genesis (existing Stage 2+)
_REGTEST = NetworkParams(
    name="regtest",
    magic=NETWORK_MAGIC["regtest"],
    default_port=DEFAULT_P2P_PORT["regtest"],
    address_hrp="mhc",
    genesis_timestamp=1_700_000_000,
    genesis_bits=0x1F0FFFFF,
    genesis_nonce=671,
    genesis_pubkey_hash=_GENESIS_BURN_PKH,
    genesis_coinbase_extra=b"MHCOIN/regtest genesis",
    genesis_hash_hex="d8d1372638973f4a52b2aca887724771fe7df8fc3bf2d2760145ff3fdfcd0900",
    genesis_merkle_hex="61550e1bd87b0915bd24057e9f8bc8835d9b4a86221b678fc211b65e4a5d83a4",
    allow_custom_genesis_bootstrap=True,
)

_LOCALNET = NetworkParams(
    name="localnet",
    magic=NETWORK_MAGIC["localnet"],
    default_port=DEFAULT_P2P_PORT["localnet"],
    address_hrp="mhc",
    genesis_timestamp=_REGTEST.genesis_timestamp,
    genesis_bits=_REGTEST.genesis_bits,
    genesis_nonce=_REGTEST.genesis_nonce,
    genesis_pubkey_hash=_REGTEST.genesis_pubkey_hash,
    genesis_coinbase_extra=_REGTEST.genesis_coinbase_extra,
    genesis_hash_hex=_REGTEST.genesis_hash_hex,
    genesis_merkle_hex=_REGTEST.genesis_merkle_hex,
    allow_custom_genesis_bootstrap=True,
)

# Testnet — separate magic/port/genesis (same monetary policy)
_TESTNET = NetworkParams(
    name="testnet",
    magic=NETWORK_MAGIC["testnet"],
    default_port=DEFAULT_P2P_PORT["testnet"],
    address_hrp="mht",
    genesis_timestamp=1_700_000_000,
    genesis_bits=0x1F0FFFFF,
    genesis_nonce=13_295,
    genesis_pubkey_hash=_GENESIS_BURN_PKH,
    genesis_coinbase_extra=b"MHCOIN/testnet genesis",
    genesis_hash_hex="93e55291d3160798920ef6826175e30d6133b1c96085b83e2155a24df7cb0100",
    genesis_merkle_hex="d000b6a8dcb9e9f34ae4cdc1485a9c463468238de35ef6899cf557f5cbca6439",
    allow_custom_genesis_bootstrap=False,
)

# Mainnet — production genesis (frozen). Do NOT regenerate at node start.
# Genesis bits 0x1e0fffff: harder than regtest; subsequent retarget is future work.
_MAINNET = NetworkParams(
    name="mainnet",
    magic=NETWORK_MAGIC["mainnet"],
    default_port=DEFAULT_P2P_PORT["mainnet"],
    address_hrp="mhc",
    genesis_timestamp=1_735_689_600,  # 2025-01-01 00:00:00 UTC
    genesis_bits=0x1E0FFFFF,
    genesis_nonce=646_812,
    genesis_pubkey_hash=_GENESIS_BURN_PKH,
    genesis_coinbase_extra=b"MHCOIN/mainnet genesis",
    genesis_hash_hex="62e078a7ca0dfeac4c6451e5852d5ec714c7aacbff4e48f1d8cc8aa9560a0000",
    genesis_merkle_hex="8ea23b65a3da9651b6e03d6f2c2398711d7568b7a942f15fd96397069053c954",
    allow_custom_genesis_bootstrap=False,
)

NETWORKS: dict[str, NetworkParams] = {
    "mainnet": _MAINNET,
    "testnet": _TESTNET,
    "regtest": _REGTEST,
    "localnet": _LOCALNET,
}


def get_network_params(name: str) -> NetworkParams:
    key = name.strip().lower()
    if key not in NETWORKS:
        raise ValueError(f"unknown network {name!r}; expected one of {sorted(NETWORKS)}")
    return NETWORKS[key]


def list_networks() -> list[str]:
    return sorted(NETWORKS)


# Consensus-critical field names (for docs / checklist)
CONSENSUS_CRITICAL_SHARED = (
    "MAX_SUPPLY_COINS",
    "DECIMALS",
    "INITIAL_BLOCK_SUBSIDY",
    "HALVING_INTERVAL",
    "TARGET_BLOCK_TIME_SECONDS",
    "POW_ALGORITHM",
    "LEDGER_MODEL",
    "BLOCK_VERSION",
    "TX_VERSION",
    "MAX_BLOCK_SIZE",
    "MAX_TXS_PER_BLOCK",
)

CONSENSUS_CRITICAL_PER_NETWORK = (
    "magic",
    "genesis_timestamp",
    "genesis_bits",
    "genesis_nonce",
    "genesis_pubkey_hash",
    "genesis_coinbase_extra",
    "genesis_hash_hex",
    "genesis_merkle_hex",
)
