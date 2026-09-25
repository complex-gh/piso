from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from .models import Account, AccountMembership
from .models.identity import MembershipRole
from .pairing import _accounts_for_user


@login_required
def setup_account(request):
    if _accounts_for_user(request.user):
        return redirect("home")
    if not request.user.is_staff:
        return render(
            request,
            "core/setup.html",
            {"error": "An administrator must create the first account (staff login)."},
        )
    error = ""
    if request.method == "POST":
        name = (request.POST.get("name") or "").strip()[:200]
        if not name:
            error = "Account name is required."
        else:
            account = Account.objects.create(name=name)
            AccountMembership.objects.create(
                account=account, user=request.user, role=MembershipRole.OWNER
            )
            return redirect("home")
    return render(request, "core/setup.html", {"error": error})
