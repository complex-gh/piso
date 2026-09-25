import base64
import binascii
import json
import uuid

from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .gateway_auth import gateway_principal_from_request
from .models import EncryptedObject, Event, KeyEnvelope, Resource
from .models.access import ResourceKind, ResourceStatus
from .models.identity import KeyPurpose, Principal, PrincipalKind, PrincipalStatus
from .notify import notify_gateway_envelopes
from .pairing import _accounts_for_user

ENVELOPE_ALG = "chacha20poly1305-v1"
WRAP_LEN = 1 + 32 + 12 + 32 + 16  # version + eph_pub + nonce + dek + tag


def _json_body(request):
    if not request.body:
        return {}
    try:
        return json.loads(request.body.decode())
    except json.JSONDecodeError:
        return None


def _account_for_request(request):
    gw = gateway_principal_from_request(request)
    if gw is not None:
        request.gateway_principal = gw
        return gw.account, gw
    if request.user.is_authenticated:
        memberships = _accounts_for_user(request.user)
        if memberships:
            return memberships[0].account, None
    return None, None


def _b64(data: str, field: str) -> bytes:
    try:
        return base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"invalid base64 {field}") from exc


def _gateway_row(principal: Principal) -> dict:
    enc = (
        principal.keys.filter(purpose=KeyPurpose.ENCRYPTION, revoked_at__isnull=True)
        .order_by("-version")
        .first()
    )
    return {
        "principal_id": str(principal.id),
        "name": principal.name,
        "status": principal.status,
        "encryption_pubkey": enc.public_key if enc else "",
        "fingerprint": (principal.nostr_pubkey or "")[:12],
    }


@csrf_exempt
@require_http_methods(["GET"])
def list_gateways(request):
    account, _ = _account_for_request(request)
    if account is None:
        return JsonResponse({"error": "unauthorized"}, status=401)
    principals = Principal.objects.filter(
        account=account,
        kind=PrincipalKind.GATEWAY,
        status=PrincipalStatus.ACTIVE,
    ).prefetch_related("keys")
    return JsonResponse({"gateways": [_gateway_row(p) for p in principals]})


def _secret_meta(resource: Resource) -> dict:
    recipients = list(
        resource.envelopes.filter(content_version=resource.encrypted_object.content_version).values_list(
            "recipient_id", flat=True
        )
    )
    blob = resource.encrypted_object
    return {
        "resource_id": str(resource.id),
        "name": resource.name,
        "metadata": resource.metadata or {},
        "algorithm": blob.algorithm,
        "content_version": blob.content_version,
        "content_hash": blob.content_hash,
        "recipients": [str(r) for r in recipients],
    }


@csrf_exempt
@require_http_methods(["GET", "POST"])
def secrets_collection(request):
    if request.method == "GET":
        return list_secrets(request)
    return create_secret(request)


def list_secrets(request):
    account, _ = _account_for_request(request)
    if account is None:
        return JsonResponse({"error": "unauthorized"}, status=401)
    rows = Resource.objects.filter(
        account=account, kind=ResourceKind.SECRET, status=ResourceStatus.ACTIVE
    ).select_related("encrypted_object")
    out = []
    for r in rows:
        try:
            out.append(_secret_meta(r))
        except EncryptedObject.DoesNotExist:
            continue
    return JsonResponse({"secrets": out})


def create_secret(request):
    account, actor = _account_for_request(request)
    if account is None:
        return JsonResponse({"error": "unauthorized"}, status=401)
    body = _json_body(request)
    if body is None:
        return JsonResponse({"error": "invalid json"}, status=400)
    if "value" in body or "plaintext" in body:
        return JsonResponse({"error": "plaintext must not be sent to the server"}, status=400)
    try:
        wraps = _parse_wraps(account, body.get("wraps") or [])
        nonce = _b64(body.get("nonce") or "", "nonce")
        ciphertext = _b64(body.get("ciphertext") or "", "ciphertext")
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    alg = (body.get("algorithm") or "").strip()
    if alg != ENVELOPE_ALG:
        return JsonResponse({"error": "unsupported algorithm"}, status=400)
    name = (body.get("name") or "").strip()[:200]
    if not name:
        return JsonResponse({"error": "name required"}, status=400)
    metadata = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
    content_hash = (body.get("content_hash") or "").strip()
    with transaction.atomic():
        resource = Resource.objects.create(
            account=account,
            kind=ResourceKind.SECRET,
            owner=actor,
            name=name,
            metadata=metadata,
            status=ResourceStatus.ACTIVE,
        )
        EncryptedObject.objects.create(
            resource=resource,
            ciphertext=ciphertext,
            nonce=nonce,
            algorithm=alg,
            content_version=1,
            content_hash=content_hash,
        )
        _replace_wraps(resource, 1, wraps)
        Event.objects.create(
            account=account,
            actor=actor,
            type="secret.granted",
            subject_type="resource",
            subject_id=str(resource.id),
            public_metadata={"name": name, "recipients": [str(p.id) for p, _ in wraps]},
        )
        pids = [str(p.id) for p, _ in wraps]
        rid = str(resource.id)
    for pid in pids:
        notify_gateway_envelopes(pid)
    return JsonResponse({"resource_id": rid, "content_version": 1}, status=201)


