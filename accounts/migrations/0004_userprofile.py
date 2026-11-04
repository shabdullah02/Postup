from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def backfill_user_profiles(apps, schema_editor):
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    UserProfile = apps.get_model("accounts", "UserProfile")
    alias = schema_editor.connection.alias
    UserProfile.objects.using(alias).bulk_create(
        [
            UserProfile(user_id=user_id)
            for user_id in User.objects.using(alias).values_list("pk", flat=True)
        ],
        ignore_conflicts=True,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0003_user_email_case_insensitive_unique"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="UserProfile",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("email_verified", models.BooleanField(default=False)),
                ("email_verified_at", models.DateTimeField(blank=True, null=True)),
                ("totp_secret_encrypted", models.BinaryField(blank=True, default=bytes)),
                ("totp_enabled", models.BooleanField(default=False)),
                ("recovery_codes", models.JSONField(blank=True, default=list)),
                ("user", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="profile", to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.RunPython(backfill_user_profiles, migrations.RunPython.noop),
    ]