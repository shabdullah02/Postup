import base64
from datetime import timedelta
from io import StringIO
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.management.commands.check_tokens import Command as CheckTokensCommand
from accounts.models import SocialAccount
from accounts.utils.crypto import decrypt_token, encrypt_token
from posts.models import Post
from socials.adapters import AuthExpiredError, FacebookAdapter, FacebookPublishResult

pytestmark = pytest.mark.django_db


def graph_response(payload):
    response = Mock(ok=True)
    response.json.return_value = payload
    return response


def callback_responses(pages, permissions=None):
    if permissions is None:
        permissions = [
            {"permission": "pages_manage_posts", "status": "granted"},
            {"permission": "pages_show_list", "status": "granted"},
        ]
    return [
        graph_response({"access_token": "short-lived-secret"}),
        graph_response({"access_token": "long-lived-secret"}),
        graph_response({"data": permissions}),
        graph_response({"data": pages}),
    ]


def assert_no_tokens(response, *tokens):
    response_body = response.content.decode()
    redirect_url = response.get("Location", "")
    for token in tokens:
        assert token not in response_body
        assert token not in redirect_url


def callback_request(client):
    return client.get(
        "/api/auth/callback/facebook/",
        {"state": "expected-state", "code": "oauth-code"},
    )


def set_pending_pages(client, pages=None):
    session = client.session
    if pages is None:
        pages = [
            {
                "id": "page-pending-1",
                "name": "Pending Page",
                "token_encrypted": base64.b64encode(
                    encrypt_token("pending-page-secret")
                ).decode(),
                "scopes": "pages_show_list,pages_manage_posts",
            }
        ]
    session["fb_pending_pages"] = pages
    session.save()


def test_facebook_oauth_settings_require_numeric_app_id_and_secret():
    from config.settings import _validate_facebook_oauth_settings

    with pytest.raises(ImproperlyConfigured, match="FB_APP_ID must contain only digits"):
        _validate_facebook_oauth_settings("app-id", "configured-secret")

    with pytest.raises(ImproperlyConfigured, match="FB_APP_SECRET is required"):
        _validate_facebook_oauth_settings("123456", "")

    _validate_facebook_oauth_settings("123456", "configured-secret")


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
    with patch("socials.adapters.requests.post") as mock_post, patch(
        "socials.adapters.requests.get"
    ) as mock_get:
        mock_post.return_value.ok = True
        mock_post.return_value.json.return_value = {"id": "post-abc-123"}
        mock_get.return_value.ok = True
        mock_get.return_value.json.return_value = {
            "permalink_url": "https://www.facebook.com/posts/post-abc-123"
        }

        result = FacebookAdapter().post(facebook_account, "Hello from pytest")

    assert result.external_post_id == "post-abc-123"
    assert result.permalink_url == "https://www.facebook.com/posts/post-abc-123"
    assert mock_post.call_count == 1
    assert mock_post.call_args.kwargs["data"]["message"] == "Hello from pytest"
    assert mock_post.call_args.kwargs["data"]["access_token"] == "super-secret-token"
    assert mock_get.call_args.args[0].endswith("/post-abc-123")
    assert mock_get.call_args.kwargs["params"] == {
        "fields": "permalink_url",
        "access_token": "super-secret-token",
    }


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


def test_scheduler_marks_invalid_encrypted_token_for_reauth_without_crashing(user):
    account = SocialAccount.objects.create(
        user=user,
        provider=SocialAccount.Provider.FACEBOOK,
        external_id="page-invalid-token",
        name="Invalid Token Page",
        token_encrypted=b"not-fernet-ciphertext",
        status=SocialAccount.Status.ACTIVE,
    )
    post = Post.objects.create(
        user=user,
        account=account,
        content="Should not reach Facebook",
        status=Post.Status.SCHEDULED,
        scheduled_at=timezone.now() - timedelta(minutes=1),
    )
    remaining_post = Post.objects.create(
        user=user,
        account=account,
        content="Also should not reach Facebook",
        status=Post.Status.SCHEDULED,
        scheduled_at=timezone.now() - timedelta(minutes=1),
    )

    with patch("socials.adapters.requests.post") as mock_post:
        call_command("publish_due")

    account.refresh_from_db()
    post.refresh_from_db()
    remaining_post.refresh_from_db()
    assert account.status == SocialAccount.Status.NEEDS_REAUTH
    assert post.status == Post.Status.FAILED
    assert post.error == "failed: reauth required"
    assert remaining_post.status == Post.Status.FAILED
    assert remaining_post.error == "failed: reauth required"
    mock_post.assert_not_called()


