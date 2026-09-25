import uuid

from django.db import models

from .identity import Account, Principal


class PairingKind(models.TextChoices):
    WEB_CLIENT = "web_client", "Web client"
    GATEWAY = "gateway", "Gateway"
    WORKER = "worker", "Worker"


class PairingStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"
    EXPIRED = "expired", "Expired"


class PairingRequest(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(
        Account,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="pairing_requests",
        help_text="Filled when an authenticated client approves the request.",
    )
    kind = models.CharField(max_length=16, choices=PairingKind.choices)
    device_pubkey = models.CharField(max_length=128)
    name = models.CharField(max_length=200, blank=True)
    fingerprint = models.CharField(max_length=64, blank=True)
    capabilities = models.JSONField(default=list, blank=True)
    status = models.CharField(
        max_length=16, choices=PairingStatus.choices, default=PairingStatus.PENDING
    )
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["status", "expires_at"])]


class Event(models.Model):
    """Append-only signed coordination log. Current-state tables are projections."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="events")
    actor = models.ForeignKey(
        Principal, null=True, blank=True, on_delete=models.SET_NULL, related_name="events"
    )
    type = models.CharField(max_length=80)
    subject_type = models.CharField(max_length=80, blank=True)
    subject_id = models.CharField(max_length=80, blank=True)
    public_metadata = models.JSONField(default=dict, blank=True)
    encrypted_payload = models.BinaryField(null=True, blank=True)
    nostr_event_id = models.CharField(max_length=128, blank=True)
    signature = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["account", "created_at"]),
            models.Index(fields=["account", "type"]),
            models.Index(fields=["subject_type", "subject_id"]),
        ]
        ordering = ["-created_at"]
