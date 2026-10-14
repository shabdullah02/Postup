from datetime import timedelta
from io import StringIO
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock, patch

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import SocialAccount
from accounts.utils.crypto import decrypt_token
from posts.models import Post

pytestmark = pytest.mark.django_db


def graph_response(payload, ok=True):
    response = Mock(ok=ok)
    response.json.return_value = payload
    return response


def oauth_responses(pages, user_token):
    return [
        graph_response({"access_token": "short-lived-secret"}),
        graph_response({"access_token": user_token}),
        graph_response(
            {
                "data": [
                    {"permission": "pages_show_list", "status": "granted"},
                    {"permission": "pages_manage_posts", "status": "granted"},
                ]
            }
        ),
        graph_response({"data": pages}),
    ]


def oauth_callback(client, state):
    return client.get(
        "/api/auth/callback/facebook/",
        {"state": state, "code": "fresh-user-oauth-code"},
    )


def oauth_state_from_connect(response):
    params = parse_qs(urlsplit(response["Location"]).query)
    return params["state"][0]


def assert_no_token_leaks(responses, tokens):
    for response in responses:
        body = response.content.decode()
        redirect_url = response.get("Location", "")
        assert "token_encrypted" not in body
        assert "access_token" not in body
        assert "access_token" not in redirect_url
        for token in tokens:
            assert token not in body
            assert token not in redirect_url


def test_new_user_facebook_connect_publish_expiry_and_reconnect():
    user = get_user_model().objects.create_user(
        username="fresh-facebook-user",
        password="test-password",
    )
    client = APIClient()
    client.force_authenticate(user=user)
    responses = []
    token_values = [
        "short-lived-secret",
        "first-user-token",
        "first-page-token",
        "second-user-token",
        "second-page-token",
    ]

    first_connect = client.get("/api/auth/connect/facebook/")
    responses.append(first_connect)
    first_state = oauth_state_from_connect(first_connect)

    with patch(
        "accounts.views.requests.get",
        side_effect=oauth_responses([], "first-user-token"),
    ):
        no_pages_response = oauth_callback(client, first_state)
    responses.append(no_pages_response)
    assert no_pages_response.status_code == 302
    assert no_pages_response["Location"] == (
        f"{settings.FRONTEND_URL.rstrip('/')}/accounts?fb_error=no_pages"
    )

    second_connect = client.get("/api/auth/connect/facebook/")
    responses.append(second_connect)
    second_state = oauth_state_from_connect(second_connect)
    first_page = {
        "id": "fresh-page-1",
        "name": "Fresh User Page",
        "access_token": "first-page-token",
    }
    with patch(
        "accounts.views.requests.get",
        side_effect=oauth_responses([first_page], "first-user-token"),
    ):
        select_redirect = oauth_callback(client, second_state)
    responses.append(select_redirect)
    assert select_redirect.status_code == 302
    assert select_redirect["Location"] == f"{settings.FRONTEND_URL.rstrip('/')}/accounts/select"

    selection_response = client.post(
        "/api/auth/connect/facebook/select/",
        {"page_ids": ["fresh-page-1"]},
        format="json",
    )
    responses.append(selection_response)
    assert selection_response.status_code == 200
    assert selection_response.data[0]["status"] == SocialAccount.Status.ACTIVE
    account = SocialAccount.objects.get(user=user, external_id="fresh-page-1")
    account_id = account.id
    assert account.status == SocialAccount.Status.ACTIVE
    assert decrypt_token(account.token_encrypted) == "first-page-token"

    first_post = Post.objects.create(
        user=user,
        account=account,
        content="First scheduled post",
        status=Post.Status.SCHEDULED,
        scheduled_at=timezone.now() - timedelta(minutes=1),
    )
    with (
        patch("socials.adapters.requests.post") as publish_request,
        patch("socials.adapters.requests.get") as permalink_request,
    ):
        publish_request.return_value.ok = True
        publish_request.return_value.json.return_value = {"id": "fresh-graph-post-1"}
        permalink_request.return_value.ok = True
        permalink_request.return_value.json.return_value = {
            "permalink_url": "https://www.facebook.com/posts/fresh-graph-post-1"
        }
        call_command("publish_due", stdout=StringIO())

    first_post.refresh_from_db()
    assert first_post.status == Post.Status.POSTED
    assert first_post.external_post_id == "fresh-graph-post-1"
    assert first_post.permalink_url == "https://www.facebook.com/posts/fresh-graph-post-1"

    second_post = Post.objects.create(
        user=user,
        account=account,
        content="Second scheduled post",
        status=Post.Status.SCHEDULED,
        scheduled_at=timezone.now() - timedelta(minutes=1),
    )
    with patch("socials.adapters.requests.post") as publish_request:
        publish_request.return_value.ok = False
        publish_request.return_value.json.return_value = {
            "error": {"code": 190, "message": "Expired token"}
        }
        call_command("publish_due", stdout=StringIO())

    account.refresh_from_db()
    second_post.refresh_from_db()
    assert account.status == SocialAccount.Status.NEEDS_REAUTH
    assert second_post.status == Post.Status.FAILED
    assert second_post.error == "failed: reauth required"

    reconnect_response = client.get(
        "/api/auth/connect/facebook/",
        {"reconnect": account.id},
    )
    responses.append(reconnect_response)
    reconnect_state = oauth_state_from_connect(reconnect_response)
    refreshed_page = {
        "id": "fresh-page-1",
        "name": "Fresh User Page",
        "access_token": "second-page-token",
    }
    with patch(
        "accounts.views.requests.get",
        side_effect=oauth_responses([refreshed_page], "second-user-token"),
    ):
        reconnect_callback = oauth_callback(client, reconnect_state)
    responses.append(reconnect_callback)

    account.refresh_from_db()
    assert reconnect_callback.status_code == 302
    assert reconnect_callback["Location"] == (
        f"{settings.FRONTEND_URL.rstrip('/')}/accounts?fb_connected=1"
    )
    assert account.id == account_id
    assert account.status == SocialAccount.Status.ACTIVE
    assert decrypt_token(account.token_encrypted) == "second-page-token"
    assert_no_token_leaks(responses, token_values)
