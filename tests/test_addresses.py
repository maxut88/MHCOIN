from mhcoin.crypto.keys import generate_keypair
from mhcoin.wallet.addresses import address_to_pubkey_hash, pubkey_to_address, validate_address


def test_mhc_address_format():
    kp = generate_keypair()
    addr = pubkey_to_address(kp.public_key_compressed, hrp="mhc")
    assert addr.startswith("mhc1")
    assert validate_address(addr, hrp="mhc")
    program = address_to_pubkey_hash(addr, hrp="mhc")
    assert len(program) == 20


def test_wrong_hrp_rejected():
    kp = generate_keypair()
    addr = pubkey_to_address(kp.public_key_compressed, hrp="mhc")
    try:
        address_to_pubkey_hash(addr, hrp="mht")
        assert False
    except ValueError:
        pass
