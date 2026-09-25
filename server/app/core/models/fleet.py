import uuid

from django.db import models

from .identity import Account, Principal


class Project(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="projects")
    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=80)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["account", "slug"], name="uniq_account_project_slug")
        ]

    def __str__(self) -> str:
        return self.slug


class Repository(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="repositories")
    remote_ref = models.CharField(max_length=500)
    default_branch = models.CharField(max_length=200, default="main")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.remote_ref


class Gateway(models.Model):
    """Trusted relay. Decrypts authorized secrets; never a planner."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    principal = models.OneToOneField(Principal, on_delete=models.CASCADE, related_name="gateway")
    machine_name = models.CharField(max_length=200, blank=True)
    endpoints = models.JSONField(
        default=list,
        blank=True,
        help_text="Reachable worker API URLs, e.g. Docker DNS, VPC IP, or server relay.",
    )
    capabilities = models.JSONField(default=list, blank=True)
    approval_policy = models.JSONField(default=dict, blank=True)
    last_heartbeat_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.machine_name or str(self.principal)


class WorkerHull(models.TextChoices):
    LOCAL_MODULE = "local_module", "Local module"
    CLOUD_SHIP = "cloud_ship", "Cloud ship"


class WorkerRuntime(models.TextChoices):
    STARTING = "starting", "Starting"
    READY = "ready", "Ready"
    BUSY = "busy", "Busy"
    STOPPED = "stopped", "Stopped"
    FAILED = "failed", "Failed"


class Worker(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    principal = models.OneToOneField(Principal, on_delete=models.CASCADE, related_name="worker")
    gateway = models.ForeignKey(Gateway, on_delete=models.CASCADE, related_name="workers")
    project = models.ForeignKey(
        Project, null=True, blank=True, on_delete=models.SET_NULL, related_name="workers"
    )
    hull = models.CharField(max_length=32, choices=WorkerHull.choices, default=WorkerHull.LOCAL_MODULE)
    runtime_state = models.CharField(
        max_length=16, choices=WorkerRuntime.choices, default=WorkerRuntime.STARTING
    )
    container_ref = models.CharField(max_length=200, blank=True)
    capability_manifest = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return str(self.principal)


class InfraKind(models.TextChoices):
    APPLICATION = "application", "Application"
    SERVICE = "service", "Service"
    ENVIRONMENT = "environment", "Environment"
    SERVER = "server", "Server"
    DATABASE = "database", "Database"
    STORAGE = "storage", "Storage"
    DOMAIN = "domain", "Domain"
    DNS_RECORD = "dns_record", "DNS record"
    CERTIFICATE = "certificate", "Certificate"
    DEPLOYMENT = "deployment", "Deployment"


class InfraResource(models.Model):
    """Reconciled inventory. Git remains desired state where practical."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="infra_resources")
    project = models.ForeignKey(
        Project, null=True, blank=True, on_delete=models.SET_NULL, related_name="infra_resources"
    )
    kind = models.CharField(max_length=32, choices=InfraKind.choices)
    name = models.CharField(max_length=200)
    environment = models.CharField(max_length=32, blank=True)
    provider = models.CharField(max_length=80, blank=True)
    external_id = models.CharField(max_length=200, blank=True)
    desired_state = models.JSONField(default=dict, blank=True)
    observed_state = models.JSONField(default=dict, blank=True)
    endpoint = models.CharField(max_length=500, blank=True)
    cost_cents_monthly = models.IntegerField(null=True, blank=True)
    credential_resource = models.ForeignKey(
        "Resource",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="infra_bindings",
        help_text="Encrypted credential used to manage this resource. Never plaintext.",
    )
    expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["account", "kind", "environment"])]

    def __str__(self) -> str:
        return f"{self.kind}:{self.name}"
