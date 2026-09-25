import uuid

from django.db import models

from .fleet import Worker
from .identity import Account, Principal


class DirectiveStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    PAUSED = "paused", "Paused"
    BLOCKED = "blocked", "Blocked"
    COMPLETE = "complete", "Complete"
    CANCELLED = "cancelled", "Cancelled"


class Directive(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="directives")
    title = models.CharField(max_length=300)
    body = models.TextField()
    status = models.CharField(
        max_length=16, choices=DirectiveStatus.choices, default=DirectiveStatus.ACTIVE
    )
    priority = models.IntegerField(default=0)
    completion_criteria = models.TextField(blank=True)
    created_by = models.ForeignKey(
        Principal, null=True, blank=True, on_delete=models.SET_NULL, related_name="directives"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return self.title


class StandingOrder(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="standing_orders")
    title = models.CharField(max_length=300)
    trigger = models.CharField(max_length=200, blank=True)
    instructions = models.TextField()
    limits = models.JSONField(default=dict, blank=True)
    enabled = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        Principal, null=True, blank=True, on_delete=models.SET_NULL, related_name="standing_orders"
    )
    created_at = models.DateTimeField(auto_now_add=True)


class Plan(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    directive = models.ForeignKey(Directive, on_delete=models.CASCADE, related_name="plans")
    version = models.PositiveIntegerField(default=1)
    status = models.CharField(
        max_length=16, choices=DirectiveStatus.choices, default=DirectiveStatus.ACTIVE
    )
    body = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["directive", "version"], name="uniq_plan_version")
        ]


class TaskStatus(models.TextChoices):
    OPEN = "open", "Open"
    CLAIMED = "claimed", "Claimed"
    DONE = "done", "Done"
    FAILED = "failed", "Failed"
    RELEASED = "released", "Released"


class Task(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    plan = models.ForeignKey(
        Plan, null=True, blank=True, on_delete=models.SET_NULL, related_name="tasks"
    )
    directive = models.ForeignKey(Directive, on_delete=models.CASCADE, related_name="tasks")
    title = models.CharField(max_length=300)
    body = models.TextField(blank=True)
    status = models.CharField(max_length=16, choices=TaskStatus.choices, default=TaskStatus.OPEN)
    assigned_worker = models.ForeignKey(
        Worker, null=True, blank=True, on_delete=models.SET_NULL, related_name="tasks"
    )
    dependencies = models.ManyToManyField("self", blank=True, symmetrical=False)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    budget = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["status", "lease_expires_at"])]


class ApprovalStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"
    CANCELLED = "cancelled", "Cancelled"
    EXPIRED = "expired", "Expired"


class ApprovalRequest(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="approval_requests")
    requested_by = models.ForeignKey(
        Principal, on_delete=models.CASCADE, related_name="approval_requests"
    )
    action_type = models.CharField(max_length=80)
    target_id = models.CharField(max_length=80, blank=True)
    payload_hash = models.CharField(max_length=128, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=16, choices=ApprovalStatus.choices, default=ApprovalStatus.PENDING
    )
    expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["account", "status"])]


class Approval(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.ForeignKey(ApprovalRequest, on_delete=models.CASCADE, related_name="decisions")
    approver = models.ForeignKey(Principal, on_delete=models.CASCADE, related_name="approvals")
    decision = models.CharField(max_length=16, choices=ApprovalStatus.choices)
    nostr_signature = models.TextField(blank=True)
    decided_at = models.DateTimeField(auto_now_add=True)


class HumanActionStatus(models.TextChoices):
    REQUESTED = "requested", "Requested"
    ACKNOWLEDGED = "acknowledged", "Acknowledged"
    COMPLETED = "completed", "Completed"
    REJECTED = "rejected", "Rejected"
    FAILED = "failed", "Failed"
    DEFERRED = "deferred", "Deferred"
    EXPIRED = "expired", "Expired"


class HumanAction(models.Model):
    """Work the system cannot perform: DNS, domain proof, publish, new machine."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="human_actions")
    requested_by = models.ForeignKey(
        Principal, on_delete=models.CASCADE, related_name="human_actions"
    )
    category = models.CharField(max_length=64)
    title = models.CharField(max_length=300)
    instructions = models.TextField()
    verification = models.TextField(blank=True)
    blocks_task = models.ForeignKey(
        Task, null=True, blank=True, on_delete=models.SET_NULL, related_name="human_actions"
    )
    status = models.CharField(
        max_length=16, choices=HumanActionStatus.choices, default=HumanActionStatus.REQUESTED
    )
    evidence = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
