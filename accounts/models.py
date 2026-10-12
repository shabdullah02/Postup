from django.conf import settings
from django.db import models


class SocialAccount(models.Model):
    class Provider(models.TextChoices):
        FACEBOOK = "facebook", "Facebook"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        NEEDS_REAUTH = "needs_reauth", "Needs reauth"
        DISCONNECTED = "disconnected", "Disconnected"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="accounts_social_accounts",
    )
    provider = models.CharField(max_length=20, choices=Provider.choices)
    external_id = models.CharField(max_length=255)
    name = models.CharField(max_length=255)
    token_encrypted = models.BinaryField()
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.ACTIVE,
    )
    scopes = models.TextField(blank=True)
    connected_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("user", "provider", "external_id")

    def __str__(self):
        return f"{self.provider}:{self.name}"
