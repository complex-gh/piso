from django.contrib import admin

from .models import (
    Account,
    AccountMembership,
    ApprovalRequest,
    Booster,
    Directive,
    Event,
    Gateway,
    Grant,
    HumanAction,
    MemoryNote,
    PairingRequest,
    Principal,
    Project,
    Resource,
    Task,
    Worker,
)


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = ("name", "created_at")


@admin.register(Principal)
class PrincipalAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "status", "account", "nostr_pubkey")
    list_filter = ("kind", "status")
    search_fields = ("name", "nostr_pubkey")


@admin.register(AccountMembership)
class AccountMembershipAdmin(admin.ModelAdmin):
    list_display = ("account", "user", "role")


@admin.register(Gateway)
class GatewayAdmin(admin.ModelAdmin):
    list_display = ("machine_name", "principal", "last_heartbeat_at")


@admin.register(Worker)
class WorkerAdmin(admin.ModelAdmin):
    list_display = ("principal", "gateway", "hull", "runtime_state")
    list_filter = ("hull", "runtime_state")


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("slug", "name", "account")


@admin.register(Resource)
class ResourceAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "status", "account")
    list_filter = ("kind", "status")


@admin.register(Grant)
class GrantAdmin(admin.ModelAdmin):
    list_display = ("resource", "principal", "effect")


@admin.register(Directive)
class DirectiveAdmin(admin.ModelAdmin):
    list_display = ("title", "status", "account")
    list_filter = ("status",)


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ("title", "status", "assigned_worker", "lease_expires_at")
    list_filter = ("status",)


@admin.register(ApprovalRequest)
class ApprovalRequestAdmin(admin.ModelAdmin):
    list_display = ("action_type", "status", "account", "created_at")
    list_filter = ("status", "action_type")


@admin.register(HumanAction)
class HumanActionAdmin(admin.ModelAdmin):
    list_display = ("title", "category", "status", "account")
    list_filter = ("status", "category")


@admin.register(Booster)
class BoosterAdmin(admin.ModelAdmin):
    list_display = ("slug", "ai_boost_id", "version", "sync_state")


@admin.register(MemoryNote)
class MemoryNoteAdmin(admin.ModelAdmin):
    list_display = ("title", "author", "account", "created_at")
    search_fields = ("title", "body")


@admin.register(PairingRequest)
class PairingRequestAdmin(admin.ModelAdmin):
    list_display = ("kind", "name", "status", "device_pubkey", "expires_at")
    list_filter = ("kind", "status")


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ("type", "account", "actor", "created_at")
    list_filter = ("type",)
    readonly_fields = ("encrypted_payload",)
