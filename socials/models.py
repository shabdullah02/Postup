from django.conf import settings
from django.db import models


class SocialAccount(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    platform = models.CharField(max_length=20)
    external_id = models.CharField(max_length=64)
    name = models.CharField(max_length=255)
    token = models.TextField()
    expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("user", "platform", "external_id")

    def __str__(self):
        return f"{self.platform}:{self.name}"

