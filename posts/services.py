from django.conf import settings
from django.utils import timezone

from accounts.models import SocialAccount
from accounts.providers.instagram import InstagramProvider
from posts.models import Post
from socials.adapters import (
    ADAPTERS,
    AuthExpiredError,
    FacebookPublishResult,
    PublishError,
)


def publish_post(post: Post, reauth_required_account_ids=None, adapters=None) -> Post:
    if reauth_required_account_ids is None:
        reauth_required_account_ids = set()
    if adapters is None:
        adapters = ADAPTERS
    account = post.account
    if (
        account.id in reauth_required_account_ids
        or account.status != SocialAccount.Status.ACTIVE
    ):
        reauth_required_account_ids.add(account.id)
        post.status = Post.Status.FAILED
        post.error = "failed: reauth required"
        post.save(update_fields=["status", "error"])
        return post

    if (
        account.provider == SocialAccount.Provider.INSTAGRAM
        and not settings.INSTAGRAM_ENABLED
    ):
        post.status = Post.Status.FAILED
        post.error = "failed: instagram disabled"
        post.save(update_fields=["status", "error"])
        return post

    try:
        if account.provider == SocialAccount.Provider.INSTAGRAM:
            publish_result = InstagramProvider().publish(
                account, post.content, image_url=post.image_url
            )
        else:
            adapter = adapters.get(account.provider)
            if adapter is None:
                raise PublishError(
                    f"Publishing for '{account.provider}' is not supported yet"
                )
            publish_result = adapter.post(
                account,
                post.content,
            )
        if account.provider == SocialAccount.Provider.THREADS and (
            not isinstance(publish_result, FacebookPublishResult)
            or not isinstance(publish_result.external_post_id, str)
            or not publish_result.external_post_id.strip()
            or not isinstance(publish_result.permalink_url, str)
            or not publish_result.permalink_url.strip()
        ):
            raise PublishError("Threads publish could not be verified.")
    except AuthExpiredError:
        reauth_required_account_ids.add(account.id)
        post.status = Post.Status.FAILED
        post.error = "failed: reauth required"
        post.save(update_fields=["status", "error"])
        return post
    except Exception as exc:
        post.status = Post.Status.FAILED
        post.error = str(exc)
        post.save(update_fields=["status", "error"])
        return post

    post.status = Post.Status.POSTED
    if isinstance(publish_result, FacebookPublishResult):
        post.external_post_id = publish_result.external_post_id
        post.permalink_url = publish_result.permalink_url
    else:
        post.external_post_id = publish_result
    post.posted_at = timezone.now()
    post.error = ""
    post.save(
        update_fields=[
            "status",
            "external_post_id",
            "permalink_url",
            "posted_at",
            "error",
        ]
    )
    return post