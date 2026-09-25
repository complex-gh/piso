import base64

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET

from .gateway_auth import require_gateway
from .models import EncryptedObject, KeyEnvelope


@csrf_exempt
@require_GET
@require_gateway
def list_envelopes(request):
    principal = request.gateway_principal
    rows = (
        KeyEnvelope.objects.filter(recipient=principal)
        .select_related("resource", "resource__encrypted_object")
        .order_by("resource_id", "content_version")
    )
    objects = []
    for env in rows:
        resource = env.resource
        try:
            blob = resource.encrypted_object
        except EncryptedObject.DoesNotExist:
            continue
        if blob.content_version != env.content_version:
            continue
        objects.append(
            {
                "resource_id": str(resource.id),
                "name": resource.name,
                "kind": resource.kind,
                "metadata": resource.metadata or {},
                "algorithm": blob.algorithm,
                "content_version": blob.content_version,
                "content_hash": blob.content_hash,
                "ciphertext": base64.b64encode(bytes(blob.ciphertext)).decode(),
                "nonce": base64.b64encode(bytes(blob.nonce)).decode(),
                "wrapped_data_key": base64.b64encode(bytes(env.wrapped_data_key)).decode(),
            }
        )
    return JsonResponse({"objects": objects})
