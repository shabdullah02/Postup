from django.conf import settings
from django.db import models


class Subscription(models.Model):
    class Plan(models.TextChoices):
        FREE = "free", "Free"
        PRO = "pro", "Pro"
        AGENCY = "agency", "Agency"

    class Status(models.TextChoices):
        TRIALING = "trialing", "Trialing"
        ACTIVE = "active", "Active"
        INCOMPLETE = "incomplete", "Incomplete"
        PAST_DUE = "past_due", "Past due"
        CANCELED = "canceled", "Canceled"
        SUSPENDED = "suspended", "Suspended"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="subscription",
    )
    plan = models.CharField(max_length=20, choices=Plan.choices, default=Plan.FREE)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.TRIALING,
    )
    started_at = models.DateTimeField(null=True, blank=True)
    trial_ends_at = models.DateTimeField(null=True, blank=True)
    current_period_end = models.DateTimeField(null=True, blank=True)
    next_billing_date = models.DateTimeField(null=True, blank=True)
    cancel_at_period_end = models.BooleanField(default=False)
    canceled_at = models.DateTimeField(null=True, blank=True)
    last_payment_at = models.DateTimeField(null=True, blank=True)
    stripe_customer_id = models.CharField(max_length=255, blank=True, null=True)
    stripe_subscription_id = models.CharField(max_length=255, blank=True, null=True)
    card_brand = models.CharField(max_length=20, null=True, blank=True)
    card_last4 = models.CharField(max_length=4, null=True, blank=True)
    card_exp_month = models.PositiveSmallIntegerField(null=True, blank=True)
    card_exp_year = models.PositiveSmallIntegerField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user_id}: {self.plan} ({self.status})"


class AdminAuditLog(models.Model):
    who = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="admin_audit_actions",
    )
    who_identifier = models.CharField(max_length=255)
    target = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="admin_audit_events",
    )
    target_identifier = models.CharField(max_length=255)
    action = models.CharField(max_length=100)
    timestamp = models.DateTimeField(auto_now_add=True)
    details = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-timestamp"]


class StripeEvent(models.Model):
    event_id = models.CharField(max_length=255, unique=True)
    event_type = models.CharField(max_length=255)
    received_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-received_at"]