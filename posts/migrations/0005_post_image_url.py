from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("posts", "0004_post_permalink_and_deleted_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="post",
            name="image_url",
            field=models.URLField(blank=True, max_length=2000),
        ),
    ]
