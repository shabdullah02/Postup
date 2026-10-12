from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
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

    def _get_fernet(self):
        key = getattr(settings, "TOKEN_ENCRYPTION_KEY", None)
        if not key:
            raise ImproperlyConfigured("TOKEN_ENCRYPTION_KEY must be configured.")
        return Fernet(key.encode())

    def encrypt_token(self, raw_token):
        if not raw_token:
            return ""
        return self._get_fernet().encrypt(raw_token.encode()).decode()

    def get_decrypted_token(self):
        if not self.token:
            return ""
        try:
            return self._get_fernet().decrypt(self.token.encode()).decode()
        except InvalidToken:
            return self.token

    def save(self, *args, **kwargs):
        if self.token and not self.token.startswith("gAAAAA"):
            self.token = self.encrypt_token(self.token)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.platform}:{self.name}"

