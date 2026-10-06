"""Application payload codecs. Envelope lives in serialization.py."""

from __future__ import annotations

from dataclasses import dataclass

from mhcoin.network.constants import (
    INV_TYPE_BLOCK,
    INV_TYPE_TX,
    MAX_ADDR_ENTRIES,
    MAX_ADDR_HOST_LEN,
    MAX_BLOCK_SIZE,
    MAX_GETDATA_ITEMS,
    MAX_HEADERS,
    MAX_INV_ITEMS,
    MAX_LOCATOR_HASHES,
    MAX_NETWORK_NAME_LEN,
    MAX_SOFTWARE_VERSION_LEN,
    MAX_TX_SIZE,
    NETWORK_MAGIC,
    ZERO_HASH,
)
from mhcoin.network.serialization import (  # noqa: F401
    NetMessage,
    ProtocolError,
    encode_envelope as encode_message,
    try_decode_envelope as try_decode_message,
)
from mhcoin.blockchain.block import Block, BlockHeader
from mhcoin.transaction.serialization import (
    Reader,
    SerializationError,
    write_hash32,
    write_i32,
    write_i64,
    write_u16,
    write_u32,
    write_u64,
    write_u8,
    write_varint,
)
from mhcoin.transaction.transaction import Transaction

# Backward-compatible aliases
INV_TX = INV_TYPE_TX
INV_BLOCK = INV_TYPE_BLOCK

_ALLOWED_INV_TYPES = frozenset({INV_TYPE_TX, INV_TYPE_BLOCK})


@dataclass(frozen=True)
class InventoryVector:
    type: int
    hash: bytes  # 32-byte object id (txid for INV_TYPE_TX)

    def __post_init__(self) -> None:
        if not isinstance(self.hash, (bytes, bytearray)) or len(self.hash) != 32:
            raise ProtocolError("inventory hash must be 32 bytes")


@dataclass(frozen=True)
class VersionPayload:
    protocol_version: int
    services: int
    timestamp: int
    nonce: int
    start_height: int
    network: str
    listen_port: int
    software_version: str


def encode_version(v: VersionPayload) -> bytes:
    """
    VERSION payload layout (all little-endian integers):

      protocol_version: uint32
      services:         uint64
      timestamp:        int64
      nonce:            uint64
      start_height:     int32
      network:          varint(len) + UTF-8 (max MAX_NETWORK_NAME_LEN)
      listen_port:      uint16
      software_version: varint(len) + UTF-8 (max MAX_SOFTWARE_VERSION_LEN)
    """
    if v.network not in NETWORK_MAGIC:
        raise ProtocolError(f"unknown network in VERSION: {v.network}")
    net_b = v.network.encode("utf-8")
    soft_b = v.software_version.encode("utf-8")
    if len(net_b) == 0 or len(net_b) > MAX_NETWORK_NAME_LEN:
        raise ProtocolError("invalid network string length")
    if len(soft_b) == 0 or len(soft_b) > MAX_SOFTWARE_VERSION_LEN:
        raise ProtocolError("invalid software_version length")
    if not 0 <= v.listen_port <= 65535:
        raise ProtocolError("listen_port out of range")
    return (
        write_u32(v.protocol_version)
        + write_u64(v.services)
        + write_i64(v.timestamp)
        + write_u64(v.nonce)
        + write_i32(v.start_height)
        + write_varint(len(net_b))
        + net_b
        + write_u16(v.listen_port)
        + write_varint(len(soft_b))
        + soft_b
    )


def decode_version(payload: bytes) -> VersionPayload:
    try:
        r = Reader(payload)
        protocol_version = r.u32()
        services = r.u64()
        timestamp = r.i64()
        nonce = r.u64()
        start_height = r.i32()
        net_b = r.bytes_blob()
        if len(net_b) == 0 or len(net_b) > MAX_NETWORK_NAME_LEN:
            raise ProtocolError("invalid network string")
        network = net_b.decode("utf-8")
        listen_port = r.u16()
        soft_b = r.bytes_blob()
        if len(soft_b) == 0 or len(soft_b) > MAX_SOFTWARE_VERSION_LEN:
            raise ProtocolError("invalid software_version")
        software_version = soft_b.decode("utf-8")
        if r.remaining() != 0:
            raise ProtocolError("trailing bytes in VERSION")
    except (SerializationError, UnicodeDecodeError, ValueError) as e:
        raise ProtocolError(f"malformed VERSION: {e}") from e
    if network not in NETWORK_MAGIC:
        raise ProtocolError(f"unknown network: {network}")
    return VersionPayload(
        protocol_version=protocol_version,
        services=services,
        timestamp=timestamp,
        nonce=nonce,
        start_height=start_height,
        network=network,
        listen_port=listen_port,
        software_version=software_version,
    )


