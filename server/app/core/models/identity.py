import uuid

from django.conf import settings
from django.db import models


class Account(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class PrincipalKind(models.TextChoices):
    USER = "user", "User"
    WEB_CLIENT = "web_client", "Web client"
    GATEWAY = "gateway", "Gateway"
    WORKER = "worker", "Worker"
    MONITOR = "monitor", "Monitor"
    CURATOR = "curator", "Booster curator"
    LOCUTUS = "locutus", "Locutus"


class PrincipalStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    ACTIVE = "active", "Active"
    OFFLINE = "offline", "Offline"
    REVOKED = "revoked", "Revoked"


class Principal(models.Model):
    """Nostr-identified actor. Private keys never enter this database."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="principals")
    kind = models.CharField(max_length=32, choices=PrincipalKind.choices)
    nostr_pubkey = models.CharField(max_length=128, unique=True)
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="children",
        help_text="Workers point at their gateway principal.",
    )
    name = models.CharField(max_length=200, blank=True)
    status = models.CharField(
        max_length=16, choices=PrincipalStatus.choices, default=PrincipalStatus.PENDING
    )
    metadata = models.JSONField(default=dict, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["account", "kind"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self) -> str:
        return self.name or f"{self.kind}:{self.nostr_pubkey[:12]}"


class KeyPurpose(models.TextChoices):
    SIGNING = "signing", "Signing"
    ENCRYPTION = "encryption", "Encryption"


class PrincipalKey(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    principal = models.ForeignKey(Principal, on_delete=models.CASCADE, related_name="keys")
    purpose = models.CharField(max_length=16, choices=KeyPurpose.choices)
    public_key = models.CharField(max_length=256)
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["principal", "purpose", "version"],
                name="uniq_principal_key_version",
            )
        ]


class MembershipRole(models.TextChoices):
    OWNER = "owner", "Owner"
    ADMIN = "admin", "Admin"
    MEMBER = "member", "Member"


class AccountMembership(models.Model):
    """Links a Django login to an account and optional web-client principal."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="technocore_memberships"
    )
    role = models.CharField(max_length=16, choices=MembershipRole.choices, default=MembershipRole.MEMBER)
    principal = models.OneToOneField(
        Principal,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="membership",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["account", "user"], name="uniq_account_user")
        ]
