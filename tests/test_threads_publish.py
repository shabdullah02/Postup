import json
import logging
from datetime import timedelta
from unittest.mock import Mock, patch

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.utils import timezone

from accounts.models import SocialAccount
from accounts.utils.crypto import encrypt_token
from posts.models import Post
from posts.serializers import PostSerializer
from posts.services import publish_post
from socials.adapters import (
    ADAPTERS,
    AuthExpiredError,
    ThreadsAdapter,
)

pytestmark = pytest.mark.django_db


def graph_response(payload, ok=True):
    response = Mock(ok=ok, status_code=200 if ok else 400)
    response.json.return_value = payload
    response.text = json.dumps(payload)
    return response


@pytest.fixture
def user():
    user = get_user_model().objects.create_user(username="threads-publisher", password="pw")
    user.profile.email_verified = True
    user.profile.save(update_fields=["email_verified"])
    return user


@pytest.fixture
def threads_account(user):
    return SocialAccount.objects.create(
        user=user,
        provider=SocialAccount.Provider.THREADS,
        external_id="threads-user-123",
        name="postup_threads",
        token_encrypted=encrypt_token("threads-secret-token"),
        token_expires_at=timezone.now() + timedelta(days=30),
        status=SocialAccount.Status.ACTIVE,
    )


@pytest.fixture
def threads_post(user, threads_account):
    return Post.objects.create(
        user=user,
        account=threads_account,
        content="Hello from pytest",
    )


@pytest.fixture
def facebook_account(user):
    return SocialAccount.objects.create(
        user=user,
        provider=SocialAccount.Provider.FACEBOOK,
        external_id="page-123",
        name="My Page",
        token_encrypted=encrypt_token("facebook-secret-token"),
        status=SocialAccount.Status.ACTIVE,
    )


def test_threads_registered_in_adapters():
    assert isinstance(ADAPTERS["threads"], ThreadsAdapter)


def test_threads_success_saves_media_id_and_permalink(
    threads_post, caplog
):
    caplog.set_level(logging.DEBUG, logger="socials.adapters")
    with (
        patch("socials.adapters.requests.post") as mock_post,
        patch("socials.adapters.requests.get") as mock_get,
    ):
        mock_post.side_effect = [
            graph_response(
                {"id": "creation-123", "echo": "threads-secret-token"}
            ),
            graph_response({"id": "media-456"}),
        ]
        mock_get.return_value = graph_response(
            {
                "id": "media-456",
                "permalink": "https://www.threads.net/@postup_threads/post/media-456",
                "text": "Hello from pytest",
            }
        )

        result = publish_post(threads_post)

    threads_post.refresh_from_db()
    assert threads_post.status == Post.Status.POSTED
    assert threads_post.external_post_id == "media-456"
    assert (
        threads_post.permalink_url
        == "https://www.threads.net/@postup_threads/post/media-456"
    )
    assert mock_post.call_count == 2
    creation_call, publish_call = mock_post.call_args_list
    assert creation_call.args[0] == (
        "https://graph.threads.net/v1.0/threads-user-123/threads"
    )
    assert creation_call.kwargs["data"] == {
        "media_type": "TEXT",
        "text": "Hello from pytest",
        "access_token": "threads-secret-token",
    }
    assert creation_call.kwargs["timeout"] == 20
    assert publish_call.args[0] == (
        "https://graph.threads.net/v1.0/threads-user-123/threads_publish"
    )
    assert publish_call.kwargs["data"] == {
        "creation_id": "creation-123",
        "access_token": "threads-secret-token",
    }
    assert publish_call.kwargs["timeout"] == 20
    mock_get.assert_called_once_with(
        "https://graph.threads.net/v1.0/media-456",
        params={
            "fields": "id,permalink,text",
            "access_token": "threads-secret-token",
        },
        timeout=20,
    )
    assert "Threads media creation response status=200" in caplog.text
    assert "Threads media publish response status=200" in caplog.text
    assert "Threads media verification response status=200" in caplog.text
    assert '"id": "creation-123"' in caplog.text
    assert (
        '"permalink": "https://www.threads.net/@postup_threads/post/media-456"'
        in caplog.text
    )
    assert "threads-secret-token" not in caplog.text


def test_threads_http_200_error_json_marks_post_failed(threads_post):
    with patch("socials.adapters.requests.post") as mock_post:
        mock_post.side_effect = [
            graph_response({"id": "creation-123"}),
            graph_response(
                {"error": {"message": "Threads publish rejected"}}, ok=True
            ),
        ]

        result = publish_post(threads_post)

    threads_post.refresh_from_db()
    assert result.status == Post.Status.FAILED
    assert threads_post.status == Post.Status.FAILED
    assert threads_post.error == "Threads publish rejected"
    assert mock_post.call_count == 2


def test_threads_missing_creation_id_marks_post_failed(threads_post):
    with patch("socials.adapters.requests.post") as mock_post:
        mock_post.return_value = graph_response({})

        result = publish_post(threads_post)

    threads_post.refresh_from_db()
    assert result.status == Post.Status.FAILED
    assert threads_post.status == Post.Status.FAILED
    assert threads_post.error == "Threads did not return a media container ID."
    mock_post.assert_called_once()
    assert mock_post.call_args.args[0].endswith("/threads")


