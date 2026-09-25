from django.contrib import admin
from django.urls import path

from core.views import health

urlpatterns = [
    path("health/", health, name="health"),
    path("admin/", admin.site.urls),
]
