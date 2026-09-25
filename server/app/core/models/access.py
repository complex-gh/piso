import uuid

from django.contrib.postgres.fields import ArrayField
from django.db import models

from .identity import Account, Principal


class ResourceKind(models.TextChoices):
    SECRET = "secret", "Secret"
    BOOSTER = "booster", "Booster"
    COLLECTION = "collection", "Collection"
    REPOSITORY = "repository", "Repository"
    MCP_CONNECTION = "mcp_connection", "MCP connection"
    MEMORY = "memory", "Hammerspace"


class ResourceStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    DISABLED = "disabled", "Disabled"
    REVOKED = "revoked", "Revoked"


class Resource(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="resources")
    kind = models.CharField(max_length=32, choices=ResourceKind.choices)
    owner = models.ForeignKey(
        Principal, null=True, blank=True, on_delete=models.SET_NULL, related_name="owned_resources"
    )
    name = models.CharField(max_length=200)
    metadata = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=16, choices=ResourceStatus.choices, default=ResourceStatus.ACTIVE
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["account", "kind"])]

    def __str__(self) -> str:
        return f"{self.kind}:{self.name}"


class GrantEffect(models.TextChoices):
    ALLOW = "allow", "Allow"
    DENY = "deny", "Deny"


class Grant(models.Model):
    """Effective access is deny-wins intersection of account, gateway, and worker grants."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    resource = models.ForeignKey(Resource, on_delete=models.CASCADE, related_name="grants")
    principal = models.ForeignKey(Principal, on_delete=models.CASCADE, related_name="grants")
    actions = ArrayField(models.CharField(max_length=32), default=list, blank=True)
    effect = models.CharField(max_length=8, choices=GrantEffect.choices, default=GrantEffect.ALLOW)
    constraints = models.JSONField(default=dict, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    granted_by = models.ForeignKey(
        Principal,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="grants_given",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["principal", "effect"])]


class CollectionMember(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    collection = models.ForeignKey(
        Resource, on_delete=models.CASCADE, related_name="collection_members"
    )
    resource = models.ForeignKey(
        Resource, on_delete=models.CASCADE, related_name="in_collections"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["collection", "resource"], name="uniq_collection_resource"
            )
        ]


class EncryptedObject(models.Model):
    """Ciphertext only. Workers and this server cannot decrypt."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    resource = models.OneToOneField(
        Resource, on_delete=models.CASCADE, related_name="encrypted_object"
    )
    ciphertext = models.BinaryField()
    nonce = models.BinaryField()
    algorithm = models.CharField(max_length=64, default="xchacha20poly1305")
    content_version = models.PositiveIntegerField(default=1)
    content_hash = models.CharField(max_length=128)
    updated_at = models.DateTimeField(auto_now=True)


class KeyEnvelope(models.Model):
    """Per-recipient wrap of the object data key. Recipients are clients or gateways."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    resource = models.ForeignKey(Resource, on_delete=models.CASCADE, related_name="envelopes")
    content_version = models.PositiveIntegerField()
    recipient = models.ForeignKey(
        Principal, on_delete=models.CASCADE, related_name="key_envelopes"
    )
    wrapped_data_key = models.BinaryField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["resource", "content_version", "recipient"],
                name="uniq_envelope_recipient_version",
            )
        ]
