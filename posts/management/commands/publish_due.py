from django.core.management.base import BaseCommand
from django.utils import timezone

from posts.models import Post
from socials.adapters import ADAPTERS


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
            try:
                external_post_id = ADAPTERS[post.account.platform].post(
                    post.account,
                    post.content,
                )
            except Exception as e:
                post.status = Post.Status.FAILED
                post.error = str(e)
                post.save(update_fields=["status", "error"])
                self.stdout.write(f"{post_id} {post.status}")
                continue

            post.status = Post.Status.POSTED
            post.external_post_id = external_post_id
            post.posted_at = timezone.now()
            post.save(update_fields=["status", "external_post_id", "posted_at"])
            self.stdout.write(f"{post_id} {post.status}")
