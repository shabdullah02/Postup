from datetime import timedelta
from io import StringIO
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import SocialAccount
from accounts.providers.instagram import InstagramProvider
from accounts.utils.crypto import encrypt_token
from posts.models import Post

pytestmark = pytest.mark.django_db


@pytest.fixture
def authenticated_client():
    user = get_user_model().objects.create_user(
        username="instagram-scaffold-user",
        password="test-password",
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return user, client


def test_instagram_connect_is_404_by_default_and_makes_no_requests(authenticated_client):
    _, client = authenticated_client

    with (
        override_settings(INSTAGRAM_ENABLED=False),
        patch("requests.get") as mock_get,
        patch("requests.post") as mock_post,
    ):
        response = client.get(
            "/api/auth/connect/facebook/",
            {"provider": "instagram"},
        )

    assert response.status_code == 404
    assert response.data == {
        "detail": "Instagram is not enabled.",
        "code": "provider_disabled",
    }
    mock_get.assert_not_called()
    mock_post.assert_not_called()


def test_instagram_connect_is_501_when_enabled_and_makes_no_requests(authenticated_client):
    _, client = authenticated_client

    with (
        override_settings(INSTAGRAM_ENABLED=True),
        patch("requests.get") as mock_get,
        patch("requests.post") as mock_post,
    ):
        response = client.get(
            "/api/auth/connect/facebook/",
            {"provider": "instagram"},
        )

    assert response.status_code == 501
    assert response.data == {
        "detail": "Instagram not implemented yet.",
        "code": "not_implemented",
    }
    mock_get.assert_not_called()
    mock_post.assert_not_called()


def test_facebook_connect_still_redirects_to_facebook_dialog_without_requests(authenticated_client):
    _, client = authenticated_client

    with patch("requests.get") as mock_get, patch("requests.post") as mock_post:
        response = client.get("/api/auth/connect/facebook/")

    parsed_url = urlsplit(response["Location"])
    assert response.status_code == 302
    assert parsed_url.netloc == "www.facebook.com"
    assert "/dialog/oauth" in parsed_url.path
    mock_get.assert_not_called()
    mock_post.assert_not_called()


def test_scheduler_marks_instagram_post_disabled_without_requests(authenticated_client):
    user, _ = authenticated_client
    account = SocialAccount.objects.create(
        user=user,
        provider="instagram",
        external_id="ig-user-123",
        name="Instagram Professional Account",
        token_encrypted=encrypt_token("instagram-token-never-printed"),
        status=SocialAccount.Status.ACTIVE,
    )
    post = Post.objects.create(
        user=user,
        account=account,
        content="Do not send this to Instagram",
        status=Post.Status.SCHEDULED,
        scheduled_at=timezone.now() - timedelta(minutes=1),
    )

    with (
        override_settings(INSTAGRAM_ENABLED=False),
        patch("requests.get") as mock_get,
        patch("requests.post") as mock_post,
        patch.object(InstagramProvider, "publish") as mock_publish,
    ):
        output = StringIO()
        call_command("publish_due", stdout=output)

    post.refresh_from_db()
    assert post.status == Post.Status.FAILED
    assert post.error == "failed: instagram disabled"
    assert "instagram-token-never-printed" not in output.getvalue()
    mock_publish.assert_not_called()
    mock_get.assert_not_called()
    mock_post.assert_not_called()


def test_instagram_provider_exposes_scopes_and_stub_errors():
    provider = InstagramProvider()

    assert provider.auth_scopes() == [
        "instagram_basic",
        "instagram_content_publish",
        "pages_show_list",
        "pages_read_engagement",
    ]
    with pytest.raises(NotImplementedError, match="^Instagram is not enabled yet$"):
        provider.list_instagram_accounts("user-token")
    with pytest.raises(NotImplementedError, match="^Instagram is not enabled yet$"):
        provider.publish(object(), "Content")
