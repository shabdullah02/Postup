from datetime import timedelta
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import SocialAccount
from accounts.utils.crypto import decrypt_token
from accounts.views import _threads_oauth_cache_key

pytestmark = pytest.mark.django_db


def graph_response(payload, ok=True):
    response = Mock(ok=ok)
    response.json.return_value = payload
    return response


@pytest.fixture
def threads_user():
    user = get_user_model().objects.create_user(
        username="threads-user",
        password="Threads-test-password-123!",
    )
    user.profile.email_verified = True
    user.profile.save(update_fields=["email_verified"])
    return user


@pytest.fixture
def threads_client(threads_user):
    client = APIClient()
    client.force_authenticate(user=threads_user)
    return client


def set_oauth_state(user, state="expected-state"):
    cache.set(_threads_oauth_cache_key(state), user.pk, 10 * 60)


def threads_responses(username="postup_threads", access_token="long-lived-token"):
    return (
        graph_response({"access_token": "short-lived-token", "user_id": "threads-user-123"}),
        graph_response({"access_token": access_token, "expires_in": 5184000}),
        graph_response({
            "id": "threads-user-123",
            "username": username,
            "threads_profile_picture_url": "https://example.com/profile.jpg",
        }),
    )


def test_threads_connect_redirects_with_expected_authorization_url(threads_client, threads_user):
    with (
        override_settings(
            THREADS_APP_ID="threads-app-id",
            THREADS_REDIRECT_URI="https://backend.example.test/api/auth/callback/threads/",
        ),
        patch("accounts.views.requests.get") as mock_get,
        patch("accounts.views.requests.post") as mock_post,
    ):
        response = threads_client.get("/api/socials/threads/connect/")

    assert response.status_code == 302
    parsed_url = urlsplit(response["Location"])
    assert parsed_url.netloc == "threads.net"
    assert parsed_url.path == "/oauth/authorize"
    params = parse_qs(parsed_url.query)
    assert params["client_id"] == ["threads-app-id"]
    assert params["redirect_uri"] == [
        "https://backend.example.test/api/auth/callback/threads/"
    ]
    assert params["scope"] == [
        "threads_basic,threads_content_publish,threads_manage_insights"
    ]
    assert params["response_type"] == ["code"]
    state = params["state"][0]
    assert cache.get(_threads_oauth_cache_key(state)) == threads_user.pk
    mock_get.assert_not_called()
    mock_post.assert_not_called()


def test_threads_callback_creates_account_and_strips_trailing_hash(threads_client, threads_user):
    set_oauth_state(threads_user)
    before = timezone.now()
    with (
        patch("accounts.views.requests.post", return_value=threads_responses()[0]) as mock_post,
        patch(
            "accounts.views.requests.get",
            side_effect=threads_responses()[1:],
        ) as mock_get,
    ):
        response = threads_client.get(
            "/api/auth/callback/threads/",
            {"state": "expected-state", "code": "oauth-code#_"},
        )
    after = timezone.now()

    assert response.status_code == 302
    assert response["Location"] == (
        f"{settings.FRONTEND_URL.rstrip('/')}/accounts?threads_connected=1"
    )
    assert mock_post.call_args.args[0] == "https://graph.threads.net/oauth/access_token"
    assert mock_post.call_args.kwargs["data"] == {
        "client_id": settings.THREADS_APP_ID,
        "client_secret": settings.THREADS_APP_SECRET,
        "grant_type": "authorization_code",
        "redirect_uri": settings.THREADS_REDIRECT_URI,
        "code": "oauth-code",
    }
    assert mock_get.call_args_list[0].args[0] == "https://graph.threads.net/access_token"
    assert mock_get.call_args_list[0].kwargs["params"] == {
        "grant_type": "th_exchange_token",
        "client_secret": settings.THREADS_APP_SECRET,
        "access_token": "short-lived-token",
    }
    assert mock_get.call_args_list[1].args[0] == "https://graph.threads.net/v1.0/me"
    assert mock_get.call_args_list[1].kwargs["params"] == {
        "fields": "id,username,threads_profile_picture_url",
        "access_token": "long-lived-token",
    }

    account = SocialAccount.objects.get(user=threads_user)
    assert account.provider == SocialAccount.Provider.THREADS
    assert account.external_id == "threads-user-123"
    assert account.name == "postup_threads"
    assert decrypt_token(account.token_encrypted) == "long-lived-token"
    assert account.status == SocialAccount.Status.ACTIVE
    assert account.scopes == (
        "threads_basic, threads_content_publish, threads_manage_insights"
    )
    expiry = timedelta(seconds=5184000)
    assert before + expiry <= account.token_expires_at <= after + expiry
    assert "long-lived-token" not in response["Location"]


