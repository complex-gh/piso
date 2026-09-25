import uuid

from django.contrib.postgres.fields import ArrayField
from django.db import models

from .access import Resource
from .identity import Account, Principal
from .work import ApprovalRequest


class BoosterSync(models.TextChoices):
    PENDING = "pending", "Pending"
    OK = "ok", "OK"
    STALE = "stale", "Stale"
    MISSING = "missing", "Missing"


class Booster(models.Model):
    """Metadata and ACL handle. AI Boost remains authoritative for content."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    resource = models.OneToOneField(Resource, on_delete=models.CASCADE, related_name="booster")
    ai_boost_id = models.CharField(max_length=80, blank=True)
    slug = models.SlugField(max_length=120, blank=True)
    version = models.CharField(max_length=32, blank=True)
    content_hash = models.CharField(max_length=128, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    sync_state = models.CharField(
        max_length=16, choices=BoosterSync.choices, default=BoosterSync.PENDING
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return self.slug or self.ai_boost_id or str(self.id)


class BoosterProposal(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="booster_proposals")
    curator = models.ForeignKey(
        Principal, on_delete=models.CASCADE, related_name="booster_proposals"
    )
    booster = models.ForeignKey(
        Booster, null=True, blank=True, on_delete=models.SET_NULL, related_name="proposals"
    )
    proposed_change = models.JSONField(default=dict)
    approval_request = models.ForeignKey(
        ApprovalRequest,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="booster_proposals",
    )
    created_at = models.DateTimeField(auto_now_add=True)


class MemoryNote(models.Model):
    """Hammerspace: unstructured notes. Advisory, not directives."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="memory_notes")
    author = models.ForeignKey(Principal, on_delete=models.CASCADE, related_name="memory_notes")
    title = models.CharField(max_length=300, blank=True)
    body = models.TextField()
    tags = ArrayField(models.CharField(max_length=64), default=list, blank=True)
    provenance = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["account", "created_at"])]
