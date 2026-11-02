from datetime import timedelta
from io import StringIO
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import SocialAccount
from accounts.providers.instagram import InstagramProvider
from accounts.utils.crypto import decrypt_token, encrypt_token
from posts.models import Post
from socials.adapters import AuthExpiredError, FacebookPublishResult, PublishError

pytestmark = pytest.mark.django_db


@pytest.fixture
def authenticated_client():
    user = get_user_model().objects.create_user(
        username="instagram-scaffold-user",
        password="test-password",
    )
    user.profile.email_verified = True
    user.profile.save(update_fields=["email_verified"])
    client = APIClient()
    client.force_authenticate(user=user)
    return user, client


def graph_response(payload, ok=True):
    response = Mock(ok=ok)
    response.json.return_value = payload
    return response


def instagram_oauth_responses(pages, token="instagram-user-token"):
    return [
        graph_response({"access_token": "instagram-short-token"}),
        graph_response({"access_token": token}),
        graph_response({"data": [
            {"permission": "instagram_content_publish", "status": "granted"},
            {"permission": "pages_show_list", "status": "granted"},
            {"permission": "instagram_basic", "status": "granted"},
        ]}),
        graph_response({"data": pages}),
    ]


def instagram_page(
    ig_user_id="ig-user-123",
    username="postup_ig",
    page_id="page-123",
    page_token="instagram-page-token",
):
    return {
        "id": page_id,
        "name": "Facebook Page",
        "access_token": page_token,
        "instagram_business_account": {"id": ig_user_id, "username": username},
    }


def instagram_callback(client, state):
    return client.get(
        "/api/auth/callback/facebook/",
        {"state": state, "code": "instagram-oauth-code"},
    )


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


def test_instagram_connect_redirects_with_instagram_scopes(authenticated_client):
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

    assert response.status_code == 302
    parsed_url = urlsplit(response["Location"])
    assert parsed_url.netloc == "www.facebook.com"
    query = parse_qs(parsed_url.query)
    assert set(query["scope"][0].split(",")) == set(InstagramProvider().auth_scopes())
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


def test_instagram_provider_exposes_scopes_and_rejects_text_only_posts():
    provider = InstagramProvider()

    assert provider.auth_scopes() == [
        "instagram_basic",
        "instagram_content_publish",
        "pages_show_list",
        "pages_read_engagement",
    ]
    with pytest.raises(PublishError, match="Instagram posts require an image URL"):
        provider.publish(object(), "Content")


def test_instagram_callback_selects_instagram_accounts_and_hides_tokens(authenticated_client):
    user, client = authenticated_client
    token = "never-return-instagram-page-token"
    with override_settings(INSTAGRAM_ENABLED=True):
        connect = client.get(
            "/api/auth/connect/facebook/", {"provider": "instagram"},
        )
        state = parse_qs(urlsplit(connect["Location"]).query)["state"][0]
        with patch(
            "accounts.views.requests.get",
            side_effect=instagram_oauth_responses([
                instagram_page(page_token=token),
                {
                    "id": "page-without-instagram",
                    "name": "Facebook Only",
                    "access_token": "facebook-page-token",
                },
            ]),
        ) as mock_get:
            callback = instagram_callback(client, state)

    assert callback.status_code == 302
    assert callback["Location"] == f"{settings.FRONTEND_URL.rstrip('/')}/accounts/select"
    assert token not in callback["Location"]
    assert "instagram_business_account{id,username}" in mock_get.call_args.kwargs["params"]["fields"]
    with override_settings(INSTAGRAM_ENABLED=True):
        pending_response = client.get("/api/auth/connect/facebook/select/")
        assert pending_response.data == [{"id": "ig-user-123", "name": "postup_ig"}]
        assert token not in pending_response.content.decode()

        select_response = client.post(
            "/api/auth/connect/facebook/select/",
            {"page_ids": ["ig-user-123"]},
            format="json",
        )
    assert select_response.status_code == 200
    account = SocialAccount.objects.get(
        user=user,
        provider=SocialAccount.Provider.INSTAGRAM,
        external_id="ig-user-123",
    )
    assert account.name == "postup_ig"
    assert decrypt_token(account.token_encrypted) == token
    assert token not in select_response.content.decode()
    assert "token" not in select_response.content.decode().lower()


def test_instagram_callback_redirects_when_no_instagram_accounts(authenticated_client):
    _, client = authenticated_client
    with override_settings(INSTAGRAM_ENABLED=True):
        connect = client.get("/api/auth/connect/facebook/", {"provider": "instagram"})
        state = parse_qs(urlsplit(connect["Location"]).query)["state"][0]
        with patch("accounts.views.requests.get", side_effect=instagram_oauth_responses([
            {
                "id": "page-without-instagram",
                "name": "Facebook Only",
                "access_token": "hidden-page-token",
            }
        ])):
            response = instagram_callback(client, state)
    assert response.status_code == 302
    assert response["Location"] == (
        f"{settings.FRONTEND_URL.rstrip('/')}/accounts?fb_error=no_instagram_accounts"
    )
    assert "hidden-page-token" not in response["Location"]


