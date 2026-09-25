from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey


AUTH_SCHEME = "piso-gateway-auth"
AUTH_VERSION = "v1"
AUTH_MAX_SKEW_SECS = 60
NONCE_BYTES = 16


def canonical_auth_message(
    timestamp: str,
    pubkey: str,
    method: str,
    path: str,
    body_hash: str,
    nonce: str,
) -> bytes:
    return (
        "\n".join(
            [
                AUTH_SCHEME,
                AUTH_VERSION,
                timestamp.strip(),
                pubkey.strip().lower(),
                method.strip().upper(),
                path,
                body_hash.strip().lower(),
                nonce.strip().lower(),
            ]
        )
        + "\n"
    ).encode()


def verify_ed25519(pubkey_hex: str, message: bytes, signature_hex: str) -> bool:
    try:
        key = VerifyKey(bytes.fromhex(pubkey_hex.strip()))
        key.verify(message, bytes.fromhex(signature_hex.strip()))
        return True
    except (BadSignatureError, ValueError):
        return False
