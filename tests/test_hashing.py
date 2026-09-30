from mhcoin.crypto.hashing import hash160, hash256, sha256


def test_sha256_deterministic():
    assert len(sha256(b"MHCOIN")) == 32


def test_hash256_double():
    h1 = sha256(b"x")
    h2 = sha256(h1)
    assert hash256(b"x") == h2


def test_hash160_length():
    assert len(hash160(b"pubkey")) == 20
