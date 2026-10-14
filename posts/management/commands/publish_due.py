from django.core.management.base import BaseCommand
from django.utils import timezone

from accounts.models import SocialAccount
from posts.models import Post
from socials.adapters import ADAPTERS, AuthExpiredError, FacebookPublishResult


class Command(BaseCommand):
    help = "Publish scheduled posts whose scheduled time has arrived."

    def handle(self, *args, **options):
        now = timezone.now()
        Post.objects.filter(
            status=Post.Status.PUBLISHING,
            claimed_at__lt=now - timezone.timedelta(minutes=10),
        ).update(
            status=Post.Status.FAILED,
            error="Publish interrupted; verify on the platform before retrying.",
        )

        post_ids = Post.objects.filter(
            status=Post.Status.SCHEDULED,
            scheduled_at__lte=now,
        ).values_list("id", flat=True)
        reauth_required_account_ids = set()

        for post_id in post_ids:
            claimed_at = timezone.now()
            updated = Post.objects.filter(
                id=post_id,
                status=Post.Status.SCHEDULED,
            ).update(
                status=Post.Status.PUBLISHING,
                claimed_at=claimed_at,
            )
            if updated == 0:
                continue

            post = Post.objects.get(pk=post_id)
            account = post.account
            if (
                account.id in reauth_required_account_ids
                or account.status != SocialAccount.Status.ACTIVE
            ):
                reauth_required_account_ids.add(account.id)
                post.status = Post.Status.FAILED
                post.error = "failed: reauth required"
                post.save(update_fields=["status", "error"])
                self.stdout.write(f"{post_id} {post.status}")
                continue

            try:
                publish_result = ADAPTERS[account.provider].post(
                    account,
                    post.content,
                )
            except AuthExpiredError:
                reauth_required_account_ids.add(account.id)
                post.status = Post.Status.FAILED
                post.error = "failed: reauth required"
                post.save(update_fields=["status", "error"])
                self.stdout.write(f"{post_id} {post.status}")
                continue
            except Exception as e:
                post.status = Post.Status.FAILED
                post.error = str(e)
                post.save(update_fields=["status", "error"])
                self.stdout.write(f"{post_id} {post.status}")
                continue

            post.status = Post.Status.POSTED
            if isinstance(publish_result, FacebookPublishResult):
                post.external_post_id = publish_result.external_post_id
                post.permalink_url = publish_result.permalink_url
            else:
                post.external_post_id = publish_result
            post.posted_at = timezone.now()
            post.save(update_fields=["status", "external_post_id", "permalink_url", "posted_at"])
            self.stdout.write(f"{post_id} {post.status}")
