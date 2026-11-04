from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0004_mock_payment_method_summary"),
    ]

    operations = [
        migrations.RenameField(
            model_name="subscription",
            old_name="payment_method_brand",
            new_name="card_brand",
        ),
        migrations.AlterField(
            model_name="subscription",
            name="card_brand",
            field=models.CharField(blank=True, max_length=20, null=True),
        ),
        migrations.RenameField(
            model_name="subscription",
            old_name="payment_method_last4",
            new_name="card_last4",
        ),
        migrations.AlterField(
            model_name="subscription",
            name="card_last4",
            field=models.CharField(blank=True, max_length=4, null=True),
        ),
        migrations.RenameField(
            model_name="subscription",
            old_name="payment_method_exp_month",
            new_name="card_exp_month",
        ),
        migrations.RenameField(
            model_name="subscription",
            old_name="payment_method_exp_year",
            new_name="card_exp_year",
        ),
    ]
