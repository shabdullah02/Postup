from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from posts.models import Post
from socials.models import SocialAccount


class Command(BaseCommand):
    help = "Generate article content and create a scheduled or draft post."

    def add_arguments(self, parser):
        parser.add_argument("topic", type=str)
        parser.add_argument("--account", type=int, required=True)
        parser.add_argument("--schedule-at", type=str)

    def handle(self, *args, **options):
        try:
            from article_publisher import exceptions as article_exceptions
            from article_publisher import generate_article
        except ImportError as e:
            raise CommandError(str(e)) from e

        try:
            content = generate_article(options["topic"])
        except Exception as e:
            article_exceptions_list = tuple(
                exc
                for exc in vars(article_exceptions).values()
                if isinstance(exc, type) and issubclass(exc, Exception)
            )
            if isinstance(e, article_exceptions_list):
                raise CommandError(str(e)) from e
            raise

        try:
            account = SocialAccount.objects.get(pk=options["account"])
        except SocialAccount.DoesNotExist as e:
            raise CommandError(f"SocialAccount does not exist: {options['account']}") from e

        scheduled_at = None
        status = Post.Status.DRAFT
        if options["schedule_at"] is not None:
            scheduled_at = parse_datetime(options["schedule_at"])
            if scheduled_at is None:
                raise CommandError("Invalid ISO datetime for --schedule-at")
            if timezone.is_naive(scheduled_at):
                scheduled_at = timezone.make_aware(scheduled_at)
            status = Post.Status.SCHEDULED

        post = Post.objects.create(
            user=account.user,
            account=account,
            content=content,
            scheduled_at=scheduled_at,
            status=status,
        )
        self.stdout.write(str(post.pk))
