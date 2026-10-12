from datetime import timedelta
from unittest.mock import Mock, patch

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import SocialAccount
from accounts.utils.crypto import encrypt_token
from posts.models import Post
from socials.adapters import AuthExpiredError, FacebookAdapter

pytestmark = pytest.mark.django_db


@pytest.fixture
def user():
    return get_user_model().objects.create_user(username="fb-user", password="pw")


@pytest.fixture
def facebook_account(user):
    return SocialAccount.objects.create(
        user=user,
        provider=SocialAccount.Provider.FACEBOOK,
        external_id="page-123",
        name="My Page",
        token_encrypted=encrypt_token("super-secret-token"),
        status=SocialAccount.Status.ACTIVE,
    )


def test_publish_success_returns_post_id(facebook_account):
    with patch("socials.adapters.requests.post") as mock_post:
        mock_post.return_value.ok = True
        mock_post.return_value.json.return_value = {"id": "post-abc-123"}

        result = FacebookAdapter().post(facebook_account, "Hello from pytest")

    assert result == "post-abc-123"
    assert mock_post.call_count == 1
    assert mock_post.call_args.kwargs["data"]["message"] == "Hello from pytest"
    assert mock_post.call_args.kwargs["data"]["access_token"] == "super-secret-token"


def test_graph_error_code_190_sets_status_needs_reauth_and_raises_authexpired(facebook_account):
    with patch("socials.adapters.requests.post") as mock_post:
        mock_post.return_value.ok = False
        mock_post.return_value.json.return_value = {
            "error": {"code": 190, "message": "Invalid OAuth access token."}
        }

        with pytest.raises(AuthExpiredError):
            FacebookAdapter().post(facebook_account, "Hello")

    facebook_account.refresh_from_db()
    assert facebook_account.status == SocialAccount.Status.NEEDS_REAUTH


def test_scheduler_skips_needs_reauth_accounts(user):
    account = SocialAccount.objects.create(
        user=user,
        provider=SocialAccount.Provider.FACEBOOK,
        external_id="page-999",
        name="Needs Reauth",
        token_encrypted=encrypt_token("expired-token"),
        status=SocialAccount.Status.NEEDS_REAUTH,
    )
    post = Post.objects.create(
        user=user,
        account=account,
        content="Should not post",
        status=Post.Status.SCHEDULED,
        scheduled_at=timezone.now() - timedelta(minutes=1),
    )

    with patch("posts.management.commands.publish_due.ADAPTERS") as mock_adapters:
        call_command("publish_due")

    post.refresh_from_db()
    assert post.status == Post.Status.FAILED
    assert post.error == "failed: reauth required"
    mock_adapters["facebook"].post.assert_not_called()


def test_callback_rejects_mismatched_state_with_400(user):
    client = APIClient()
    client.force_authenticate(user=user)
    session = client.session
    session["fb_oauth_state"] = "expected-state"
    session.save()

    response = client.get(
        "/api/auth/callback/facebook/",
        {"state": "wrong-state", "code": "some-code"},
    )

    assert response.status_code == 400
    assert "Invalid Facebook OAuth state." in response.data["detail"]


def test_callback_does_not_include_tokens_in_response_body(user):
    client = APIClient()
    client.force_authenticate(user=user)
    session = client.session
    session["fb_oauth_state"] = "expected-state"
    session.save()

    short_token_response = Mock(ok=True)
    short_token_response.json.return_value = {"access_token": "short-lived-secret"}
    long_token_response = Mock(ok=True)
    long_token_response.json.return_value = {"access_token": "long-lived-secret"}
    pages_response = Mock(ok=True)
    pages_response.json.return_value = {
        "data": [
            {
                "id": "page-123",
                "name": "My Page",
                "access_token": "page-access-secret",
            }
        ]
    }

    with patch(
        "accounts.views.requests.get",
        side_effect=[short_token_response, long_token_response, pages_response],
    ):
        response = client.get(
            "/api/auth/callback/facebook/",
            {"state": "expected-state", "code": "oauth-code"},
        )

    assert response.status_code == 302
    body = response.content.decode()
    assert "short-lived-secret" not in body
    assert "long-lived-secret" not in body
    assert "page-access-secret" not in body
