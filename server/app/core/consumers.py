from channels.consumer import SyncConsumer
from channels.generic.websocket import AsyncJsonWebsocketConsumer


class GatewayConsumer(AsyncJsonWebsocketConsumer):
    """Persistent outbound socket from an enrolled gateway."""

    async def connect(self):
        await self.accept()
        await self.send_json({"type": "hello", "role": "gateway"})

    async def receive_json(self, content, **kwargs):
        msg_type = content.get("type")
        if msg_type == "ping":
            await self.send_json({"type": "pong"})
            return
        if msg_type == "heartbeat":
            await self.send_json({"type": "ack", "event": "heartbeat"})
            return
        await self.send_json({"type": "error", "error": "unknown_type"})


class ClientConsumer(AsyncJsonWebsocketConsumer):
    """Dashboard / phone web client socket."""

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


class BackgroundConsumer(SyncConsumer):
    """Channels worker entrypoint for fan-out and retries."""

    def event_dispatch(self, message):
        return None
