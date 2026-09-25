from django.urls import path

from .consumers import ClientConsumer, GatewayConsumer

websocket_urlpatterns = [
    path("ws/gateway/", GatewayConsumer.as_asgi()),
    path("ws/client/", ClientConsumer.as_asgi()),
]
