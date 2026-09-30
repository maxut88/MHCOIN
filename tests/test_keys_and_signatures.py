from mhcoin.crypto.hashing import hash256
from mhcoin.crypto.keys import generate_keypair, private_key_to_public_key
from mhcoin.crypto.signatures import SignatureError, sign_digest, verify_digest, verify_digest_strict


def test_keypair_roundtrip():
    kp = generate_keypair()
    assert len(kp.private_key) == 32
    assert len(kp.public_key_compressed) == 33
    assert private_key_to_public_key(kp.private_key) == kp.public_key_compressed


def test_sign_and_verify():
    kp = generate_keypair()
    digest = hash256(b"MHCOIN tx placeholder")
    sig = sign_digest(kp.private_key, digest)
    assert verify_digest(kp.public_key_compressed, digest, sig)


def test_tampered_digest_fails():
    kp = generate_keypair()
    digest = hash256(b"a")
    sig = sign_digest(kp.private_key, digest)
    bad = hash256(b"b")
    assert not verify_digest(kp.public_key_compressed, bad, sig)
    try:
        verify_digest_strict(kp.public_key_compressed, bad, sig)
        assert False, "expected SignatureError"
    except SignatureError:
        pass
