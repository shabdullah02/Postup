from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from billing.models import AdminAuditLog


pytestmark = pytest.mark.django_db


@pytest.fixture
def staff_user():
    return get_user_model().objects.create_user(
        username="panel-staff", is_staff=True
    )


@pytest.fixture
def target_user():
    user = get_user_model().objects.create_user(
        username="panel-target", email="panel-target@example.com"
    )
    user.profile.email_verified = False
    user.profile.totp_enabled = True
    user.profile.totp_secret_encrypted = b"encrypted-secret"
    user.profile.recovery_codes = ["encoded-code"]
    user.profile.save()
    user.subscription.trial_ends_at = None
    user.subscription.cancel_at_period_end = True
    user.subscription.save(update_fields=["trial_ends_at", "cancel_at_period_end"])
    return user


def admin_client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def test_admin_user_detail_includes_security_lifecycle_fields(staff_user, target_user):
    detail = admin_client(staff_user).get(f"/api/admin/users/{target_user.pk}/")
    listing = admin_client(staff_user).get("/api/admin/users/")

    assert detail.status_code == 200
    assert detail.data["email_verified"] is False
    assert detail.data["totp_enabled"] is True
    assert "trial_ends_at" in detail.data
    assert detail.data["cancel_at_period_end"] is True
    assert "email_verified" not in listing.data["results"][0]


def test_admin_can_reset_2fa_and_verify_email(staff_user, target_user):
    client = admin_client(staff_user)
    reset = client.post(f"/api/admin/users/{target_user.pk}/reset-2fa/")
    verify = client.post(f"/api/admin/users/{target_user.pk}/verify-email/")

    assert reset.status_code == 200
    assert verify.status_code == 200
    target_user.profile.refresh_from_db()
    assert target_user.profile.totp_enabled is False
    assert target_user.profile.totp_secret_encrypted == b""
    assert target_user.profile.recovery_codes == []
    assert target_user.profile.email_verified is True
    assert AdminAuditLog.objects.filter(action="admin_two_factor_reset").exists()
    assert AdminAuditLog.objects.filter(action="admin_email_verified").exists()


def test_admin_reactivation_keeps_next_billing_date_in_sync(staff_user, target_user):
    subscription = target_user.subscription
    subscription.status = "suspended"
    subscription.current_period_end = timezone.now() - timedelta(days=1)
    subscription.next_billing_date = None
    subscription.save()

    response = admin_client(staff_user).post(
        f"/api/admin/users/{target_user.pk}/reactivate/"
    )

    subscription.refresh_from_db()
    assert response.status_code == 200
    assert subscription.next_billing_date == subscription.current_period_end


def test_admin_security_actions_reject_superuser_target(staff_user):
    root = get_user_model().objects.create_superuser(
        username="protected-root", password="Valid-password-for-root-7!"
    )

    response = admin_client(staff_user).post(
        f"/api/admin/users/{root.pk}/reset-2fa/"
    )

    assert response.status_code == 400
    assert response.data["code"] == "protected_user"
    assert root.profile.totp_enabled is False


def test_audit_log_is_paginated_and_requires_admin(staff_user, target_user):
    AdminAuditLog.objects.create(
        who=staff_user,
        who_identifier=f"{staff_user.pk}:{staff_user.username}",
        target=target_user,
        target_identifier=f"{target_user.pk}:{target_user.username}",
        action="sample_action",
    )

    response = admin_client(staff_user).get("/api/admin/audit-log/?page_size=1")
    denied = admin_client(target_user).get("/api/admin/audit-log/")

    assert response.status_code == 200
    assert response.data["count"] == 1
    assert len(response.data["results"]) == 1
    assert response.data["results"][0]["action"] == "sample_action"
    assert denied.status_code == 403