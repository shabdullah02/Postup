from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("posts", "0003_alter_post_account"),
    ]

    operations = [
        migrations.AddField(
            model_name="post",
            name="permalink_url",
            field=models.URLField(blank=True, max_length=500, null=True),
        ),
        migrations.AlterField(
            model_name="post",
            name="status",
            field=models.CharField(
                choices=[
                    ("draft", "Draft"),
                    ("scheduled", "Scheduled"),
                    ("publishing", "Publishing"),
                    ("posted", "Posted"),
                    ("failed", "Failed"),
                    ("deleted_on_facebook", "Deleted on Facebook"),
                ],
                default="draft",
                max_length=20,
            ),
        ),
    ]
