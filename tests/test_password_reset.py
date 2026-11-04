from urllib.parse import parse_qs, urlsplit

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core.cache import cache
from django.contrib.sessions.models import Session
from django.core import mail
from django.test import override_settings
from rest_framework.test import APIClient
from django.utils.http import urlsafe_base64_encode


pytestmark = pytest.mark.django_db


@pytest.fixture
def user():
    return get_user_model().objects.create_user(
        username="reset-owner",
        email="reset-owner@example.com",
        password="Valid-password-for-reset-6!",
    )


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
def test_reset_request_does_not_enumerate_users(user):
    mail.outbox = []
    client = APIClient()
    known = client.post(
        "/api/auth/password-reset/", {"email": user.email}, format="json"
    )
    unknown = client.post(
        "/api/auth/password-reset/", {"email": "unknown@example.com"}, format="json"
    )

    assert known.status_code == unknown.status_code == 200
    assert known.data == unknown.data == {"ok": True}
    assert len(mail.outbox) == 1
    assert "Reset your Postup password" == mail.outbox[0].subject


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
def test_reset_confirm_changes_password_and_revokes_sessions(user):
    mail.outbox = []
    client = APIClient()
    client.force_login(user)
    session_key = client.session.session_key
    client.post("/api/auth/password-reset/", {"email": user.email}, format="json")
    reset_link = next(
        line for line in mail.outbox[0].body.splitlines() if "?uid=" in line
    )
    params = parse_qs(urlsplit(reset_link).query)

    response = APIClient().post(
        "/api/auth/password-reset/confirm/",
        {
            "uid": params["uid"][0],
            "token": params["token"][0],
            "new_password": "Strong-new-reset-Password-951!",
        },
        format="json",
    )

    assert response.status_code == 200
    assert not Session.objects.filter(session_key=session_key).exists()
    user.refresh_from_db()
    assert user.check_password("Strong-new-reset-Password-951!")
    assert len(mail.outbox) == 2


def test_reset_confirm_rejects_bad_token_and_weak_password(user):
    cache.clear()
    uid = APIClient().post(
        "/api/auth/password-reset/", {"email": user.email}, format="json"
    ).data
    assert uid == {"ok": True}

    response = APIClient().post(
        "/api/auth/password-reset/confirm/",
        {"uid": "not-a-real-uid", "token": "invalid", "new_password": "password"},
        format="json",
    )

    assert response.status_code == 400
    assert response.data["code"] == "invalid_reset_token"

    valid_uid = urlsafe_base64_encode(str(user.pk).encode())
    valid_token = default_token_generator.make_token(user)
    weak_password = APIClient().post(
        "/api/auth/password-reset/confirm/",
        {"uid": valid_uid, "token": valid_token, "new_password": "password"},
        format="json",
    )
    assert weak_password.status_code == 400
    assert "new_password" in weak_password.data["detail"]