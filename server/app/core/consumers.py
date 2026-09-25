from channels.consumer import SyncConsumer
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.utils import timezone

from .crypto import AUTH_SCHEME
from .gateway_auth import verify_bound_auth
from .models import Principal
from .models.identity import PrincipalKind, PrincipalStatus

WS_PATH = "/ws/gateway/"


class GatewayConsumer(AsyncJsonWebsocketConsumer):
    """Persistent outbound socket from an enrolled gateway."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.principal_id = None

    async def connect(self):
        await self.accept()
        await self.send_json({"type": "hello", "role": "gateway", "auth": AUTH_SCHEME})

    async def receive_json(self, content, **kwargs):
        msg_type = content.get("type")
        if msg_type == "auth":
            principal = await self._authenticate(content)
            if principal is None:
                await self.send_json({"type": "error", "error": "unauthorized"})
                await self.close(code=4401)
                return
            self.principal_id = str(principal.id)
            await self.channel_layer.group_add(
                f"gateway.{self.principal_id}", self.channel_name
            )
            await self.send_json(
                {
                    "type": "ready",
                    "principal_id": self.principal_id,
                    "sync": "envelopes",
                }
            )
            return
        if self.principal_id is None:
            await self.send_json({"type": "error", "error": "auth_required"})
            return
        if msg_type == "ping":
            await self.send_json({"type": "pong"})
            return
        if msg_type == "heartbeat":
            await self.send_json({"type": "ack", "event": "heartbeat"})
            return
        await self.send_json({"type": "error", "error": "unknown_type"})

    async def disconnect(self, code):
        if self.principal_id:
            await self.channel_layer.group_discard(
                f"gateway.{self.principal_id}", self.channel_name
            )

    async def envelopes_changed(self, event):
        await self.send_json({"type": "envelopes_changed"})

    @database_sync_to_async
    def _authenticate(self, content):
        pubkey = (content.get("pubkey") or "").strip().lower()
        timestamp = str(content.get("timestamp") or "").strip()
        nonce = (content.get("nonce") or "").strip()
        signature = (content.get("signature") or "").strip()
        if not verify_bound_auth(pubkey, timestamp, nonce, signature, "WS", WS_PATH, b""):
            return None
        try:
            principal = Principal.objects.get(
                nostr_pubkey=pubkey,
                kind=PrincipalKind.GATEWAY,
                status=PrincipalStatus.ACTIVE,
            )
        except Principal.DoesNotExist:
            return None
        principal.last_seen_at = timezone.now()
        principal.save(update_fields=["last_seen_at"])
        return principal


class BackgroundConsumer(SyncConsumer):
    def event_dispatch(self, message):
        return None


class ClientConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        if self.scope["user"].is_anonymous:
            await self.close(code=4401)
            return
        await self.accept()
        await self.send_json({"type": "hello", "role": "client"})

    async def receive_json(self, content, **kwargs):
        if content.get("type") == "ping":
            await self.send_json({"type": "pong"})
            return
        await self.send_json({"type": "error", "error": "unknown_type"})
