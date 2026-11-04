from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0002_subscription_lifecycle"),
    ]

    operations = [
        migrations.AlterField(
            model_name="subscription",
            name="status",
            field=models.CharField(
                choices=[
                    ("trialing", "Trialing"),
                    ("active", "Active"),
                    ("incomplete", "Incomplete"),
                    ("past_due", "Past due"),
                    ("canceled", "Canceled"),
                    ("suspended", "Suspended"),
                ],
                default="trialing",
                max_length=20,
            ),
        ),
    ]