@pytest.mark.parametrize(
    "query",
    [
        {"state": "wrong-state", "code": "oauth-code"},
        {"code": "oauth-code"},
    ],
)
def test_threads_callback_rejects_bad_or_missing_state(threads_client, threads_user, query):
    set_oauth_state(threads_user)
    with (
        patch("accounts.views.requests.get") as mock_get,
        patch("accounts.views.requests.post") as mock_post,
    ):
        response = threads_client.get("/api/auth/callback/threads/", query)

    assert response.status_code == 302
    assert response["Location"] == (
        f"{settings.FRONTEND_URL.rstrip('/')}/accounts?threads_error=invalid_state"
    )
    assert not SocialAccount.objects.filter(user=threads_user).exists()
    mock_get.assert_not_called()
    mock_post.assert_not_called()


@pytest.mark.parametrize(
    "query",
    [
        {"state": "expected-state", "error": "access_denied"},
        {"state": "expected-state", "error_reason": "user_denied"},
    ],
)
def test_threads_callback_handles_user_denied(threads_client, threads_user, query):
    set_oauth_state(threads_user)
    with (
        patch("accounts.views.requests.get") as mock_get,
        patch("accounts.views.requests.post") as mock_post,
    ):
        response = threads_client.get("/api/auth/callback/threads/", query)

    assert response.status_code == 302
    assert response["Location"] == (
        f"{settings.FRONTEND_URL.rstrip('/')}/accounts?threads_error=threads_denied"
    )
    assert not SocialAccount.objects.filter(user=threads_user).exists()
    mock_get.assert_not_called()
    mock_post.assert_not_called()


@pytest.mark.parametrize("failed_call", [0, 1, 2])
def test_threads_callback_handles_each_meta_api_failure(
    threads_client, threads_user, failed_call
):
    set_oauth_state(threads_user)
    post_response = graph_response({"access_token": "short-lived-token"})
    get_responses = [
        graph_response({"access_token": "long-lived-token", "expires_in": 5184000}),
        graph_response({"id": "threads-user-123", "username": "postup_threads"}),
    ]
    if failed_call == 0:
        post_response = graph_response({}, ok=False)
    else:
        get_responses[failed_call - 1] = graph_response({}, ok=False)

    with (
        patch("accounts.views.requests.post", return_value=post_response) as mock_post,
        patch("accounts.views.requests.get", side_effect=get_responses) as mock_get,
    ):
        response = threads_client.get(
            "/api/auth/callback/threads/",
            {"state": "expected-state", "code": "oauth-code"},
        )

    assert response.status_code == 302
    assert response["Location"] == (
        f"{settings.FRONTEND_URL.rstrip('/')}/accounts?threads_error=threads_unavailable"
    )
    assert not SocialAccount.objects.filter(user=threads_user).exists()
    assert mock_post.call_count == 1
    assert mock_get.call_count == failed_call


def test_threads_reconnect_updates_existing_account(threads_client, threads_user):
    for username, token in (
        ("initial_username", "initial-token"),
        ("updated_username", "updated-token"),
    ):
        set_oauth_state(threads_user)
        responses = threads_responses(username=username, access_token=token)
        with (
            patch("accounts.views.requests.post", return_value=responses[0]),
            patch("accounts.views.requests.get", side_effect=responses[1:]),
        ):
            response = threads_client.get(
                "/api/auth/callback/threads/",
                {"state": "expected-state", "code": "oauth-code"},
            )
        assert response["Location"].endswith("/accounts?threads_connected=1")

    account = SocialAccount.objects.get(user=threads_user)
    assert SocialAccount.objects.filter(
        user=threads_user,
        provider=SocialAccount.Provider.THREADS,
        external_id="threads-user-123",
    ).count() == 1
    assert account.name == "updated_username"
    assert decrypt_token(account.token_encrypted) == "updated-token"


def test_threads_accounts_are_listed_and_can_be_disconnected(threads_client, threads_user):
    account = SocialAccount.objects.create(
        user=threads_user,
        provider=SocialAccount.Provider.THREADS,
        external_id="threads-user-123",
        name="postup_threads",
        token_encrypted=b"encrypted-token",
    )

    listed = threads_client.get("/api/auth/social-accounts/")
    assert listed.status_code == 200
    assert any(item["provider"] == "threads" for item in listed.data)

    disconnected = threads_client.delete(f"/api/accounts/{account.pk}/")
    assert disconnected.status_code == 204
    assert not SocialAccount.objects.filter(pk=account.pk).exists()


def test_threads_connect_requires_authentication():
    client = APIClient()
    with (
        patch("accounts.views.requests.get") as mock_get,
        patch("accounts.views.requests.post") as mock_post,
    ):
        response = client.get("/api/socials/threads/connect/")

    assert response.status_code == 403
    mock_get.assert_not_called()
    mock_post.assert_not_called()


def test_threads_callback_completes_without_authentication(threads_user):
    set_oauth_state(threads_user)
    responses = threads_responses()
    with (
        patch("accounts.views.requests.post", return_value=responses[0]),
        patch("accounts.views.requests.get", side_effect=responses[1:]),
    ):
        response = APIClient().get(
            "/api/auth/callback/threads/",
            {"state": "expected-state", "code": "oauth-code"},
        )

    assert response["Location"].endswith("/accounts?threads_connected=1")
    account = SocialAccount.objects.get(user=threads_user)
    assert account.provider == SocialAccount.Provider.THREADS
    assert account.external_id == "threads-user-123"
