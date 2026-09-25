import hashlib
import hmac
import json
import secrets
from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db import transaction
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .models import Account, AccountMembership, Event, Gateway, PairingRequest, Principal, PrincipalKey
from .models.events import PairingKind, PairingStatus
from .models.identity import KeyPurpose, MembershipRole, PrincipalKind, PrincipalStatus

PAIRING_TTL = timedelta(minutes=15)
PAIRING_RATE_LIMIT = 20
PAIRING_RATE_WINDOW = 3600
POLL_HEADER = "X-Piso-Poll-Token"


def _json_body(request):
    if not request.body:
        return {}
    try:
        return json.loads(request.body.decode())
    except json.JSONDecodeError:
        return None


def hash_poll_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def expire_if_needed(rec: PairingRequest) -> PairingRequest:
    if rec.status == PairingStatus.PENDING and rec.expires_at <= timezone.now():
        rec.status = PairingStatus.EXPIRED
        rec.save(update_fields=["status"])
    return rec


def _rate_limited(request) -> bool:
    ip = request.META.get("REMOTE_ADDR") or "unknown"
    key = f"piso-pair-create:{ip}"
    added = cache.add(key, 1, PAIRING_RATE_WINDOW)
    if added:
        return False
    try:
        n = cache.incr(key)
    except ValueError:
        cache.set(key, 1, PAIRING_RATE_WINDOW)
        return False
    return n > PAIRING_RATE_LIMIT


def _hex32(value: str, field: str):
    raw = (value or "").strip().lower()
    if len(raw) != 64:
        raise ValueError(f"{field} must be 32-byte hex")
    try:
        bytes.fromhex(raw)
    except ValueError as exc:
        raise ValueError(f"{field} must be hex") from exc
    return raw


@csrf_exempt
@require_http_methods(["POST"])
def create_pairing(request):
    if _rate_limited(request):
        return JsonResponse({"error": "rate limited"}, status=429)
    body = _json_body(request)
    if body is None:
        return JsonResponse({"error": "invalid json"}, status=400)
    kind = body.get("kind") or PairingKind.GATEWAY
    if kind != PairingKind.GATEWAY:
        return JsonResponse({"error": "only gateway pairing is implemented"}, status=400)
    try:
        pubkey = _hex32(body.get("device_pubkey"), "device_pubkey")
        enc = _hex32(body.get("encryption_pubkey"), "encryption_pubkey")
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    name = (body.get("name") or "").strip()[:200]
    token = secrets.token_urlsafe(32)
    rec = PairingRequest.objects.create(
        kind=PairingKind.GATEWAY,
        device_pubkey=pubkey,
        encryption_pubkey=enc,
        name=name,
        fingerprint=pubkey[:12],
        capabilities=body.get("capabilities") or ["credential-relay"],
        poll_token=hash_poll_token(token),
        expires_at=timezone.now() + PAIRING_TTL,
    )
    return JsonResponse(
        {
            "id": str(rec.id),
            "kind": rec.kind,
            "fingerprint": rec.fingerprint,
            "poll_token": token,
            "approve_path": f"/pair/{rec.id}/",
            "expires_at": rec.expires_at.isoformat(),
            "status": rec.status,
        },
        status=201,
    )


@csrf_exempt
@require_http_methods(["GET"])
def pairing_status(request, pk):
    rec = get_object_or_404(PairingRequest, pk=pk)
    token = (request.headers.get(POLL_HEADER) or "").strip()
    if not token or not hmac.compare_digest(hash_poll_token(token), rec.poll_token or ""):
        return JsonResponse({"error": "unauthorized"}, status=401)
    rec = expire_if_needed(rec)
    return JsonResponse(
        {
            "id": str(rec.id),
            "status": rec.status,
            "fingerprint": rec.fingerprint,
            "principal_id": str(rec.principal_id) if rec.principal_id else None,
        }
    )


def _accounts_for_user(user):
    return list(
        AccountMembership.objects.filter(
            user=user,
            role__in=[MembershipRole.OWNER, MembershipRole.ADMIN],
        ).select_related("account")
    )


@transaction.atomic
def approve_gateway(rec: PairingRequest, account: Account) -> Principal:
    existing = Principal.objects.filter(nostr_pubkey=rec.device_pubkey).first()
    if existing:
        if existing.account_id != account.id:
            raise ValueError("device already belongs to another account")
        if existing.kind != PrincipalKind.GATEWAY:
            raise ValueError("device principal is not a gateway")
        principal = existing
        principal.status = PrincipalStatus.ACTIVE
        principal.name = rec.name or rec.fingerprint or principal.name
        principal.save(update_fields=["status", "name"])
        Gateway.objects.get_or_create(
            principal=principal, defaults={"machine_name": principal.name}
        )
    else:
        principal = Principal.objects.create(
            account=account,
            kind=PrincipalKind.GATEWAY,
            nostr_pubkey=rec.device_pubkey,
            name=rec.name or rec.fingerprint,
            status=PrincipalStatus.ACTIVE,
        )
        Gateway.objects.create(principal=principal, machine_name=principal.name)
    _upsert_key(principal, KeyPurpose.SIGNING, rec.device_pubkey)
    if rec.encryption_pubkey:
        _upsert_key(principal, KeyPurpose.ENCRYPTION, rec.encryption_pubkey)
    rec.status = PairingStatus.APPROVED
    rec.account = account
    rec.principal = principal
    rec.save(update_fields=["status", "account", "principal"])
    Event.objects.create(
        account=account,
        actor=principal,
        type="gateway.paired",
        subject_type="gateway",
        subject_id=str(principal.id),
        public_metadata={"name": principal.name, "fingerprint": rec.fingerprint},
    )
    return principal


def _upsert_key(principal: Principal, purpose: str, public_key: str) -> None:
    rec, created = PrincipalKey.objects.get_or_create(
        principal=principal,
        purpose=purpose,
        version=1,
        defaults={"public_key": public_key},
    )
    if not created and rec.public_key != public_key:
        rec.public_key = public_key
        rec.save(update_fields=["public_key"])


@login_required
@require_http_methods(["GET", "POST"])
def pair_page(request, pk):
    rec = expire_if_needed(get_object_or_404(PairingRequest, pk=pk))
    memberships = _accounts_for_user(request.user)
    accounts = [m.account for m in memberships]
    error = ""
    if request.method == "POST":
        action = request.POST.get("action")
        account_id = request.POST.get("account") or ""
        account = next((a for a in accounts if str(a.id) == account_id), None)
        if len(accounts) == 1:
            account = accounts[0]
        if rec.status != PairingStatus.PENDING:
            error = "This pairing request is no longer pending."
        elif account is None:
            error = "Select an account you administer, or create one in admin first."
        elif action == "reject":
            rec.status = PairingStatus.REJECTED
            rec.account = account
            rec.save(update_fields=["status", "account"])
            return redirect("pair", pk=rec.id)
        elif action == "approve":
            try:
                approve_gateway(rec, account)
            except ValueError as exc:
                error = str(exc)
            else:
                return redirect("pair", pk=rec.id)
        else:
            return HttpResponseBadRequest("unknown action")
    return render(
        request,
        "core/pair.html",
        {
            "pairing": rec,
            "accounts": accounts,
            "error": error,
        },
    )
