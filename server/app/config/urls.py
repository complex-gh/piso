from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path

from core.pairing import create_pairing, pair_page, pairing_status
from core.sync import list_envelopes
from core.views import health, home, logout_view

urlpatterns = [
    path("", home, name="home"),
    path("health/", health, name="health"),
    path("login/", auth_views.LoginView.as_view(template_name="core/login.html"), name="login"),
    path("logout/", logout_view, name="logout"),
    path("admin/", admin.site.urls),
    path("pair/<uuid:pk>/", pair_page, name="pair"),
    path("api/v1/pairing-requests", create_pairing),
    path("api/v1/pairing-requests/<uuid:pk>", pairing_status),
    path("api/v1/sync/secrets", list_envelopes),
]