def test_scheduler_graph_error_190_marks_reauth_and_skips_remaining_posts(user, facebook_account):
    first_post = Post.objects.create(
        user=user,
        account=facebook_account,
        content="First post",
        status=Post.Status.SCHEDULED,
        scheduled_at=timezone.now() - timedelta(minutes=1),
    )
    remaining_post = Post.objects.create(
        user=user,
        account=facebook_account,
        content="Second post",
        status=Post.Status.SCHEDULED,
        scheduled_at=timezone.now() - timedelta(minutes=1),
    )

    with patch("socials.adapters.requests.post") as mock_post:
        mock_post.return_value.ok = False
        mock_post.return_value.json.return_value = {
            "error": {"code": 190, "message": "Invalid OAuth access token."}
        }
        call_command("publish_due")

    facebook_account.refresh_from_db()
    first_post.refresh_from_db()
    remaining_post.refresh_from_db()
    assert facebook_account.status == SocialAccount.Status.NEEDS_REAUTH
    assert first_post.status == Post.Status.FAILED
    assert first_post.error == "failed: reauth required"
    assert remaining_post.status == Post.Status.FAILED
    assert remaining_post.error == "failed: reauth required"
    assert mock_post.call_count == 1


def test_scheduler_publishes_active_account_post_with_mocked_graph(user, facebook_account):
    post = Post.objects.create(
        user=user,
        account=facebook_account,
        content="Publish this post",
        status=Post.Status.SCHEDULED,
        scheduled_at=timezone.now() - timedelta(minutes=1),
    )

    with patch("socials.adapters.requests.post") as mock_post, patch(
        "socials.adapters.requests.get"
    ) as mock_get:
        mock_post.return_value.ok = True
        mock_post.return_value.json.return_value = {"id": "graph-post-123"}
        mock_get.return_value.ok = True
        mock_get.return_value.json.return_value = {
            "permalink_url": "https://www.facebook.com/posts/graph-post-123"
        }
        call_command("publish_due")

    post.refresh_from_db()
    facebook_account.refresh_from_db()
    assert post.status == Post.Status.POSTED
    assert post.external_post_id == "graph-post-123"
    assert post.permalink_url == "https://www.facebook.com/posts/graph-post-123"
    assert facebook_account.status == SocialAccount.Status.ACTIVE
    assert mock_post.call_count == 1
    assert mock_post.call_args.kwargs["data"]["message"] == "Publish this post"
    assert mock_post.call_args.kwargs["data"]["access_token"] == "super-secret-token"


def test_sync_posts_marks_graph_error_100_as_deleted_without_printing_token(user, facebook_account):
    post = Post.objects.create(
        user=user,
        account=facebook_account,
        content="Published post",
        status=Post.Status.POSTED,
        external_post_id="graph-post-100",
        posted_at=timezone.now(),
    )
    graph_response_with_error = Mock(ok=False)
    graph_response_with_error.json.return_value = {
        "error": {"code": 100, "message": "Unsupported get request."}
    }
    output = StringIO()

    with patch(
        "posts.management.commands.sync_posts.requests.get",
        return_value=graph_response_with_error,
    ) as mock_get:
        call_command("sync_posts", stdout=output)

    post.refresh_from_db()
    assert post.status == Post.Status.DELETED_ON_FACEBOOK
    assert mock_get.call_args.args[0].endswith("/graph-post-100")
    assert mock_get.call_args.kwargs["params"]["fields"] == "id"
    assert mock_get.call_args.kwargs["params"]["access_token"] == "super-secret-token"
    assert "super-secret-token" not in output.getvalue()


