from datetime import timedelta

from django.conf import settings
from django.db import migrations
from django.utils import timezone


def backfill_profiles_and_subscriptions(apps, schema_editor):
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    UserProfile = apps.get_model("accounts", "UserProfile")
    Subscription = apps.get_model("billing", "Subscription")
    alias = schema_editor.connection.alias

    UserProfile.objects.using(alias).bulk_create(
        [
            UserProfile(user_id=user_id, email_verified=True)
            for user_id in User.objects.using(alias)
            .exclude(profile__isnull=False)
            .values_list("pk", flat=True)
        ],
        ignore_conflicts=True,
    )

    now = timezone.now()
    users = User.objects.using(alias).values_list("pk", "date_joined")
    joined_by_id = dict(users)
    for subscription in Subscription.objects.using(alias).all().iterator():
        started_at = subscription.started_at or joined_by_id.get(subscription.user_id) or now
        update_fields = []
        if subscription.started_at is None:
            subscription.started_at = started_at
            update_fields.append("started_at")
        if subscription.trial_ends_at is None:
            subscription.trial_ends_at = started_at + timedelta(days=14)
            update_fields.append("trial_ends_at")
        if update_fields:
            subscription.save(using=alias, update_fields=update_fields)


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0004_userprofile"),
        ("billing", "0002_subscription_lifecycle"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(backfill_profiles_and_subscriptions, migrations.RunPython.noop),
    ]