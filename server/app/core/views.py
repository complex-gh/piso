from django.contrib.auth import logout
from django.http import JsonResponse
from django.shortcuts import redirect, render


def health(request):
    return JsonResponse({"status": "ok"})


def home(request):
    return render(request, "core/home.html")


def logout_view(request):
    logout(request)
    return redirect("home")
