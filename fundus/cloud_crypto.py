"""Fundus Cloud cryptography (libsodium via PyNaCl, Argon2id via argon2-cffi). Nothing here touches the network.

Design (see README "Fundus Cloud"):
- Password: Argon2id(password, salt = SHA-256(lowercase email)) → 32 bytes; HKDF-SHA256 splits it into auth_secret (sent to
  Firebase as the account password, so Google never sees the real one) and wrap_key (encrypts the private keys).
- Identity: X25519 (encryption) + Ed25519 (signing), generated locally. The private keys are wrapped (secretbox) once with
  wrap_key and once with a recovery key (128 random bits, shown once as base32).
- Fingerprint: 6 words (German wordlist, 256 words = 1 byte each) from SHA-256 of both public keys.
- Packages: random key K; XChaCha20-Poly1305 secretstream over the file in chunks; K sealed (SealedBox) to each recipient;
  the header (package id, sender uid/email/fingerprint, created, size, sha256 of the plaintext, recipients) is signed with
  Ed25519 and bound to every chunk as additional data.

File format (.bin):  MAGIC | u32 header length | header JSON | 64-byte signature | 24-byte stream header |
                     chunks: u32 ciphertext length | ciphertext (last chunk tagged FINAL)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
from pathlib import Path

import nacl.bindings as sb
from nacl.public import PrivateKey, PublicKey, SealedBox
from nacl.secret import SecretBox
from nacl.signing import SigningKey, VerifyKey
from nacl.exceptions import BadSignatureError, CryptoError as NaclCryptoError

MAGIC = b"FUNDUSCLOUD1\n"
CHUNK = 64 * 1024
# Argon2id parameters per version. Raise by adding a version; accounts keep theirs (stored in the cloud record and the
# local identity), sign-in tries the newest first and then the older ones.
KDF_VERSIONS = {1: {"alg": "argon2id", "v": 1, "t": 3, "m": 131072, "p": 1}}   # ~0.4 s on the Spark, 128 MiB
KDF_CURRENT = 1
WORDS_FILE = Path(__file__).resolve().parent / "cloud_words_de.txt"


class CryptoError(Exception):
    """Decryption, unwrapping or verification failed (wrong key, tampered or truncated data, bad signature)."""


def b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def unb64(s: str) -> bytes:
    return base64.b64decode(s)


def norm_email(email: str) -> str:
    e = (email or "").strip().lower()
    if "@" not in e or len(e) > 254 or any(c.isspace() for c in e):
        raise ValueError(f"keine gültige E-Mail-Adresse: '{email}'")
    return e


def email_hash(email: str) -> str:
    """Document id of the address book entry: hex SHA-256 of the lowercase email."""
    return hashlib.sha256(norm_email(email).encode()).hexdigest()


# ---------------------------------------------------------------- password derivation
def hkdf(ikm: bytes, info: bytes, length: int = 32, salt: bytes = b"") -> bytes:
    """HKDF-SHA256 (RFC 5869)."""
    prk = hmac.new(salt or b"\0" * 32, ikm, hashlib.sha256).digest()
    out, t, i = b"", b"", 1
    while len(out) < length:
        t = hmac.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
        out += t
        i += 1
    return out[:length]


def derive(password: str, email: str, params: dict | None = None) -> tuple[str, bytes]:
    """(auth_secret, wrap_key) for a password. auth_secret is the Firebase password (base64url, 43 chars)."""
    from argon2.low_level import Type, hash_secret_raw
    if not password:
        raise ValueError("Passwort fehlt")
    p = params or KDF_VERSIONS[KDF_CURRENT]
    if p.get("alg") != "argon2id":
        raise ValueError(f"unbekanntes KDF-Verfahren {p.get('alg')}")
    salt = hashlib.sha256(norm_email(email).encode()).digest()
    master = hash_secret_raw(password.encode("utf-8"), salt, time_cost=int(p["t"]), memory_cost=int(p["m"]),
                             parallelism=int(p["p"]), hash_len=32, type=Type.ID)
    auth = hkdf(master, b"fundus-cloud auth v1")
    wrap = hkdf(master, b"fundus-cloud wrap v1")
    return base64.urlsafe_b64encode(auth).decode().rstrip("="), wrap


# ---------------------------------------------------------------- identity
def new_identity() -> dict:
    """Fresh key pairs (private parts as base64)."""
    box, sign = PrivateKey.generate(), SigningKey.generate()
    pub = {"box_pk": b64(bytes(box.public_key)), "sign_pk": b64(bytes(sign.verify_key))}
    return {"box_sk": b64(bytes(box)), "sign_seed": b64(bytes(sign)), **pub, "fingerprint": fingerprint(pub["box_pk"], pub["sign_pk"])}


def public_part(ident: dict) -> dict:
    return {k: ident[k] for k in ("box_pk", "sign_pk", "fingerprint")}


def _secret_blob(ident: dict) -> bytes:
    return json.dumps({"box_sk": ident["box_sk"], "sign_seed": ident["sign_seed"]}, sort_keys=True).encode()


def _from_blob(blob: bytes) -> dict:
    d = json.loads(blob)
    box, sign = PrivateKey(unb64(d["box_sk"])), SigningKey(unb64(d["sign_seed"]))
    pub = {"box_pk": b64(bytes(box.public_key)), "sign_pk": b64(bytes(sign.verify_key))}
    return {**d, **pub, "fingerprint": fingerprint(pub["box_pk"], pub["sign_pk"])}


def wrap(ident: dict, key: bytes) -> str:
    """Private keys encrypted with a 32-byte key (secretbox: XSalsa20-Poly1305, random nonce), base64."""
    return b64(bytes(SecretBox(key).encrypt(_secret_blob(ident))))


def unwrap(blob: str, key: bytes) -> dict:
    try:
        return _from_blob(SecretBox(key).decrypt(unb64(blob)))
    except (NaclCryptoError, ValueError, KeyError) as e:
        raise CryptoError("Schlüssel lassen sich nicht entschlüsseln (falsches Passwort oder Wiederherstellungscode)") from e


def new_recovery_code() -> str:
    """128 random bits as base32 in groups of four (shown once)."""
    raw = base64.b32encode(secrets.token_bytes(16)).decode().rstrip("=")    # 26 characters
    return "-".join(raw[i:i + 4] for i in range(0, len(raw), 4))


def recovery_key(code: str) -> bytes:
    s = "".join(c for c in (code or "").upper() if c.isalnum())
    s = s.replace("0", "O").replace("1", "I").replace("8", "B")            # common misreadings (base32 has no 0/1/8)
    if len(s) != 26:
        raise ValueError("Wiederherstellungscode: 26 Zeichen erwartet")
    try:
        raw = base64.b32decode(s + "=" * 6)
    except ValueError as e:
        raise ValueError("Wiederherstellungscode ungültig") from e
    return hkdf(raw, b"fundus-cloud recovery v1")


# ---------------------------------------------------------------- fingerprint
_words: list[str] | None = None


def words() -> list[str]:
    global _words
    if _words is None:
        w = [x.strip() for x in WORDS_FILE.read_text(encoding="utf-8").splitlines() if x.strip()]
        if len(w) != 256 or len({x.lower() for x in w}) != 256:
            raise RuntimeError("Wortliste: genau 256 verschiedene Wörter erwartet")
        _words = w
    return _words


def fingerprint(box_pk: str, sign_pk: str) -> str:
    """Six words from SHA-256 over both public keys (48 bits) – compared by phone before 'Kontakt bestätigen'."""
    h = hashlib.sha256(b"fundus-cloud fingerprint v1\0" + unb64(box_pk) + unb64(sign_pk)).digest()
    w = words()
    return " ".join(w[b] for b in h[:6])


# ---------------------------------------------------------------- packages
def _canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def seal_key(key: bytes, box_pk: str) -> str:
    return b64(SealedBox(PublicKey(unb64(box_pk))).encrypt(key))


def open_key(sealed: str, ident: dict) -> bytes:
    try:
        return SealedBox(PrivateKey(unb64(ident["box_sk"]))).decrypt(unb64(sealed))
    except (NaclCryptoError, ValueError) as e:
        raise CryptoError("Paketschlüssel nicht für diesen Empfänger") from e


def encrypt_file(src, dst, header: dict, ident: dict, recipients: dict[str, str], progress=None) -> dict:
    """Encrypt src to dst. header: package id, sender uid/email, created … (sha256, size, fingerprint and recipients are
    added here). recipients: {uid: box_pk}. Returns {"header": signed header, "sealed": {uid: sealed K}}."""
    src, dst = Path(src), Path(dst)
    size = src.stat().st_size
    hdr = {**header, "size": size, "sha256": sha256_file(src), "sender_fingerprint": ident["fingerprint"],
           "recipients": sorted(recipients), "chunk": CHUNK, "format": 1}
    hbytes = _canon(hdr)
    sig = SigningKey(unb64(ident["sign_seed"])).sign(hbytes).signature
    key = sb.crypto_secretstream_xchacha20poly1305_keygen()
    state = sb.crypto_secretstream_xchacha20poly1305_state()
    sheader = sb.crypto_secretstream_xchacha20poly1305_init_push(state, key)
    ad = hashlib.sha256(hbytes).digest()
    done = 0
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        fout.write(MAGIC + struct.pack(">I", len(hbytes)) + hbytes + sig + sheader)
        chunk = fin.read(CHUNK)
        while True:
            nxt = fin.read(CHUNK) if chunk else b""
            tag = (sb.crypto_secretstream_xchacha20poly1305_TAG_FINAL if not nxt
                   else sb.crypto_secretstream_xchacha20poly1305_TAG_MESSAGE)
            ct = sb.crypto_secretstream_xchacha20poly1305_push(state, chunk, ad, tag)
            fout.write(struct.pack(">I", len(ct)) + ct)
            done += len(chunk)
            if progress:
                progress(done, size)
            if not nxt:
                break
            chunk = nxt
    sealed = {uid: seal_key(key, pk) for uid, pk in recipients.items()}
    return {"header": hdr, "signature": b64(sig), "sealed": sealed}


def read_header(path) -> tuple[dict, bytes, bytes]:
    """(header, header bytes, signature) of an encrypted package, without verifying."""
    with open(path, "rb") as f:
        if f.read(len(MAGIC)) != MAGIC:
            raise CryptoError("kein Fundus-Cloud-Paket")
        (n,) = struct.unpack(">I", f.read(4))
        if n > 1 << 20:
            raise CryptoError("Paketkopf zu gross")
        hbytes = f.read(n)
        sig = f.read(64)
    try:
        return json.loads(hbytes), hbytes, sig
    except ValueError as e:
        raise CryptoError("Paketkopf unlesbar") from e


def verify_header(hbytes: bytes, sig: bytes, sign_pk: str) -> None:
    try:
        VerifyKey(unb64(sign_pk)).verify(hbytes, sig)
    except (BadSignatureError, ValueError) as e:
        raise CryptoError("Signatur ungültig: das Paket stammt nicht vom erwarteten Absender oder wurde verändert") from e


def decrypt_file(src, dst, ident: dict, sealed_key: str, sender_sign_pk: str, progress=None) -> dict:
    """Verify the header signature against sender_sign_pk, decrypt src to dst, check size and SHA-256. Returns the header.
    On any failure dst is removed and CryptoError raised."""
    src, dst = Path(src), Path(dst)
    hdr, hbytes, sig = read_header(src)
    verify_header(hbytes, sig, sender_sign_pk)
    key = open_key(sealed_key, ident)
    ad = hashlib.sha256(hbytes).digest()
    state = sb.crypto_secretstream_xchacha20poly1305_state()
    h = hashlib.sha256()
    done, final = 0, False
    try:
        with open(src, "rb") as fin, open(dst, "wb") as fout:
            fin.seek(len(MAGIC) + 4 + len(hbytes) + 64)
            sheader = fin.read(sb.crypto_secretstream_xchacha20poly1305_HEADERBYTES)
            try:
                sb.crypto_secretstream_xchacha20poly1305_init_pull(state, sheader, key)
            except Exception as e:                       # noqa: BLE001 – libsodium raises various types
                raise CryptoError("Stromkopf ungültig") from e
            while True:
                lb = fin.read(4)
                if not lb:
                    break
                if final:
                    raise CryptoError("Daten nach dem letzten Block")
                (n,) = struct.unpack(">I", lb)
                if n > CHUNK + sb.crypto_secretstream_xchacha20poly1305_ABYTES:
                    raise CryptoError("Block zu gross")
                ct = fin.read(n)
                try:
                    pt, tag = sb.crypto_secretstream_xchacha20poly1305_pull(state, ct, ad)
                except Exception as e:                   # noqa: BLE001
                    raise CryptoError("Block verändert oder falscher Schlüssel") from e
                fout.write(pt)
                h.update(pt)
                done += len(pt)
                if progress:
                    progress(done, hdr.get("size") or done)
                final = tag == sb.crypto_secretstream_xchacha20poly1305_TAG_FINAL
        if not final:
            raise CryptoError("Paket unvollständig (letzter Block fehlt)")
        if done != hdr.get("size") or h.hexdigest() != hdr.get("sha256"):
            raise CryptoError("Prüfsumme stimmt nicht")
    except BaseException:
        dst.unlink(missing_ok=True)
        raise
    return hdr


def write_private(path, data: dict) -> None:
    """JSON file readable only by the owner (0600), written atomically."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
