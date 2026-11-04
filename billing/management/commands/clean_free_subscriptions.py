from django.core.management.base import BaseCommand
from django.db.models import Q

from billing.models import Subscription


class Command(BaseCommand):
    help = "Clear stale cancellation and billing-period fields from free subscriptions."

    def handle(self, *args, **options):
        updated = Subscription.objects.filter(plan=Subscription.Plan.FREE).filter(
            Q(cancel_at_period_end=True) | Q(current_period_end__isnull=False)
        ).update(
            cancel_at_period_end=False,
            current_period_end=None,
            next_billing_date=None,
        )
        self.stdout.write(f"Reset {updated} free subscription(s).")