def encode_verack() -> bytes:
    """VERACK has empty payload."""
    return b""


def decode_verack(payload: bytes) -> None:
    if payload:
        raise ProtocolError("VERACK must be empty")


def encode_ping(nonce: int) -> bytes:
    return write_u64(nonce & 0xFFFFFFFFFFFFFFFF)


def decode_ping(payload: bytes) -> int:
    if len(payload) != 8:
        raise ProtocolError("PING payload must be 8 bytes")
    try:
        return Reader(payload).u64()
    except SerializationError as e:
        raise ProtocolError(str(e)) from e


def encode_pong(nonce: int) -> bytes:
    return encode_ping(nonce)


def decode_pong(payload: bytes) -> int:
    if len(payload) != 8:
        raise ProtocolError("PONG payload must be 8 bytes")
    return decode_ping(payload)


# Advisory miner/node status (post-handshake). Old peers ignore unknown commands.
STATUS_FLAG_MINING = 0x01


@dataclass(frozen=True)
class StatusPayload:
    mining: bool
    hps: int  # integer hashes/sec
    # Optional tip height (advisory). -1 = omitted (legacy 9-byte STATUS).
    height: int = -1


def encode_status(s: StatusPayload) -> bytes:
    """
    STATUS payload:

      flags:  uint8   (bit0 = mining)
      hps:    uint64  (integer H/s)
      height: uint32  (optional tip height; present when height >= 0)

    Legacy peers send/accept 9-byte payloads; newer peers use 13 bytes.
    """
    flags = STATUS_FLAG_MINING if s.mining else 0
    hps = int(s.hps)
    if hps < 0:
        raise ProtocolError("STATUS hps must be >= 0")
    out = write_u8(flags & 0xFF) + write_u64(hps & 0xFFFFFFFFFFFFFFFF)
    h = int(s.height)
    if h >= 0:
        if h > 0xFFFFFFFF:
            raise ProtocolError("STATUS height out of range")
        out += write_u32(h)
    return out


def decode_status(payload: bytes) -> StatusPayload:
    if len(payload) not in (9, 13):
        raise ProtocolError("STATUS payload must be 9 or 13 bytes")
    try:
        r = Reader(payload)
        flags = r.u8()
        hps = r.u64()
        height = -1
        if r.remaining() == 4:
            height = int(r.u32())
        elif r.remaining() != 0:
            raise ProtocolError("trailing bytes in STATUS")
        return StatusPayload(
            mining=bool(flags & STATUS_FLAG_MINING),
            hps=int(hps),
            height=height,
        )
    except SerializationError as e:
        raise ProtocolError(str(e)) from e


def _encode_inventory(items: list[InventoryVector] | list[tuple[int, bytes]], *, max_items: int) -> bytes:
    if len(items) > max_items:
        raise ProtocolError(f"inventory count exceeds {max_items}")
    out = write_varint(len(items))
    for item in items:
        if isinstance(item, InventoryVector):
            typ, h = item.type, item.hash
        else:
            typ, h = item
        if typ not in _ALLOWED_INV_TYPES:
            raise ProtocolError(f"unsupported inventory type {typ}")
        if not isinstance(h, (bytes, bytearray)) or len(h) != 32:
            raise ProtocolError("inventory hash must be 32 bytes")
        out += write_u32(typ) + write_hash32(bytes(h))
    return out