def test_threads_missing_published_media_id_marks_post_failed(threads_post):
    with patch("socials.adapters.requests.post") as mock_post:
        mock_post.side_effect = [
            graph_response({"id": "creation-123"}),
            graph_response({}),
        ]

        result = publish_post(threads_post)

    threads_post.refresh_from_db()
    assert result.status == Post.Status.FAILED
    assert threads_post.status == Post.Status.FAILED
    assert threads_post.error == "Threads did not return a published media ID."
    assert mock_post.call_count == 2
    assert mock_post.call_args.args[0].endswith("/threads_publish")


def test_threads_verification_error_marks_post_failed(threads_post):
    with (
        patch(
            "socials.adapters.requests.post",
            side_effect=[
                graph_response({"id": "creation-123"}),
                graph_response({"id": "media-456"}),
            ],
        ),
        patch("socials.adapters.requests.get") as mock_get,
    ):
        mock_get.return_value = graph_response(
            {"error": {"message": "Media could not be verified"}}, ok=True
        )

        result = publish_post(threads_post)

    threads_post.refresh_from_db()
    assert result.status == Post.Status.FAILED
    assert threads_post.status == Post.Status.FAILED
    assert threads_post.error == "Media could not be verified"
    assert threads_post.external_post_id == ""
    assert threads_post.permalink_url is None


def test_threads_error_code_190_marks_account_needs_reauth(threads_account):
    with patch("socials.adapters.requests.post") as mock_post:
        mock_post.return_value = graph_response(
            {"error": {"code": 190, "message": "Invalid OAuth access token."}},
            ok=False,
        )

        with pytest.raises(AuthExpiredError):
            ThreadsAdapter().post(threads_account, "Hello")

    threads_account.refresh_from_db()
    assert threads_account.status == SocialAccount.Status.NEEDS_REAUTH


def test_threads_expired_token_marks_account_needs_reauth_without_requests(
    threads_account,
):
    threads_account.token_expires_at = timezone.now() - timedelta(minutes=1)
    threads_account.save(update_fields=["token_expires_at"])

    with patch("socials.adapters.requests.post") as mock_post:
        with pytest.raises(AuthExpiredError):
            ThreadsAdapter().post(threads_account, "Hello")

    threads_account.refresh_from_db()
    assert threads_account.status == SocialAccount.Status.NEEDS_REAUTH
    mock_post.assert_not_called()


def test_serializer_accepts_threads_text_at_500_character_limit(
    user, threads_account
):
    serializer = PostSerializer(
        data={"account": threads_account.pk, "content": "a" * 500},
        context={"request": Mock(user=user)},
    )

    assert serializer.is_valid(), serializer.errors


def test_serializer_rejects_threads_text_over_500_characters(user, threads_account):
    serializer = PostSerializer(
        data={"account": threads_account.pk, "content": "a" * 501},
        context={"request": Mock(user=user)},
    )

    assert not serializer.is_valid()
    assert "content" in serializer.errors
    assert "500 characters" in str(serializer.errors["content"])


def test_serializer_rejects_empty_threads_text(user, threads_account):
    serializer = PostSerializer(
        data={"account": threads_account.pk, "content": "   "},
        context={"request": Mock(user=user)},
    )

    assert not serializer.is_valid()
    assert "content" in serializer.errors


def test_publish_due_threads_failure_does_not_block_facebook(
    user, threads_account, facebook_account
):
    threads_post = Post.objects.create(
        user=user,
        account=threads_account,
        content="Threads post",
        status=Post.Status.SCHEDULED,
        scheduled_at=timezone.now() - timedelta(minutes=1),
    )
    facebook_post = Post.objects.create(
        user=user,
        account=facebook_account,
        content="Facebook post",
        status=Post.Status.SCHEDULED,
        scheduled_at=timezone.now() - timedelta(minutes=1),
    )

    def fake_post(url, data=None, timeout=None):
        if "graph.threads.net" in url:
            return graph_response(
                {"error": {"message": "Threads is unavailable"}}, ok=False
            )
        return graph_response({"id": "facebook-post-1"})

    with (
        patch("socials.adapters.requests.post", side_effect=fake_post),
        patch("socials.adapters.requests.get") as mock_get,
    ):
        mock_get.return_value = graph_response({}, ok=False)
        call_command("publish_due")

    threads_post.refresh_from_db()
    facebook_post.refresh_from_db()
    assert threads_post.status == Post.Status.FAILED
    assert threads_post.error == "Threads is unavailable"
    assert facebook_post.status == Post.Status.POSTED
    assert facebook_post.external_post_id == "facebook-post-1"


def test_unknown_provider_fails_with_clear_message_instead_of_keyerror(user):
    account = SocialAccount.objects.create(
        user=user,
        provider="mastodon",
        external_id="mastodon-1",
        name="Mastodon",
        token_encrypted=encrypt_token("mastodon-token"),
        status=SocialAccount.Status.ACTIVE,
    )
    post = Post.objects.create(user=user, account=account, content="Hello")

    publish_post(post)

    post.refresh_from_db()
    assert post.status == Post.Status.FAILED
    assert post.error == "Publishing for 'mastodon' is not supported yet"
