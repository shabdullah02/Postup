from datetime import timedelta
from urllib.parse import unquote, urlsplit

import pytest
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.core import signing
from django.core import mail
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from unittest.mock import patch

from accounts.email_tokens import EMAIL_VERIFICATION_SALT
from accounts.models import SocialAccount
from billing.models import AdminAuditLog
from posts.models import Post


pytestmark = pytest.mark.django_db


@pytest.fixture
def user():
    return get_user_model().objects.create_user(
        username="account-owner",
        email="account-owner@example.com",
        password="Valid-password-for-account-6!",
    )


def client_for(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
def test_email_change_requires_verification_and_sends_token(user):
    mail.outbox = []
    response = client_for(user).patch(
        "/api/auth/profile/", {"email": "new-address@example.com"}, format="json"
    )

    assert response.status_code == 200
    assert response.data["email_verified"] is False
    assert user.profile.email_verified is False
    assert len(mail.outbox) == 1
    link = next(line for line in mail.outbox[0].body.splitlines() if "/verify-email/" in line)
    token = unquote(urlsplit(link).path.removeprefix("/verify-email/"))

    verify = APIClient().post(
        "/api/auth/verify-email/", {"token": token}, format="json"
    )
    user.profile.refresh_from_db()
    assert verify.status_code == 200
    assert verify.data == {"email_verified": True}
    assert user.profile.email_verified is True
    assert AdminAuditLog.objects.filter(action="email_changed").exists()


def test_email_verification_rejects_expired_token(user):
    with patch("django.core.signing.time.time", return_value=1):
        token = signing.dumps(
            {"user_id": user.pk, "email": user.email},
            salt=EMAIL_VERIFICATION_SALT,
        )

    response = APIClient().post(
        "/api/auth/verify-email/", {"token": token}, format="json"
    )

    assert response.status_code == 400
    assert response.data["code"] == "invalid_verification_token"


def test_registration_sends_verification_email():
    mail.outbox = []
    response = APIClient().post(
        "/api/auth/register/",
        {
            "username": "verify-on-register",
            "email": "verify-on-register@example.com",
            "password": "Unique-registration-passphrase-847!",
        },
        format="json",
    )

    assert response.status_code == 201
    assert len(mail.outbox) == 1
    assert "/verify-email/" in urlsplit(mail.outbox[0].body).path


def test_resend_verification_reports_success(user):
    with patch("accounts.views.send_verification_email", return_value=True) as send_email:
        response = client_for(user).post("/api/auth/resend-verification/")

    assert response.status_code == 200
    assert response.data == {"ok": True, "email_sent": True}
    send_email.assert_called_once_with(user)


def test_resend_verification_reports_delivery_failure(user):
    with patch("accounts.views.send_verification_email", return_value=False):
        response = client_for(user).post("/api/auth/resend-verification/")

    assert response.status_code == 503
    assert response.data == {
        "ok": False,
        "email_sent": False,
        "detail": "Could not send verification email. Please try again.",
    }


def test_resend_verification_skips_verified_user(user):
    user.profile.email_verified = True
    user.profile.save(update_fields=["email_verified"])

    with patch("accounts.views.send_verification_email") as send_email:
        response = client_for(user).post("/api/auth/resend-verification/")

    assert response.status_code == 200
    assert response.data == {
        "ok": True,
        "email_sent": False,
        "already_verified": True,
    }
    send_email.assert_not_called()


def test_connect_and_schedule_require_verified_email(user):
    client = client_for(user)
    connect = client.get("/api/auth/connect/facebook/")
    account = SocialAccount.objects.create(
        user=user,
        provider=SocialAccount.Provider.FACEBOOK,
        external_id="unverified-page",
        name="Unverified Page",
        token_encrypted=b"encrypted",
    )
    post = Post.objects.create(user=user, account=account, content="Draft")
    schedule = client.post(
        f"/api/posts/{post.pk}/schedule/",
        {"scheduled_at": (timezone.now() + timedelta(days=1)).isoformat()},
        format="json",
    )

    assert connect.status_code == 403
    assert connect.data["code"] == "email_not_verified"
    assert schedule.status_code == 403
    assert schedule.data["code"] == "email_not_verified"


def test_change_password_keeps_current_session_and_revokes_others(user):
    client = APIClient()
    client.force_login(user)
    other_client = APIClient()
    other_client.force_login(user)
    other_session_key = other_client.session.session_key

    response = client.post(
        "/api/auth/change-password/",
        {
            "current_password": "Valid-password-for-account-6!",
            "new_password": "Strong-new-account-Password-951!",
        },
        format="json",
    )

    assert response.status_code == 200
    assert client.get("/api/auth/me/").status_code == 200
    assert not Session.objects.filter(session_key=other_session_key).exists()
    user.refresh_from_db()
    assert user.check_password("Strong-new-account-Password-951!")
    assert AdminAuditLog.objects.filter(action="password_changed").exists()


def test_delete_account_requires_password_and_username_and_audits(user):
    client = client_for(user)
    rejected = client.post(
        "/api/auth/delete-account/",
        {"password": "wrong", "confirm": user.username},
        format="json",
    )
    assert rejected.status_code == 400
    assert get_user_model().objects.filter(pk=user.pk).exists()

    deleted = client.post(
        "/api/auth/delete-account/",
        {"password": "Valid-password-for-account-6!", "confirm": user.username},
        format="json",
    )

    assert deleted.status_code == 204
    assert not get_user_model().objects.filter(pk=user.pk).exists()
    assert AdminAuditLog.objects.filter(action="account_deleted").exists()


def test_superuser_cannot_self_delete():
    user = get_user_model().objects.create_superuser(
        username="account-root", password="Valid-password-for-root-7!"
    )

    response = client_for(user).post(
        "/api/auth/delete-account/",
        {"password": "Valid-password-for-root-7!", "confirm": user.username},
        format="json",
    )

    assert response.status_code == 400
    assert response.data["code"] == "protected_user"
    assert get_user_model().objects.filter(pk=user.pk).exists()