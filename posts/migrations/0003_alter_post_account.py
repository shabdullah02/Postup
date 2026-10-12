import django.db.models.deletion
from django.db import migrations, models

from accounts.utils.crypto import encrypt_token


def migrate_social_accounts(apps, schema_editor):
    LegacySocialAccount = apps.get_model("socials", "SocialAccount")
    NewSocialAccount = apps.get_model("accounts", "SocialAccount")
    Post = apps.get_model("posts", "Post")

    for legacy in LegacySocialAccount.objects.all():
        new_account, _ = NewSocialAccount.objects.get_or_create(
            user_id=legacy.user_id,
            provider="facebook",
            external_id=legacy.external_id,
            defaults={
                "name": legacy.name,
                "token_encrypted": encrypt_token(legacy.token),
                "status": "active",
                "scopes": "",
            },
        )
        Post.objects.filter(account_id=legacy.id).update(account_id=new_account.id)


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0001_initial"),
        ("posts", "0002_post_claimed_at"),
        ("socials", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(migrate_social_accounts, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="post",
            name="account",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                to="accounts.socialaccount",
            ),
        ),
    ]
