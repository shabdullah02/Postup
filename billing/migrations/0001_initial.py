import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def backfill_subscriptions(apps, schema_editor):
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    Subscription = apps.get_model("billing", "Subscription")
    Subscription.objects.using(schema_editor.connection.alias).bulk_create(
        [
            Subscription(
                user_id=user_id,
                plan="free",
                status="trialing",
            )
            for user_id in User.objects.using(schema_editor.connection.alias)
            .exclude(subscription__isnull=False)
            .values_list("pk", flat=True)
        ],
        ignore_conflicts=True,
    )


class Migration(migrations.Migration):
    initial = True

    dependencies = [migrations.swappable_dependency(settings.AUTH_USER_MODEL)]

    operations = [
        migrations.CreateModel(
            name="AdminAuditLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("target_identifier", models.CharField(max_length=255)),
                ("who_identifier", models.CharField(max_length=255)),
                ("action", models.CharField(max_length=100)),
                ("timestamp", models.DateTimeField(auto_now_add=True)),
                ("details", models.JSONField(blank=True, default=dict)),
                ("target", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="admin_audit_events", to=settings.AUTH_USER_MODEL)),
                ("who", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="admin_audit_actions", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-timestamp"]},
        ),
        migrations.CreateModel(
            name="StripeEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("event_id", models.CharField(max_length=255, unique=True)),
                ("event_type", models.CharField(max_length=255)),
                ("received_at", models.DateTimeField(auto_now_add=True)),
                ("processed_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={"ordering": ["-received_at"]},
        ),
        migrations.CreateModel(
            name="Subscription",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("plan", models.CharField(choices=[("free", "Free"), ("pro", "Pro"), ("agency", "Agency")], default="free", max_length=20)),
                ("status", models.CharField(choices=[("trialing", "Trialing"), ("active", "Active"), ("past_due", "Past due"), ("canceled", "Canceled"), ("suspended", "Suspended")], default="trialing", max_length=20)),
                ("current_period_end", models.DateTimeField(blank=True, null=True)),
                ("stripe_customer_id", models.CharField(blank=True, max_length=255)),
                ("stripe_subscription_id", models.CharField(blank=True, max_length=255)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("user", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="subscription", to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.RunPython(backfill_subscriptions, migrations.RunPython.noop),
    ]