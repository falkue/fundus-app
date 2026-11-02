"""Fundus Cloud crypto: round trips, wrong keys, tampering, signatures, fingerprints, recovery code (no network)."""
import os
import struct

import pytest

from fundus import cloud_crypto as cc

FAST = {"alg": "argon2id", "v": 1, "t": 1, "m": 8192, "p": 1}


@pytest.fixture
def alice():
    return {**cc.new_identity(), "uid": "uidA"}


@pytest.fixture
def bob():
    return {**cc.new_identity(), "uid": "uidB"}


def _pkg(tmp_path, size=200_000):
    p = tmp_path / "plain.zip"
    p.write_bytes(os.urandom(size))
    return p


def test_wordlist_and_fingerprint_stable(alice):
    assert len(cc.words()) == 256
    fp = cc.fingerprint(alice["box_pk"], alice["sign_pk"])
    assert fp == alice["fingerprint"] == cc.fingerprint(alice["box_pk"], alice["sign_pk"])
    assert len(fp.split()) == 6 and all(w in cc.words() for w in fp.split())
    other = cc.new_identity()
    assert cc.fingerprint(other["box_pk"], alice["sign_pk"]) != fp


def test_fingerprint_known_value():
    # fixed keys → fixed words (guards against accidental changes of the wordlist or the derivation)
    pk = cc.b64(bytes(32))
    assert cc.fingerprint(pk, pk) == "Igel Bank Glocke Hecke Fest Burg"


def test_derive_deterministic_and_separated():
    a1, w1 = cc.derive("geheimes Passwort", "Falk@Example.org ", FAST)
    a2, w2 = cc.derive("geheimes Passwort", "falk@example.org", FAST)
    assert (a1, w1) == (a2, w2)
    assert len(w1) == 32 and len(a1) == 43 and a1.encode() != w1
    a3, w3 = cc.derive("anderes Passwort", "falk@example.org", FAST)
    assert a3 != a1 and w3 != w1
    a4, _ = cc.derive("geheimes Passwort", "jemand@example.org", FAST)
    assert a4 != a1


def test_wrap_unwrap_password_and_recovery(alice):
    _, wk = cc.derive("pw-123456789", "a@example.org", FAST)
    blob = cc.wrap(alice, wk)
    back = cc.unwrap(blob, wk)
    assert back["box_sk"] == alice["box_sk"] and back["fingerprint"] == alice["fingerprint"]
    _, wrong = cc.derive("falsch-123456", "a@example.org", FAST)
    with pytest.raises(cc.CryptoError):
        cc.unwrap(blob, wrong)
    code = cc.new_recovery_code()
    assert len(code.replace("-", "")) == 26
    rblob = cc.wrap(alice, cc.recovery_key(code))
    assert cc.unwrap(rblob, cc.recovery_key(code.lower().replace("-", " ")))["sign_seed"] == alice["sign_seed"]
    with pytest.raises(cc.CryptoError):
        cc.unwrap(rblob, cc.recovery_key(cc.new_recovery_code()))
    with pytest.raises(ValueError):
        cc.recovery_key("ABCD")


def test_package_round_trip(tmp_path, alice, bob):
    src = _pkg(tmp_path, 3 * cc.CHUNK + 17)
    enc = cc.encrypt_file(src, tmp_path / "p.bin", {"package_id": "p1", "sender_uid": "uidA"}, alice, {"uidB": bob["box_pk"]})
    assert enc["header"]["sha256"] == cc.sha256_file(src)
    hdr = cc.decrypt_file(tmp_path / "p.bin", tmp_path / "out.zip", bob, enc["sealed"]["uidB"], alice["sign_pk"])
    assert hdr["package_id"] == "p1" and hdr["sender_fingerprint"] == alice["fingerprint"]
    assert (tmp_path / "out.zip").read_bytes() == src.read_bytes()


def test_empty_and_exact_chunk(tmp_path, alice, bob):
    for n in (0, cc.CHUNK):
        src = _pkg(tmp_path, n)
        enc = cc.encrypt_file(src, tmp_path / "p.bin", {"package_id": "p"}, alice, {"uidB": bob["box_pk"]})
        cc.decrypt_file(tmp_path / "p.bin", tmp_path / "o", bob, enc["sealed"]["uidB"], alice["sign_pk"])
        assert (tmp_path / "o").read_bytes() == src.read_bytes()


