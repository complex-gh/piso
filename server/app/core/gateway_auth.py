import hashlib
import time

from django.core.cache import cache
from django.http import JsonResponse

from .crypto import (
    AUTH_MAX_SKEW_SECS,
    NONCE_BYTES,
    canonical_auth_message,
    verify_ed25519,
)
from .models import Principal
from .models.identity import PrincipalKind, PrincipalStatus

HEADER_PUBKEY = "X-Piso-Pubkey"
HEADER_TIMESTAMP = "X-Piso-Timestamp"
HEADER_NONCE = "X-Piso-Nonce"
HEADER_SIGNATURE = "X-Piso-Signature"


def _body_hash_hex(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _consume_nonce(nonce: str) -> bool:
    raw = nonce.strip().lower()
    try:
        decoded = bytes.fromhex(raw)
    except ValueError:
        return False
    if len(decoded) != NONCE_BYTES:
        return False
    key = f"piso-auth-nonce:{raw}"
    return cache.add(key, 1, timeout=AUTH_MAX_SKEW_SECS * 2)


def verify_bound_auth(pubkey: str, timestamp: str, nonce: str, signature: str, method: str, path: str, body: bytes) -> bool:
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(int(time.time()) - ts) > AUTH_MAX_SKEW_SECS:
        return False
    if not _consume_nonce(nonce):
        return False
    msg = canonical_auth_message(timestamp, pubkey, method, path, _body_hash_hex(body), nonce)
    return verify_ed25519(pubkey, msg, signature)


def gateway_principal_from_request(request):
    pubkey = (request.headers.get(HEADER_PUBKEY) or "").strip().lower()
    timestamp = (request.headers.get(HEADER_TIMESTAMP) or "").strip()
    nonce = (request.headers.get(HEADER_NONCE) or "").strip()
    signature = (request.headers.get(HEADER_SIGNATURE) or "").strip()
    if not pubkey or not timestamp or not nonce or not signature:
        return None
    if not verify_bound_auth(
        pubkey,
        timestamp,
        nonce,
        signature,
        request.method,
        request.path,
        request.body or b"",
    ):
        return None
    try:
        return Principal.objects.get(
            nostr_pubkey=pubkey,
            kind=PrincipalKind.GATEWAY,
            status=PrincipalStatus.ACTIVE,
        )
    except Principal.DoesNotExist:
        return None


def require_gateway(view):
    def wrapped(request, *args, **kwargs):
        principal = gateway_principal_from_request(request)
        if principal is None:
            return JsonResponse({"error": "unauthorized"}, status=401)
        request.gateway_principal = principal
        return view(request, *args, **kwargs)

    return wrapped
