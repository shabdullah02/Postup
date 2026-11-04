import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from accounts.models import UserProfile


pytestmark = pytest.mark.django_db


def test_user_without_profile_logs_in_successfully():
    user = get_user_model().objects.create_user(
        username="profileless-user",
        password="Valid-password-for-profileless-7!",
    )
    UserProfile.objects.filter(user=user).delete()

    response = APIClient().post(
        "/api/auth/login/",
        {"username": user.username, "password": "Valid-password-for-profileless-7!"},
        format="json",
    )

    assert response.status_code == 200
    assert UserProfile.objects.filter(user=user, email_verified=False).exists()


def test_null_subscription_lifecycle_fields_do_not_break_account_views():
    user = get_user_model().objects.create_user(
        username="null-lifecycle-user",
        password="Valid-password-for-lifecycle-7!",
    )
    subscription = user.subscription
    subscription.started_at = None
    subscription.trial_ends_at = None
    subscription.current_period_end = None
    subscription.next_billing_date = None
    subscription.save(update_fields=[
        "started_at",
        "trial_ends_at",
        "current_period_end",
        "next_billing_date",
    ])
    client = APIClient()
    client.force_login(user)

    billing_response = client.get("/api/billing/subscription/")
    me_response = client.get("/api/auth/me/")

    assert billing_response.status_code == 200
    assert billing_response.data["days_remaining"] is None
    assert me_response.status_code == 200