def test_sync_posts_graph_error_190_marks_account_for_reauth(user, facebook_account):
    post = Post.objects.create(
        user=user,
        account=facebook_account,
        content="Published post",
        status=Post.Status.POSTED,
        external_post_id="graph-post-190",
        posted_at=timezone.now(),
    )
    graph_response_with_error = Mock(ok=False)
    graph_response_with_error.json.return_value = {
        "error": {"code": 190, "message": "Invalid OAuth access token."}
    }

    with patch(
        "posts.management.commands.sync_posts.requests.get",
        return_value=graph_response_with_error,
    ):
        call_command("sync_posts", stdout=StringIO())

    facebook_account.refresh_from_db()
    post.refresh_from_db()
    assert facebook_account.status == SocialAccount.Status.NEEDS_REAUTH
    assert post.status == Post.Status.POSTED


def test_sync_posts_invalid_ciphertext_marks_account_for_reauth(user):
    account = SocialAccount.objects.create(
        user=user,
        provider=SocialAccount.Provider.FACEBOOK,
        external_id="page-invalid-sync-token",
        name="Invalid Sync Page",
        token_encrypted=b"not-fernet-ciphertext",
        status=SocialAccount.Status.ACTIVE,
    )
    Post.objects.create(
        user=user,
        account=account,
        content="Published post",
        status=Post.Status.POSTED,
        external_post_id="graph-post-invalid-token",
        posted_at=timezone.now(),
    )

    with patch("posts.management.commands.sync_posts.requests.get") as mock_get:
        call_command("sync_posts", stdout=StringIO())

    account.refresh_from_db()
    assert account.status == SocialAccount.Status.NEEDS_REAUTH
    mock_get.assert_not_called()


def test_check_tokens_marks_invalid_encrypted_token_for_reauth_and_raises(user):
    account = SocialAccount.objects.create(
        user=user,
        provider=SocialAccount.Provider.FACEBOOK,
        external_id="page-invalid-check-token",
        name="Invalid Check Token Page",
        token_encrypted=b"not-fernet-ciphertext",
        status=SocialAccount.Status.ACTIVE,
    )

    with pytest.raises(AuthExpiredError):
        CheckTokensCommand().handle()

    account.refresh_from_db()
    assert account.status == SocialAccount.Status.NEEDS_REAUTH


def test_facebook_select_get_returns_only_page_ids_and_names(user):
    client = APIClient()
    client.force_authenticate(user=user)
    set_pending_pages(client)

    response = client.get("/api/auth/connect/facebook/select/")

    assert response.status_code == 200
    assert response.data == [{"id": "page-pending-1", "name": "Pending Page"}]
    assert "pending-page-secret" not in response.content.decode()
    assert "token" not in response.content.decode().lower()


def test_facebook_select_post_ignores_unknown_ids_and_saves_known_page(user):
    client = APIClient()
    client.force_authenticate(user=user)
    set_pending_pages(client)

    response = client.post(
        "/api/auth/connect/facebook/select/",
        {"page_ids": ["page-pending-1", "unknown-page"]},
        format="json",
    )

    assert response.status_code == 200
    assert len(response.data) == 1
    saved = response.data[0]
    assert saved["name"] == "Pending Page"
    assert saved["status"] == SocialAccount.Status.ACTIVE
    assert set(saved) == {"id", "name", "status"}
    account = SocialAccount.objects.get(external_id="page-pending-1", user=user)
    assert decrypt_token(account.token_encrypted) == "pending-page-secret"
    assert not SocialAccount.objects.filter(external_id="unknown-page", user=user).exists()
    assert "pending-page-secret" not in response.content.decode()
    assert "token" not in response.content.decode().lower()
    assert "fb_pending_pages" not in client.session


