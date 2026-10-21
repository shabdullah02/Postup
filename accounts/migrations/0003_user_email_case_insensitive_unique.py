from django.conf import settings
from django.db import migrations


INDEX_NAME = "auth_user_email_ci_unique"


def create_email_index(apps, schema_editor):
    app_label, model_name = settings.AUTH_USER_MODEL.split(".")
    User = apps.get_model(app_label, model_name)
    connection = schema_editor.connection
    if connection.vendor not in {"sqlite", "postgresql"}:
        raise RuntimeError(
            "Case-insensitive email uniqueness migration currently supports SQLite and PostgreSQL."
        )

    table = schema_editor.quote_name(User._meta.db_table)
    email = schema_editor.quote_name("email")
    index = schema_editor.quote_name(INDEX_NAME)
    schema_editor.execute(
        f"CREATE UNIQUE INDEX {index} ON {table} (LOWER({email})) WHERE {email} <> ''"
    )


def drop_email_index(apps, schema_editor):
    if schema_editor.connection.vendor in {"sqlite", "postgresql"}:
        schema_editor.execute(f"DROP INDEX IF EXISTS {schema_editor.quote_name(INDEX_NAME)}")


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0002_alter_socialaccount_provider"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(create_email_index, drop_email_index),
    ]