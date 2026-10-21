from django.contrib.auth import get_user_model
from django.db.models.signals import post_save
from django.dispatch import receiver

from billing.models import Subscription


@receiver(post_save, sender=get_user_model())
def create_user_subscription(sender, instance, created, **kwargs):
    if created:
        Subscription.objects.get_or_create(
            user=instance,
            defaults={
                "plan": Subscription.Plan.FREE,
                "status": Subscription.Status.TRIALING,
            },
        )