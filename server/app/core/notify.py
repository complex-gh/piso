from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer


def notify_gateway_envelopes(principal_id: str) -> None:
    layer = get_channel_layer()
    if layer is None:
        return
    async_to_sync(layer.group_send)(
        f"gateway.{principal_id}",
        {"type": "envelopes_changed"},
    )
