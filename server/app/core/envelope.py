"""Secret envelope format shared with piso/internal/technocore (Go).

Payload: ChaCha20-Poly1305 (RFC 8439) with AAD piso-secret-v1.
Wrap: ephemeral X25519 + HKDF-SHA256 + ChaCha20-Poly1305 of the DEK.

Wrap blob:
  version (1) || eph_pub (32) || nonce (12) || sealed_dek (48)
"""

from __future__ import annotations

import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

ENVELOPE_VERSION = 1
ENVELOPE_ALG = "chacha20poly1305-v1"
WRAP_INFO = b"piso-envelope-wrap-v1"
PAYLOAD_AAD = b"piso-secret-v1"
X25519_SIZE = 32
NONCE_SIZE = 12
DEK_SIZE = 32
TAG_SIZE = 16
WRAP_LEN = 1 + X25519_SIZE + NONCE_SIZE + DEK_SIZE + TAG_SIZE


def seal_payload(plaintext: bytes) -> tuple[bytes, bytes, bytes]:
    dek = os.urandom(DEK_SIZE)
    nonce = os.urandom(NONCE_SIZE)
    ct = ChaCha20Poly1305(dek).encrypt(nonce, plaintext, PAYLOAD_AAD)
    return dek, nonce, ct


def open_payload(dek: bytes, nonce: bytes, ciphertext: bytes) -> bytes:
    return ChaCha20Poly1305(dek).decrypt(nonce, ciphertext, PAYLOAD_AAD)


def wrap_dek(recipient_pub: bytes, dek: bytes) -> bytes:
    if len(dek) != DEK_SIZE:
        raise ValueError("dek must be 32 bytes")
    if len(recipient_pub) != X25519_SIZE:
        raise ValueError("recipient public key must be 32 bytes")
    eph = X25519PrivateKey.generate()
    shared = eph.exchange(X25519PublicKey.from_public_bytes(recipient_pub))
    wrap_key = HKDF(
        algorithm=hashes.SHA256(),
        length=DEK_SIZE,
        salt=None,
        info=WRAP_INFO,
    ).derive(shared)
    nonce = os.urandom(NONCE_SIZE)
    sealed = ChaCha20Poly1305(wrap_key).encrypt(nonce, dek, WRAP_INFO)
    eph_pub = eph.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return bytes([ENVELOPE_VERSION]) + eph_pub + nonce + sealed


def unwrap_dek(recipient_priv: bytes, blob: bytes) -> bytes:
    if len(blob) != WRAP_LEN:
        raise ValueError(f"wrap blob length {len(blob)}, want {WRAP_LEN}")
    if blob[0] != ENVELOPE_VERSION:
        raise ValueError(f"unsupported wrap version {blob[0]}")
    eph_pub = blob[1 : 1 + X25519_SIZE]
    nonce = blob[1 + X25519_SIZE : 1 + X25519_SIZE + NONCE_SIZE]
    sealed = blob[1 + X25519_SIZE + NONCE_SIZE :]
    priv = X25519PrivateKey.from_private_bytes(recipient_priv)
    shared = priv.exchange(X25519PublicKey.from_public_bytes(eph_pub))
    wrap_key = HKDF(
        algorithm=hashes.SHA256(),
        length=DEK_SIZE,
        salt=None,
        info=WRAP_INFO,
    ).derive(shared)
    return ChaCha20Poly1305(wrap_key).decrypt(nonce, sealed, WRAP_INFO)


def open_envelope(recipient_priv: bytes, wrap_blob: bytes, nonce: bytes, ciphertext: bytes) -> bytes:
    dek = unwrap_dek(recipient_priv, wrap_blob)
    return open_payload(dek, nonce, ciphertext)
