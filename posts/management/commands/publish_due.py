from django.core.management.base import BaseCommand
from django.utils import timezone

from posts.models import Post
from posts.services import publish_post
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
            publish_post(post, reauth_required_account_ids, ADAPTERS)
            self.stdout.write(f"{post_id} {post.status}")