def _decode_inventory(payload: bytes, *, max_items: int) -> list[InventoryVector]:
    try:
        r = Reader(payload)
        n = r.varint()
        if n > max_items:
            raise ProtocolError(f"inventory count exceeds {max_items}")
        items: list[InventoryVector] = []
        for _ in range(n):
            typ = r.u32()
            h = r.hash32()
            if typ not in _ALLOWED_INV_TYPES:
                raise ProtocolError(f"invalid inventory type {typ}")
            items.append(InventoryVector(type=typ, hash=h))
        if r.remaining() != 0:
            raise ProtocolError("trailing bytes in inventory payload")
        return items
    except SerializationError as e:
        raise ProtocolError(f"malformed inventory: {e}") from e


def encode_inv(items: list[InventoryVector] | list[tuple[int, bytes]]) -> bytes:
    """
    INV payload:

      count: varint
      repeated:
        type: uint32   (INV_TYPE_TX = 1)
        hash: 32 bytes (txid)
    """
    return _encode_inventory(items, max_items=MAX_INV_ITEMS)


def decode_inv(payload: bytes) -> list[InventoryVector]:
    return _decode_inventory(payload, max_items=MAX_INV_ITEMS)


def encode_getdata(items: list[InventoryVector] | list[tuple[int, bytes]]) -> bytes:
    """GETDATA uses the same layout as INV (bounded by MAX_GETDATA_ITEMS)."""
    return _encode_inventory(items, max_items=MAX_GETDATA_ITEMS)


def decode_getdata(payload: bytes) -> list[InventoryVector]:
    return _decode_inventory(payload, max_items=MAX_GETDATA_ITEMS)


def encode_tx(tx: Transaction) -> bytes:
    """TX payload = canonical Transaction.serialize()."""
    raw = tx.serialize()
    if len(raw) > MAX_TX_SIZE:
        raise ProtocolError(f"transaction exceeds MAX_TX_SIZE ({MAX_TX_SIZE})")
    return raw


def decode_tx(payload: bytes) -> Transaction:
    if len(payload) > MAX_TX_SIZE:
        raise ProtocolError(f"transaction exceeds MAX_TX_SIZE ({MAX_TX_SIZE})")
    try:
        return Transaction.deserialize(payload)
    except SerializationError as e:
        raise ProtocolError(f"malformed TX: {e}") from e


def encode_block(block: Block) -> bytes:
    """BLOCK payload = canonical Block.serialize()."""
    raw = block.serialize()
    if len(raw) > MAX_BLOCK_SIZE:
        raise ProtocolError(f"block exceeds MAX_BLOCK_SIZE ({MAX_BLOCK_SIZE})")
    return raw


def decode_block(payload: bytes) -> Block:
    if len(payload) > MAX_BLOCK_SIZE:
        raise ProtocolError(f"block exceeds MAX_BLOCK_SIZE ({MAX_BLOCK_SIZE})")
    try:
        return Block.deserialize(payload)
    except (ValueError, SerializationError) as e:
        raise ProtocolError(f"malformed BLOCK: {e}") from e


# --- GETHEADERS / HEADERS --------------------------------------------------


def encode_getheaders(locator: list[bytes], hash_stop: bytes | None = None) -> bytes:
    """
    GETHEADERS payload:

      locator_count: varint
      locator_hashes: count × 32 bytes (newest first)
      hash_stop: 32 bytes (zeros = continue to tip / limit)
    """
    if len(locator) > MAX_LOCATOR_HASHES:
        raise ProtocolError(f"locator exceeds {MAX_LOCATOR_HASHES}")
    stop = hash_stop if hash_stop is not None else ZERO_HASH
    if len(stop) != 32:
        raise ProtocolError("hash_stop must be 32 bytes")
    out = write_varint(len(locator))
    for h in locator:
        if len(h) != 32:
            raise ProtocolError("locator hash must be 32 bytes")
        out += write_hash32(h)
    out += write_hash32(stop)
    return out


def decode_getheaders(payload: bytes) -> tuple[list[bytes], bytes]:
    try:
        r = Reader(payload)
        n = r.varint()
        if n > MAX_LOCATOR_HASHES:
            raise ProtocolError(f"locator exceeds {MAX_LOCATOR_HASHES}")
        locator = [r.hash32() for _ in range(n)]
        hash_stop = r.hash32()
        if r.remaining() != 0:
            raise ProtocolError("trailing bytes in GETHEADERS")
        return locator, hash_stop
    except SerializationError as e:
        raise ProtocolError(f"malformed GETHEADERS: {e}") from e


