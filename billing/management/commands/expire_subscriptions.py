from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from accounts.emails import send_renewal_reminder_email
from billing.entitlements import is_exempt
from billing.models import Subscription


class Command(BaseCommand):
    help = "Send subscription renewal reminders."

    def handle(self, *args, **options):
        now = timezone.now()
        for days_remaining in (7, 1):
            target_date = (now + timedelta(days=days_remaining)).date()
            subscriptions = Subscription.objects.select_related("user").filter(
                current_period_end__date=target_date,
                status__in=[Subscription.Status.ACTIVE, Subscription.Status.TRIALING],
            )
            for subscription in subscriptions.iterator():
                if not is_exempt(subscription.user):
                    send_renewal_reminder_email(
                        subscription.user,
                        days_remaining,
                        subscription.current_period_end,
                    )