def _parse_wraps(account, wraps_in):
    if not isinstance(wraps_in, list) or not wraps_in:
        raise ValueError("wraps required")
    out = []
    seen = set()
    for item in wraps_in:
        if not isinstance(item, dict):
            raise ValueError("invalid wrap")
        pid = item.get("principal_id") or ""
        try:
            uid = uuid.UUID(str(pid))
        except ValueError as exc:
            raise ValueError("invalid principal_id") from exc
        if uid in seen:
            raise ValueError("duplicate principal_id")
        seen.add(uid)
        try:
            principal = Principal.objects.get(
                id=uid,
                account=account,
                kind=PrincipalKind.GATEWAY,
                status=PrincipalStatus.ACTIVE,
            )
        except Principal.DoesNotExist as exc:
            raise ValueError("unknown gateway recipient") from exc
        blob = _b64(item.get("wrapped_data_key") or "", "wrapped_data_key")
        if len(blob) != WRAP_LEN:
            raise ValueError("invalid wrap blob length")
        out.append((principal, blob))
    return out


def _replace_wraps(resource, version, wraps):
    KeyEnvelope.objects.filter(resource=resource).delete()
    for principal, blob in wraps:
        KeyEnvelope.objects.create(
            resource=resource,
            content_version=version,
            recipient=principal,
            wrapped_data_key=blob,
        )


@csrf_exempt
@require_http_methods(["GET", "PUT"])
def secret_item(request, pk):
    account, actor = _account_for_request(request)
    if account is None:
        return JsonResponse({"error": "unauthorized"}, status=401)
    resource = get_object_or_404(
        Resource, pk=pk, account=account, kind=ResourceKind.SECRET
    )
    if request.method == "GET":
        try:
            return JsonResponse(_secret_meta(resource))
        except EncryptedObject.DoesNotExist:
            return JsonResponse({"error": "missing ciphertext"}, status=404)
    return update_secret(request, resource, account, actor)


def update_secret(request, resource, account, actor):
    body = _json_body(request)
    if body is None:
        return JsonResponse({"error": "invalid json"}, status=400)
    if "value" in body or "plaintext" in body:
        return JsonResponse({"error": "plaintext must not be sent to the server"}, status=400)
    try:
        wraps = _parse_wraps(account, body.get("wraps") or [])
        nonce = _b64(body.get("nonce") or "", "nonce")
        ciphertext = _b64(body.get("ciphertext") or "", "ciphertext")
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    alg = (body.get("algorithm") or "").strip()
    if alg != ENVELOPE_ALG:
        return JsonResponse({"error": "unsupported algorithm"}, status=400)
    with transaction.atomic():
        blob = resource.encrypted_object
        previous = list(
            resource.envelopes.filter(content_version=blob.content_version).values_list(
                "recipient_id", flat=True
            )
        )
        blob.content_version += 1
        blob.nonce = nonce
        blob.ciphertext = ciphertext
        blob.algorithm = alg
        blob.content_hash = (body.get("content_hash") or "").strip()
        blob.save()
        version = blob.content_version
        if isinstance(body.get("metadata"), dict):
            resource.metadata = body["metadata"]
            resource.save(update_fields=["metadata", "updated_at"])
        _replace_wraps(resource, version, wraps)
        notify_ids = {str(p.id) for p, _ in wraps} | {str(x) for x in previous}
        Event.objects.create(
            account=account,
            actor=actor,
            type="secret.updated",
            subject_type="resource",
            subject_id=str(resource.id),
            public_metadata={"content_version": version},
        )
        rid = str(resource.id)
    for pid in notify_ids:
        notify_gateway_envelopes(pid)
    return JsonResponse({"resource_id": rid, "content_version": version})


@csrf_exempt
@require_http_methods(["DELETE"])
def delete_envelope(request, pk, principal_id):
    account, _ = _account_for_request(request)
    if account is None:
        return JsonResponse({"error": "unauthorized"}, status=401)
    resource = get_object_or_404(
        Resource, pk=pk, account=account, kind=ResourceKind.SECRET
    )
    try:
        uid = uuid.UUID(str(principal_id))
    except ValueError:
        return JsonResponse({"error": "invalid principal_id"}, status=400)
    n, _ = KeyEnvelope.objects.filter(resource=resource, recipient_id=uid).delete()
    if n == 0:
        return JsonResponse({"error": "not found"}, status=404)
    notify_gateway_envelopes(str(uid))
    return JsonResponse({"ok": True})
