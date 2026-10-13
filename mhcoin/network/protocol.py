"""P2P protocol surface (message envelope).

Later stages add VERSION/VERACK payloads and peer I/O.
"""

from mhcoin.network.constants import (
    COMMAND_SIZE,
    DEFAULT_P2P_PORT,
    HEADER_SIZE,
    MAX_PAYLOAD_SIZE,
    NETWORK_MAGIC,
    PROTOCOL_VERSION,
    USER_AGENT,
)
from mhcoin.network.serialization import (
    NetMessage,
    ProtocolError,
    decode_envelope,
    encode_envelope,
    magic_for_network,
    payload_checksum,
    try_decode_envelope,
)

__all__ = [
    "COMMAND_SIZE",
    "DEFAULT_P2P_PORT",
    "HEADER_SIZE",
    "MAX_PAYLOAD_SIZE",
    "NETWORK_MAGIC",
    "PROTOCOL_VERSION",
    "USER_AGENT",
    "NetMessage",
    "ProtocolError",
    "decode_envelope",
    "encode_envelope",
    "magic_for_network",
    "payload_checksum",
    "try_decode_envelope",
]
