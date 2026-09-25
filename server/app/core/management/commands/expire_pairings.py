from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import PairingRequest
from core.models.events import PairingStatus


class Command(BaseCommand):
    help = "Expire pending pairing requests past their deadline."

    def handle(self, *args, **options):
        n = PairingRequest.objects.filter(
            status=PairingStatus.PENDING,
            expires_at__lte=timezone.now(),
        ).update(status=PairingStatus.EXPIRED)
        self.stdout.write(f"expired {n} pairing request(s)")
