from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0003_subscription_incomplete_status"),
    ]

    operations = [
        migrations.AlterField(
            model_name="subscription",
            name="stripe_customer_id",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AlterField(
            model_name="subscription",
            name="stripe_subscription_id",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name="subscription",
            name="payment_method_brand",
            field=models.CharField(blank=True, max_length=20),
        ),
        migrations.AddField(
            model_name="subscription",
            name="payment_method_last4",
            field=models.CharField(blank=True, max_length=4),
        ),
        migrations.AddField(
            model_name="subscription",
            name="payment_method_exp_month",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="subscription",
            name="payment_method_exp_year",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
    ]
