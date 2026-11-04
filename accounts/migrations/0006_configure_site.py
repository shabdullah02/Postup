from django.conf import settings
from django.db import migrations


def configure_site(apps, schema_editor):
    Site = apps.get_model("sites", "Site")
    Site.objects.update_or_create(
        pk=settings.SITE_ID,
        defaults={"domain": settings.SITE_DOMAIN, "name": "Postup"},
    )


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0005_backfill_profile_and_subscription_lifecycle"),
        ("sites", "0001_initial"),
    ]

    operations = [migrations.RunPython(configure_site, migrations.RunPython.noop)]