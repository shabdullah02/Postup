from urllib.parse import parse_qs, urlsplit

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient


pytestmark = pytest.mark.django_db


@pytest.fixture
def authenticated_client():
    user = get_user_model().objects.create_user(username="oauth-callback-user")
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def assert_frontend_error(response, expected_error):
    assert response.status_code == 302
    redirect = urlsplit(response["Location"])
    assert redirect.scheme and redirect.netloc
    assert f"{redirect.scheme}://{redirect.netloc}{redirect.path}" == (
        f"{settings.FRONTEND_URL.rstrip('/')}/accounts"
    )
    assert parse_qs(redirect.query) == {"fb_error": [expected_error]}


def test_callback_invalid_state_redirects_to_frontend(authenticated_client):
    session = authenticated_client.session
    session["fb_oauth_state"] = "expected-state"
    session.save()

    response = authenticated_client.get(
        "/api/auth/callback/facebook/",
        {"state": "wrong-state", "code": "oauth-code"},
    )

    assert_frontend_error(response, "invalid_state")
    assert "fb_oauth_state" not in authenticated_client.session


def test_callback_missing_code_redirects_to_frontend(authenticated_client):
    session = authenticated_client.session
    session["fb_oauth_state"] = "expected-state"
    session.save()

    response = authenticated_client.get(
        "/api/auth/callback/facebook/",
        {"state": "expected-state"},
    )

    assert_frontend_error(response, "missing_code")
    assert "fb_oauth_state" not in authenticated_client.session


def test_callback_facebook_error_redirects_to_frontend(authenticated_client):
    response = authenticated_client.get(
        "/api/auth/callback/facebook/",
        {"error": "access_denied", "error_description": "User denied access"},
    )

    assert_frontend_error(response, "facebook_denied")


def test_registration_creates_user_subscription_and_session():
    client = APIClient()
    response = client.post(
        "/api/auth/register/",
        {
            "username": "registered-user",
            "email": "registered@example.com",
            "password": "Unique-registration-passphrase-847!",
        },
        format="json",
    )

    assert response.status_code == 201
    user = get_user_model().objects.get(username="registered-user")
    assert user.subscription.plan == "free"
    assert user.subscription.status == "trialing"
    assert client.session.get("_auth_user_id") == str(user.pk)


def test_registration_rejects_case_insensitive_duplicate_email():
    get_user_model().objects.create_user(
        username="existing-email-user",
        email="Existing@Example.com",
        password="existing-password",
    )
    response = APIClient().post(
        "/api/auth/register/",
        {
            "username": "another-user",
            "email": "existing@example.COM",
            "password": "Unique-registration-passphrase-847!",
        },
        format="json",
    )

    assert response.status_code == 400
    assert "email" in response.data["detail"]
    assert response.data["code"] == "validation_error"


def test_registration_rejects_weak_password():
    response = APIClient().post(
        "/api/auth/register/",
        {
            "username": "weak-password-user",
            "email": "weak@example.com",
            "password": "password123",
        },
        format="json",
    )

    assert response.status_code == 400
    assert "password" in response.data["detail"]


def test_me_includes_staff_status(authenticated_client):
    response = authenticated_client.get("/api/auth/me/")

    assert response.status_code == 200
    assert response.data["is_staff"] is False