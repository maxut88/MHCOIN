"""Bootstrap peers — hardcoded seeds and optional DNS.

Consensus does NOT depend on these hosts. They are only the first contacts
for a fresh node. After handshake, ADDR/GETADDR gossip + AddrDB take over
(seeds → peers.dat → mesh).
"""

from __future__ import annotations

import logging
import os
import socket
from typing import Iterable

logger = logging.getLogger("mhcoin.seeds")

# Hardcoded IP fallbacks for first contact.
# Add more operator IPs as the network grows — never a single point of authority.
HARDCODED_SEEDS: dict[str, list[str]] = {
    "mainnet": [
        "176.38.3.168:8333",
    ],
    "testnet": [],
    "regtest": [],
    "localnet": [],
}

# DNS seeds. Each name should resolve to many A/AAAA
# records run by independent operators. Empty until domains are published.
DNS_SEEDS: dict[str, list[str]] = {
    "mainnet": [
        # Examples once live:
        # "seed.mhcoin.network",
        # "seed2.mhcoin.network",
    ],
    "testnet": [],
    "regtest": [],
    "localnet": [],
}

DEFAULT_P2P_PORT = 8333


def _parse_host_port(item: str, default_port: int) -> tuple[str, int] | None:
    item = item.strip()
    if not item:
        return None
    if item.startswith("[") and "]" in item:
        # [ipv6]:port
        host, _, rest = item[1:].partition("]")
        if rest.startswith(":"):
            try:
                return host, int(rest[1:])
            except ValueError:
                return None
        return host, default_port
    if item.count(":") == 1:
        host, _, port_s = item.partition(":")
        try:
            return host, int(port_s)
        except ValueError:
            return None
    return item, default_port


def resolve_dns_seed(name: str, *, port: int = DEFAULT_P2P_PORT, limit: int = 16) -> list[str]:
    """Resolve a DNS seed hostname to host:port dial targets."""
    name = name.strip().rstrip(".")
    if not name:
        return []
    out: list[str] = []
    try:
        infos = socket.getaddrinfo(name, port, type=socket.SOCK_STREAM)
    except OSError as e:
        logger.info("DNS seed %s resolve failed: %s", name, e)
        return []
    seen: set[str] = set()
    for family, _type, _proto, _canon, sockaddr in infos:
        if family == socket.AF_INET:
            ip = sockaddr[0]
        elif family == socket.AF_INET6:
            ip = sockaddr[0]
        else:
            continue
        key = f"{ip}:{port}"
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
        if len(out) >= limit:
            break
    if out:
        logger.info("DNS seed %s → %d address(es)", name, len(out))
    return out


def _unique(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def default_connect_peers(network: str, *, resolve_dns: bool = True) -> list[str]:
    """Return bootstrap dial targets for a network (override via env).

    Order of preference:
      1) MHCOIN_CONNECT=host:port,... (manual)
      2) Hardcoded seeds + resolved DNS seeds (+ MHCOIN_DNS_SEEDS)
    """
    net = network.strip().lower()
    override = os.environ.get("MHCOIN_CONNECT", "").strip()
    if override:
        return [p.strip() for p in override.split(",") if p.strip()]

    peers = list(HARDCODED_SEEDS.get(net, []))

    dns_names = list(DNS_SEEDS.get(net, []))
    extra_dns = os.environ.get("MHCOIN_DNS_SEEDS", "").strip()
    if extra_dns:
        dns_names.extend(x.strip() for x in extra_dns.split(",") if x.strip())

    params_port = DEFAULT_P2P_PORT
    try:
        from mhcoin.consensus.params import get_network_params

        params_port = int(get_network_params(net).default_port)
    except Exception:
        pass

    if resolve_dns:
        for name in dns_names:
            # Allow "seed.example:8333" or bare hostname
            parsed = _parse_host_port(name, params_port)
            if not parsed:
                continue
            host, port = parsed
            # If it already looks like an IP, use directly
            try:
                socket.inet_pton(socket.AF_INET, host)
                peers.append(f"{host}:{port}")
                continue
            except OSError:
                pass
            try:
                socket.inet_pton(socket.AF_INET6, host)
                peers.append(f"[{host}]:{port}")
                continue
            except OSError:
                pass
            peers.extend(resolve_dns_seed(host, port=port))

    return _unique(peers)
