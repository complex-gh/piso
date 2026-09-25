from django.contrib.auth import logout
from django.http import JsonResponse
from django.shortcuts import redirect, render

from .models.events import PairingStatus
from .models import PairingRequest
from .pairing import _accounts_for_user


def health(request):
    return JsonResponse({"status": "ok"})


def home(request):
    pending = []
    accounts = []
    if request.user.is_authenticated:
        accounts = _accounts_for_user(request.user)
        if accounts:
            pending = list(
                PairingRequest.objects.filter(
                    status=PairingStatus.PENDING,
                ).order_by("-created_at")[:20]
            )
    return render(
        request,
        "core/home.html",
        {"pending": pending, "accounts": accounts},
    )


def logout_view(request):
    logout(request)
    return redirect("home")