def test_instagram_reconnect_matches_ig_user_id(authenticated_client):
    user, client = authenticated_client
    account = SocialAccount.objects.create(
        user=user,
        provider=SocialAccount.Provider.INSTAGRAM,
        external_id="ig-user-123",
        name="old_username",
        token_encrypted=encrypt_token("old-instagram-token"),
        status=SocialAccount.Status.NEEDS_REAUTH,
    )
    with override_settings(INSTAGRAM_ENABLED=True):
        connect = client.get(
            "/api/auth/connect/facebook/",
            {"provider": "instagram", "reconnect": account.id},
        )
        state = parse_qs(urlsplit(connect["Location"]).query)["state"][0]
        with patch("accounts.views.requests.get", side_effect=instagram_oauth_responses([
            instagram_page(
                ig_user_id="ig-user-123",
                username="new_username",
                page_id="different-linked-page",
                page_token="new-instagram-page-token",
            )
        ])):
            response = instagram_callback(client, state)

    account.refresh_from_db()
    assert response["Location"] == f"{settings.FRONTEND_URL.rstrip('/')}/accounts?fb_connected=1"
    assert account.name == "new_username"
    assert account.status == SocialAccount.Status.ACTIVE
    assert decrypt_token(account.token_encrypted) == "new-instagram-page-token"
    assert SocialAccount.objects.filter(user=user, provider="instagram").count() == 1


def test_instagram_provider_lists_linked_professional_accounts():
    with patch(
        "accounts.providers.instagram.requests.get",
        return_value=graph_response({"data": [
            instagram_page(),
            {"id": "facebook-only", "name": "No IG"},
        ]}),
    ) as mock_get:
        accounts = InstagramProvider().list_instagram_accounts("user-token")
    assert accounts == [{
        "id": "ig-user-123",
        "username": "postup_ig",
        "page_id": "page-123",
        "access_token": "instagram-page-token",
    }]
    assert mock_get.call_args.kwargs["params"]["access_token"] == "user-token"


def test_instagram_publish_completes_two_step_flow_and_fetches_permalink(authenticated_client):
    user, _ = authenticated_client
    account = SocialAccount.objects.create(
        user=user,
        provider="instagram",
        external_id="ig-user-123",
        name="postup_ig",
        token_encrypted=encrypt_token("private-instagram-token"),
    )
    with (
        patch(
            "accounts.providers.instagram.requests.post",
            side_effect=[
                graph_response({"id": "container-123"}),
                graph_response({"id": "media-456"}),
            ],
        ) as mock_post,
        patch(
            "accounts.providers.instagram.requests.get",
            side_effect=[
                graph_response({"status_code": "FINISHED"}),
                graph_response({"permalink": "https://www.instagram.com/p/media-456/"}),
            ],
        ) as mock_get,
    ):
        result = InstagramProvider().publish(
            account, "A caption", image_url="https://cdn.example.com/photo.jpg"
        )
    assert result == FacebookPublishResult(
        "media-456", "https://www.instagram.com/p/media-456/"
    )
    assert mock_post.call_count == 2
    assert mock_post.call_args_list[0].kwargs["data"]["caption"] == "A caption"
    assert mock_post.call_args_list[0].kwargs["data"]["image_url"] == (
        "https://cdn.example.com/photo.jpg"
    )
    assert mock_post.call_args_list[1].args[0].endswith("/ig-user-123/media_publish")
    assert mock_post.call_args_list[1].kwargs["data"]["creation_id"] == "container-123"
    assert mock_get.call_args_list[0].kwargs["params"]["fields"] == "status_code"
    assert mock_get.call_args_list[1].kwargs["params"]["fields"] == "permalink"


def test_instagram_publish_container_error_does_not_publish(authenticated_client):
    user, _ = authenticated_client
    account = SocialAccount.objects.create(
        user=user,
        provider="instagram",
        external_id="ig-user-123",
        name="postup_ig",
        token_encrypted=encrypt_token("private-instagram-token"),
    )
    with (
        patch(
            "accounts.providers.instagram.requests.post",
            return_value=graph_response({"id": "container-123"}),
        ) as mock_post,
        patch(
            "accounts.providers.instagram.requests.get",
            return_value=graph_response({"status_code": "ERROR"}),
        ),
        pytest.raises(PublishError, match="processing failed"),
    ):
        InstagramProvider().publish(
            account, "A caption", image_url="https://cdn.example.com/photo.jpg"
        )
    assert mock_post.call_count == 1


def test_instagram_graph_error_190_marks_reauth_and_hides_token(authenticated_client):
    user, _ = authenticated_client
    token = "private-instagram-token"
    account = SocialAccount.objects.create(
        user=user,
        provider="instagram",
        external_id="ig-user-123",
        name="postup_ig",
        token_encrypted=encrypt_token(token),
    )
    with patch(
        "accounts.providers.instagram.requests.post",
        return_value=graph_response({
            "error": {"code": 190, "message": f"Invalid token {token}"}
        }, ok=False),
    ):
        with pytest.raises(AuthExpiredError) as error:
            InstagramProvider().publish(
                account, "A caption", image_url="https://cdn.example.com/photo.jpg"
            )
    account.refresh_from_db()
    assert account.status == SocialAccount.Status.NEEDS_REAUTH
    assert token not in str(error.value)
