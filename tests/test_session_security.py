import pytest
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.core.cache import cache
from rest_framework.test import APIClient


pytestmark = pytest.mark.django_db


@pytest.fixture
def user():
    return get_user_model().objects.create_user(
        username="session-owner",
        password="Valid-password-for-session-6!",
    )


def test_login_username_ip_lockout_after_five_failures(user):
    cache.clear()
    client = APIClient()
    for _ in range(5):
        response = client.post(
            "/api/auth/login/",
            {"username": user.username, "password": "incorrect"},
            format="json",
        )
        assert response.status_code == 400
    locked = client.post(
        "/api/auth/login/",
        {"username": user.username, "password": "Valid-password-for-session-6!"},
        format="json",
    )

    assert locked.status_code == 429
    assert locked.data["code"] == "login_locked"


def test_login_is_throttled_per_ip_after_ten_attempts():
    cache.clear()
    client = APIClient()
    for attempt in range(10):
        response = client.post(
            "/api/auth/login/",
            {"username": f"missing-{attempt}", "password": "incorrect"},
            format="json",
        )
        assert response.status_code == 400

    limited = client.post(
        "/api/auth/login/",
        {"username": "eleventh-user", "password": "incorrect"},
        format="json",
    )
    assert limited.status_code == 429
    assert limited.data["code"] == "throttled"


def test_sessions_list_and_revoke_others_only_affects_current_user(user):
    current_client = APIClient()
    current_client.force_login(user)
    current_key = current_client.session.session_key
    other_client = APIClient()
    other_client.force_login(user)
    other_key = other_client.session.session_key

    listed = current_client.get("/api/auth/sessions/")
    assert listed.status_code == 200
    assert {row["key"] for row in listed.data["results"]} == {current_key, other_key}
    assert next(row for row in listed.data["results"] if row["key"] == current_key)["current"]

    revoked = current_client.post("/api/auth/sessions/revoke-others/")
    assert revoked.status_code == 200
    assert revoked.data == {"revoked": 1}
    assert Session.objects.filter(session_key=current_key).exists()
    assert not Session.objects.filter(session_key=other_key).exists()


def test_session_delete_cannot_revoke_another_users_session(user):
    owner_client = APIClient()
    owner_client.force_login(user)
    other_user = get_user_model().objects.create_user(username="different-session-owner")
    other_client = APIClient()
    other_client.force_login(other_user)
    other_key = other_client.session.session_key

    response = owner_client.delete(f"/api/auth/sessions/{other_key}/")

    assert response.status_code == 404
    assert Session.objects.filter(session_key=other_key).exists()