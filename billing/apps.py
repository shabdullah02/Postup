from django.apps import AppConfig
from importlib import import_module


class BillingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "billing"

    def ready(self):
        import_module("billing.signals")
        import_module("billing.schema")