def test_wrong_recipient_key(tmp_path, alice, bob):
    eve = cc.new_identity()
    src = _pkg(tmp_path)
    enc = cc.encrypt_file(src, tmp_path / "p.bin", {"package_id": "p"}, alice, {"uidB": bob["box_pk"]})
    with pytest.raises(cc.CryptoError):
        cc.decrypt_file(tmp_path / "p.bin", tmp_path / "o", eve, enc["sealed"]["uidB"], alice["sign_pk"])
    assert not (tmp_path / "o").exists()


def test_tampered_chunk(tmp_path, alice, bob):
    src = _pkg(tmp_path, 2 * cc.CHUNK)
    enc = cc.encrypt_file(src, tmp_path / "p.bin", {"package_id": "p"}, alice, {"uidB": bob["box_pk"]})
    raw = bytearray((tmp_path / "p.bin").read_bytes())
    raw[-100] ^= 1
    (tmp_path / "p.bin").write_bytes(bytes(raw))
    with pytest.raises(cc.CryptoError):
        cc.decrypt_file(tmp_path / "p.bin", tmp_path / "o", bob, enc["sealed"]["uidB"], alice["sign_pk"])
    assert not (tmp_path / "o").exists()


def test_truncated_package(tmp_path, alice, bob):
    src = _pkg(tmp_path, 3 * cc.CHUNK)
    enc = cc.encrypt_file(src, tmp_path / "p.bin", {"package_id": "p"}, alice, {"uidB": bob["box_pk"]})
    raw = (tmp_path / "p.bin").read_bytes()
    last = cc.CHUNK + 17 + 4          # drop the final chunk
    (tmp_path / "p.bin").write_bytes(raw[:-last])
    with pytest.raises(cc.CryptoError, match="unvollständig|Block"):
        cc.decrypt_file(tmp_path / "p.bin", tmp_path / "o", bob, enc["sealed"]["uidB"], alice["sign_pk"])


def test_signature_wrong_sender_key_and_header_change(tmp_path, alice, bob):
    src = _pkg(tmp_path)
    enc = cc.encrypt_file(src, tmp_path / "p.bin", {"package_id": "p", "sender_uid": "uidA"}, alice, {"uidB": bob["box_pk"]})
    mallory = cc.new_identity()
    # key changed since first contact: the locally stored key does not verify the package
    with pytest.raises(cc.CryptoError, match="Signatur"):
        cc.decrypt_file(tmp_path / "p.bin", tmp_path / "o", bob, enc["sealed"]["uidB"], mallory["sign_pk"])
    # a changed header (other sender uid) breaks the signature
    raw = (tmp_path / "p.bin").read_bytes()
    raw2 = raw.replace(b'"sender_uid":"uidA"', b'"sender_uid":"uidX"', 1)
    assert raw2 != raw
    (tmp_path / "p.bin").write_bytes(raw2)
    with pytest.raises(cc.CryptoError, match="Signatur"):
        cc.decrypt_file(tmp_path / "p.bin", tmp_path / "o", bob, enc["sealed"]["uidB"], alice["sign_pk"])


def test_several_recipients(tmp_path, alice, bob):
    carol = cc.new_identity()
    src = _pkg(tmp_path)
    enc = cc.encrypt_file(src, tmp_path / "p.bin", {"package_id": "p"}, alice, {"uidB": bob["box_pk"], "uidC": carol["box_pk"]})
    for who, uid in ((bob, "uidB"), (carol, "uidC")):
        cc.decrypt_file(tmp_path / "p.bin", tmp_path / "o", who, enc["sealed"][uid], alice["sign_pk"])
        assert (tmp_path / "o").read_bytes() == src.read_bytes()
    with pytest.raises(cc.CryptoError):     # bob cannot use carol's sealed key
        cc.decrypt_file(tmp_path / "p.bin", tmp_path / "o", bob, enc["sealed"]["uidC"], alice["sign_pk"])


def test_private_file_mode(tmp_path):
    cc.write_private(tmp_path / "x" / "identity.json", {"a": 1})
    assert oct((tmp_path / "x" / "identity.json").stat().st_mode & 0o777) == "0o600"


def test_bad_magic(tmp_path, bob):
    (tmp_path / "p.bin").write_bytes(b"nope" + struct.pack(">I", 2) + b"{}")
    with pytest.raises(cc.CryptoError):
        cc.read_header(tmp_path / "p.bin")
