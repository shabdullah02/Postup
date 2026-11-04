import pyotp
import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from rest_framework.test import APIClient

from accounts.two_factor import get_totp_secret
from billing.models import AdminAuditLog


pytestmark = pytest.mark.django_db


@pytest.fixture
def user():
    return get_user_model().objects.create_user(
        username="totp-owner",
        email="totp-owner@example.com",
        password="Valid-password-for-totp-6!",
    )


def authenticated_client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def test_totp_setup_enable_recovery_and_disable(user):
    client = authenticated_client(user)
    setup = client.post("/api/auth/2fa/setup/")

    assert setup.status_code == 200
    assert set(setup.data) == {"otpauth_uri", "secret"}
    assert "otpauth://totp/" in setup.data["otpauth_uri"]
    user.profile.refresh_from_db()
    assert user.profile.totp_enabled is False
    assert get_totp_secret(user.profile) == setup.data["secret"]

    code = pyotp.TOTP(setup.data["secret"]).now()
    enabled = client.post("/api/auth/2fa/enable/", {"code": code}, format="json")
    assert enabled.status_code == 200
    assert enabled.data["enabled"] is True
    assert len(enabled.data["recovery_codes"]) == 10
    user.profile.refresh_from_db()
    assert user.profile.totp_enabled is True
    assert len(user.profile.recovery_codes) == 10
    assert enabled.data["recovery_codes"][0] not in user.profile.recovery_codes
    assert AdminAuditLog.objects.filter(action="two_factor_enabled").exists()

    regenerated = client.post(
        "/api/auth/2fa/recovery-codes/regenerate/",
        {
            "password": "Valid-password-for-totp-6!",
            "code": pyotp.TOTP(setup.data["secret"]).now(),
        },
        format="json",
    )
    assert regenerated.status_code == 200
    assert len(regenerated.data["recovery_codes"]) == 10

    disabled = client.post(
        "/api/auth/2fa/disable/",
        {
            "password": "Valid-password-for-totp-6!",
            "code": pyotp.TOTP(setup.data["secret"]).now(),
        },
        format="json",
    )
    assert disabled.status_code == 200
    user.profile.refresh_from_db()
    assert user.profile.totp_enabled is False
    assert user.profile.totp_secret_encrypted == b""
    assert user.profile.recovery_codes == []
    assert AdminAuditLog.objects.filter(action="two_factor_disabled").exists()


def test_login_requires_totp_then_completes_session(user):
    secret = pyotp.random_base32()
    from accounts.two_factor import store_totp_secret

    store_totp_secret(user.profile, secret)
    user.profile.totp_enabled = True
    user.profile.save(update_fields=["totp_enabled"])
    client = APIClient()

    login_response = client.post(
        "/api/auth/login/",
        {"username": user.username, "password": "Valid-password-for-totp-6!"},
        format="json",
    )
    assert login_response.status_code == 200
    assert login_response.data == {"two_factor_required": True}
    assert client.session.get("_auth_user_id") is None
    assert client.session["pending_2fa_user_id"] == user.pk

    challenge = client.post(
        "/api/auth/login/2fa/", {"code": pyotp.TOTP(secret).now()}, format="json"
    )
    assert challenge.status_code == 200
    assert challenge.data == {"id": user.pk, "username": user.username}
    assert client.session.get("_auth_user_id") == str(user.pk)
    assert "pending_2fa_user_id" not in client.session


def test_superuser_login_is_not_blocked_by_enabled_totp():
    user = get_user_model().objects.create_superuser(
        username="totp-root", password="Valid-password-for-root-7!"
    )
    user.profile.totp_enabled = True
    user.profile.save(update_fields=["totp_enabled"])

    response = APIClient().post(
        "/api/auth/login/",
        {"username": user.username, "password": "Valid-password-for-root-7!"},
        format="json",
    )

    assert response.status_code == 200
    assert response.data == {"id": user.pk, "username": user.username}


def test_recovery_code_is_one_time_and_totp_attempts_are_limited(user):
    cache.clear()
    secret = pyotp.random_base32()
    from accounts.two_factor import generate_recovery_codes, store_totp_secret

    plaintext_codes, encoded_codes = generate_recovery_codes()
    store_totp_secret(user.profile, secret)
    user.profile.totp_enabled = True
    user.profile.recovery_codes = encoded_codes
    user.profile.save(update_fields=["totp_enabled", "recovery_codes"])
    client = APIClient()
    client.post(
        "/api/auth/login/",
        {"username": user.username, "password": "Valid-password-for-totp-6!"},
        format="json",
    )
    first = client.post(
        "/api/auth/login/2fa/",
        {"recovery_code": plaintext_codes[0]},
        format="json",
    )
    assert first.status_code == 200
    user.profile.refresh_from_db()
    assert len(user.profile.recovery_codes) == 9

    other_client = APIClient()
    other_client.post(
        "/api/auth/login/",
        {"username": user.username, "password": "Valid-password-for-totp-6!"},
        format="json",
    )
    reused = other_client.post(
        "/api/auth/login/2fa/",
        {"recovery_code": plaintext_codes[0]},
        format="json",
    )
    assert reused.status_code == 400
    assert reused.data["code"] == "invalid_two_factor_code"

    cache.clear()
    limited_client = APIClient()
    limited_client.post(
        "/api/auth/login/",
        {"username": user.username, "password": "Valid-password-for-totp-6!"},
        format="json",
    )
    for _ in range(5):
        failure = limited_client.post(
            "/api/auth/login/2fa/", {"code": "000000"}, format="json"
        )
        assert failure.status_code == 400
    locked = limited_client.post(
        "/api/auth/login/2fa/", {"code": "000000"}, format="json"
    )
    assert locked.status_code == 429