@pytest.mark.parametrize(
    ("pending_pages", "page_ids"),
    [(None, ["page-pending-1"]), ("set", [])],
    ids=["no-pending-session", "empty-selection"],
)
def test_facebook_select_post_rejects_missing_pending_pages_or_empty_list(
    user, pending_pages, page_ids
):
    client = APIClient()
    client.force_authenticate(user=user)
    if pending_pages == "set":
        set_pending_pages(client)

    response = client.post(
        "/api/auth/connect/facebook/select/",
        {"page_ids": page_ids},
        format="json",
    )

    assert response.status_code == 400
    assert not SocialAccount.objects.filter(user=user).exists()


def test_callback_rejects_mismatched_state_with_frontend_redirect(user):
    client = APIClient()
    client.force_authenticate(user=user)
    session = client.session
    session["fb_oauth_state"] = "expected-state"
    session.save()

    response = client.get(
        "/api/auth/callback/facebook/",
        {"state": "wrong-state", "code": "some-code"},
    )

    assert response.status_code == 302
    assert response["Location"] == (
        f"{settings.FRONTEND_URL.rstrip('/')}/accounts?fb_error=invalid_state"
    )
    assert "fb_oauth_state" not in client.session


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
    permissions_response = Mock(ok=True)
    permissions_response.json.return_value = {
        "data": [
            {"permission": "pages_manage_posts", "status": "granted"},
            {"permission": "pages_show_list", "status": "granted"},
        ]
    }
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
        side_effect=[short_token_response, long_token_response, permissions_response, pages_response],
    ) as mock_get:
        response = client.get(
            "/api/auth/callback/facebook/",
            {"state": "expected-state", "code": "oauth-code"},
        )

    assert response.status_code == 302
    assert response["Location"] == f"{settings.FRONTEND_URL.rstrip('/')}/accounts/select"
    assert mock_get.call_args_list[0].kwargs["params"]["redirect_uri"] == settings.FB_REDIRECT_URI
    body = response.content.decode()
    assert "short-lived-secret" not in body
    assert "long-lived-secret" not in body
    assert "page-access-secret" not in body
    assert_no_tokens(response, "short-lived-secret", "long-lived-secret", "page-access-secret")


def test_facebook_connect_uses_rerequest_for_reconnect_and_declined_permissions(user, facebook_account):
    client = APIClient()
    client.force_authenticate(user=user)

    reconnect_response = client.get(
        "/api/auth/connect/facebook/",
        {"reconnect": facebook_account.id},
    )
    reconnect_params = parse_qs(urlsplit(reconnect_response["Location"]).query)
    assert "auth_type" not in reconnect_params
    assert reconnect_params["redirect_uri"] == [settings.FB_REDIRECT_URI]
    assert client.session["fb_reconnect_id"] == str(facebook_account.id)

    user.subscription.plan = "pro"
    user.subscription.status = "active"
    user.subscription.save(update_fields=["plan", "status", "updated_at"])
    session = client.session
    session["fb_rerequest"] = True
    session.save()
    rerequest_response = client.get("/api/auth/connect/facebook/")
    rerequest_params = parse_qs(urlsplit(rerequest_response["Location"]).query)
    assert rerequest_params["auth_type"] == ["rerequest"]


def test_callback_redirects_for_declined_page_permissions(user):
    client = APIClient()
    client.force_authenticate(user=user)
    session = client.session
    session["fb_oauth_state"] = "expected-state"
    session.save()

    responses = callback_responses(
        [],
        permissions=[{"permission": "pages_manage_posts", "status": "declined"}],
    )
    with patch("accounts.views.requests.get", side_effect=responses) as mock_get:
        response = callback_request(client)

    assert response.status_code == 302, response.data
    assert response["Location"] == f"{settings.FRONTEND_URL.rstrip('/')}/accounts?fb_error=permissions_declined"
    assert client.session["fb_rerequest"] is True
    assert mock_get.call_count == 3
    assert_no_tokens(response, "short-lived-secret", "long-lived-secret")


