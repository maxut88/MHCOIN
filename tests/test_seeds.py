"""Bootstrap seed helpers."""

from __future__ import annotations

from mhcoin.network.seeds import default_connect_peers, resolve_dns_seed


def test_hardcoded_mainnet_seed_present():
    peers = default_connect_peers("mainnet", resolve_dns=False)
    assert any(p.endswith(":8333") for p in peers)
    assert "176.38.3.168:8333" in peers


def test_connect_env_override(monkeypatch):
    monkeypatch.setenv("MHCOIN_CONNECT", "203.0.113.9:8333,198.51.100.2:8333")
    peers = default_connect_peers("mainnet")
    assert peers == ["203.0.113.9:8333", "198.51.100.2:8333"]


def test_dns_seed_resolve_localhost():
    # localhost always resolves; proves resolver wiring
    got = resolve_dns_seed("localhost", port=8333, limit=4)
    assert got
    assert all(":8333" in x for x in got)
