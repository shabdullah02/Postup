from django.conf import settings
from django.db import models

from accounts.models import SocialAccount


class Post(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SCHEDULED = "scheduled", "Scheduled"
        PUBLISHING = "publishing", "Publishing"
        POSTED = "posted", "Posted"
        FAILED = "failed", "Failed"
        DELETED_ON_FACEBOOK = "deleted_on_facebook", "Deleted on Facebook"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    account = models.ForeignKey(SocialAccount, on_delete=models.CASCADE)
    content = models.TextField()
    scheduled_at = models.DateTimeField(null=True, blank=True, db_index=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.DRAFT,
    )
    claimed_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True)
    external_post_id = models.CharField(max_length=64, blank=True)
    permalink_url = models.URLField(max_length=500, null=True, blank=True)
    posted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

