from datetime import timedelta

from django.conf import settings
from django.db import migrations, models
from django.utils import timezone


def backfill_lifecycle_fields(apps, schema_editor):
    Subscription = apps.get_model("billing", "Subscription")
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    alias = schema_editor.connection.alias
    migration_time = timezone.now()

    for subscription in Subscription.objects.using(alias).all().iterator():
        user = User.objects.using(alias).filter(pk=subscription.user_id).first()
        if not user:
            continue
        changed_fields = []
        if subscription.started_at is None:
            subscription.started_at = (
                migration_time
                if subscription.status == "trialing"
                else user.date_joined or migration_time
            )
            changed_fields.append("started_at")
        if subscription.status == "trialing" and subscription.trial_ends_at is None:
            subscription.trial_ends_at = migration_time + timedelta(days=14)
            changed_fields.append("trial_ends_at")
        if subscription.next_billing_date is None and subscription.current_period_end:
            subscription.next_billing_date = subscription.current_period_end
            changed_fields.append("next_billing_date")
        if subscription.status == "canceled" and subscription.canceled_at is None:
            subscription.canceled_at = subscription.updated_at
            changed_fields.append("canceled_at")
        if changed_fields:
            subscription.save(using=alias, update_fields=changed_fields)


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="subscription",
            name="started_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="subscription",
            name="trial_ends_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="subscription",
            name="cancel_at_period_end",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="subscription",
            name="canceled_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="subscription",
            name="last_payment_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="subscription",
            name="next_billing_date",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(backfill_lifecycle_fields, migrations.RunPython.noop),
    ]