def test_callback_redirects_when_facebook_returns_no_pages(user):
    client = APIClient()
    client.force_authenticate(user=user)
    session = client.session
    session["fb_oauth_state"] = "expected-state"
    session.save()

    with patch("accounts.views.requests.get", side_effect=callback_responses([])):
        response = callback_request(client)

    assert response["Location"] == f"{settings.FRONTEND_URL.rstrip('/')}/accounts?fb_error=no_pages"
    assert_no_tokens(response, "short-lived-secret", "long-lived-secret")


def test_callback_reconnect_updates_token_and_activates_account(user, facebook_account):
    facebook_account.status = SocialAccount.Status.NEEDS_REAUTH
    facebook_account.save(update_fields=["status"])
    client = APIClient()
    client.force_authenticate(user=user)
    session = client.session
    session["fb_oauth_state"] = "expected-state"
    session["fb_reconnect_id"] = str(facebook_account.id)
    session.save()

    pages = [{"id": "page-123", "name": "Updated Page", "access_token": "new-page-secret"}]
    with patch("accounts.views.requests.get", side_effect=callback_responses(pages)):
        response = callback_request(client)

    assert response["Location"] == f"{settings.FRONTEND_URL.rstrip('/')}/accounts?fb_connected=1"
    facebook_account.refresh_from_db()
    assert facebook_account.name == "Updated Page"
    assert facebook_account.status == SocialAccount.Status.ACTIVE
    assert decrypt_token(facebook_account.token_encrypted) == "new-page-secret"
    assert facebook_account.scopes == "pages_manage_posts, pages_show_list"
    session = client.session
    assert "fb_reconnect_id" not in session
    assert "fb_rerequest" not in session
    assert "fb_pending_pages" not in session
    assert "fb_oauth_state" not in session
    assert_no_tokens(response, "short-lived-secret", "long-lived-secret", "new-page-secret")


def test_callback_reconnect_missing_page_does_not_change_account(user, facebook_account):
    facebook_account.status = SocialAccount.Status.NEEDS_REAUTH
    facebook_account.save(update_fields=["status"])
    original_name = facebook_account.name
    original_token = bytes(facebook_account.token_encrypted)
    client = APIClient()
    client.force_authenticate(user=user)
    session = client.session
    session["fb_oauth_state"] = "expected-state"
    session["fb_reconnect_id"] = str(facebook_account.id)
    session.save()

    pages = [{"id": "another-page", "name": "Other Page", "access_token": "other-page-secret"}]
    with patch("accounts.views.requests.get", side_effect=callback_responses(pages)):
        response = callback_request(client)

    assert response["Location"] == f"{settings.FRONTEND_URL.rstrip('/')}/accounts?fb_error=page_not_available"
    facebook_account.refresh_from_db()
    assert facebook_account.name == original_name
    assert bytes(facebook_account.token_encrypted) == original_token
    assert facebook_account.status == SocialAccount.Status.NEEDS_REAUTH
    assert_no_tokens(response, "short-lived-secret", "long-lived-secret", "other-page-secret")


@pytest.mark.parametrize("network_error", [False, True], ids=["graph-error", "network-error"])
def test_callback_graph_failure_redirects_without_response_details(user, network_error):
    client = APIClient()
    client.force_authenticate(user=user)
    session = client.session
    session["fb_oauth_state"] = "expected-state"
    session.save()

    if network_error:
        request_failure = requests.RequestException("long-lived-secret")
        request_patch = patch("accounts.views.requests.get", side_effect=request_failure)
    else:
        failed_response = Mock(ok=False)
        failed_response.json.return_value = {
            "error": {"message": "long-lived-secret must not appear in redirects"}
        }
        request_patch = patch("accounts.views.requests.get", return_value=failed_response)

    with request_patch:
        response = callback_request(client)

    assert response["Location"] == f"{settings.FRONTEND_URL.rstrip('/')}/accounts?fb_error=facebook_unavailable"
    assert_no_tokens(response, "long-lived-secret", "must not appear")
