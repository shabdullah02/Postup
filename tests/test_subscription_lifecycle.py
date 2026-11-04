from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from billing.entitlements import PlanLimitExceeded, ensure_subscription_active, is_exempt


pytestmark = pytest.mark.django_db


def test_new_user_receives_a_fourteen_day_trial():
    user = get_user_model().objects.create_user(username="trial-user")
    subscription = user.subscription

    assert subscription.started_at is not None
    assert subscription.trial_ends_at is not None
    assert timedelta(days=13, hours=23) < subscription.trial_ends_at - subscription.started_at
    assert subscription.trial_ends_at - subscription.started_at <= timedelta(days=14)


def test_expired_trial_is_blocked_from_subscription_gated_actions():
    user = get_user_model().objects.create_user(username="expired-trial")
    subscription = user.subscription
    subscription.trial_ends_at = timezone.now() - timedelta(seconds=1)
    subscription.save(update_fields=["trial_ends_at"])

    with pytest.raises(PlanLimitExceeded, match="free trial has ended"):
        ensure_subscription_active(user)


def test_superuser_is_exempt_from_expired_and_suspended_subscription():
    user = get_user_model().objects.create_superuser(
        username="exempt-root", password="Valid-password-for-root-7!"
    )
    subscription = user.subscription
    subscription.status = "suspended"
    subscription.trial_ends_at = timezone.now() - timedelta(days=1)
    subscription.save(update_fields=["status", "trial_ends_at"])

    assert is_exempt(user)
    assert ensure_subscription_active(user) == subscription