def encode_headers(headers: list[BlockHeader]) -> bytes:
    """
    HEADERS payload:

      count: varint
      headers: count × 80-byte BlockHeader
    """
    if len(headers) > MAX_HEADERS:
        raise ProtocolError(f"headers exceed {MAX_HEADERS}")
    out = write_varint(len(headers))
    for h in headers:
        raw = h.serialize()
        if len(raw) != 80:
            raise ProtocolError("header must be 80 bytes")
        out += raw
    return out


def decode_headers(payload: bytes) -> list[BlockHeader]:
    try:
        r = Reader(payload)
        n = r.varint()
        if n > MAX_HEADERS:
            raise ProtocolError(f"headers exceed {MAX_HEADERS}")
        headers: list[BlockHeader] = []
        for _ in range(n):
            raw = r.read(80)
            headers.append(BlockHeader.deserialize(raw))
        if r.remaining() != 0:
            raise ProtocolError("trailing bytes in HEADERS")
        return headers
    except (SerializationError, ValueError) as e:
        raise ProtocolError(f"malformed HEADERS: {e}") from e


# --- GETADDR / ADDR --------------------------------------------------------


@dataclass(frozen=True)
class NetAddress:
    """Advertised peer address (gossip). Host is untrusted text."""

    timestamp: int
    services: int
    host: str
    port: int

    def key(self) -> str:
        return f"{self.host}:{self.port}"


def encode_getaddr() -> bytes:
    """GETADDR has empty payload."""
    return b""


def decode_getaddr(payload: bytes) -> None:
    if payload:
        raise ProtocolError("GETADDR must be empty")


def encode_addr(addrs: list[NetAddress]) -> bytes:
    """
    ADDR payload:

      count: varint
      repeated:
        timestamp: uint32
        services:  uint64
        host:      varint(len) + UTF-8 (max MAX_ADDR_HOST_LEN)
        port:      uint16
    """
    if len(addrs) > MAX_ADDR_ENTRIES:
        raise ProtocolError(f"ADDR count exceeds {MAX_ADDR_ENTRIES}")
    out = write_varint(len(addrs))
    for a in addrs:
        host_b = a.host.encode("utf-8")
        if not host_b or len(host_b) > MAX_ADDR_HOST_LEN:
            raise ProtocolError("invalid ADDR host length")
        if not 1 <= a.port <= 65535:
            raise ProtocolError("ADDR port out of range")
        if a.timestamp < 0 or a.timestamp > 0xFFFFFFFF:
            raise ProtocolError("ADDR timestamp out of range")
        out += (
            write_u32(int(a.timestamp) & 0xFFFFFFFF)
            + write_u64(int(a.services) & 0xFFFFFFFFFFFFFFFF)
            + write_varint(len(host_b))
            + host_b
            + write_u16(int(a.port))
        )
    return out


def decode_addr(payload: bytes) -> list[NetAddress]:
    try:
        r = Reader(payload)
        n = r.varint()
        if n > MAX_ADDR_ENTRIES:
            raise ProtocolError(f"ADDR count exceeds {MAX_ADDR_ENTRIES}")
        out: list[NetAddress] = []
        for _ in range(n):
            ts = r.u32()
            services = r.u64()
            host_b = r.bytes_blob()
            if not host_b or len(host_b) > MAX_ADDR_HOST_LEN:
                raise ProtocolError("invalid ADDR host")
            host = host_b.decode("utf-8")
            port = r.u16()
            if not 1 <= port <= 65535:
                raise ProtocolError("ADDR port out of range")
            # Reject NULs / whitespace-only / path separators (basic hygiene)
            if "\x00" in host or host != host.strip() or "/" in host or "\\" in host:
                raise ProtocolError("invalid ADDR host characters")
            out.append(NetAddress(timestamp=ts, services=services, host=host, port=port))
        if r.remaining() != 0:
            raise ProtocolError("trailing bytes in ADDR")
        return out
    except (SerializationError, UnicodeDecodeError, ValueError) as e:
        raise ProtocolError(f"malformed ADDR: {e}") from e
