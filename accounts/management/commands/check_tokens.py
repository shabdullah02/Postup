from django.core.management.base import BaseCommand
from django.conf import settings

import requests
from cryptography.fernet import InvalidToken

from accounts.models import SocialAccount
from accounts.utils.crypto import decrypt_token
from socials.adapters import AuthExpiredError


class Command(BaseCommand):
    help = "Check validity of active social account tokens and mark invalid ones for reauth."

    def handle(self, *args, **options):
        for account in SocialAccount.objects.filter(status=SocialAccount.Status.ACTIVE):
            if account.provider != SocialAccount.Provider.FACEBOOK:
                continue

            try:
                token = decrypt_token(account.token_encrypted)
            except InvalidToken as exc:
                account.status = SocialAccount.Status.NEEDS_REAUTH
                account.save(update_fields=["status"])
                raise AuthExpiredError(
                    "Stored Facebook token is invalid; reauth required."
                ) from exc

            url = f"https://graph.facebook.com/{settings.FB_GRAPH_VERSION}/debug_token"
            response = requests.get(
                url,
                params={
                    "input_token": token,
                    "access_token": f"{settings.FB_APP_ID}|{settings.FB_APP_SECRET}",
                },
                timeout=20,
            )

            payload = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
            data = payload.get("data", {})
            is_valid = bool(data.get("is_valid"))
            expires_at = data.get("expires_at")
            scopes = ", ".join(data.get("scopes", [])) if isinstance(data.get("scopes"), list) else ""

            self.stdout.write(
                f"{account.name}: is_valid={is_valid}, expires_at={expires_at}, scopes={scopes}"
            )

            if not is_valid:
                account.status = SocialAccount.Status.NEEDS_REAUTH
                account.save(update_fields=["status"])
