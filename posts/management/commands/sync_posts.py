import requests
from cryptography.fernet import InvalidToken
from django.conf import settings
from django.core.management.base import BaseCommand

from accounts.models import SocialAccount
from accounts.utils.crypto import decrypt_token
from posts.models import Post


class Command(BaseCommand):
    help = "Sync Facebook post deletion and token status."

    def handle(self, *args, **options):
        posts = Post.objects.filter(
            status=Post.Status.POSTED,
            external_post_id__gt="",
            account__status=SocialAccount.Status.ACTIVE,
            account__provider=SocialAccount.Provider.FACEBOOK,
        ).select_related("account")
        reauth_required_account_ids = set()

        for post in posts:
            account = post.account
            if account.id in reauth_required_account_ids:
                continue

            try:
                token = decrypt_token(account.token_encrypted)
            except InvalidToken:
                account.status = SocialAccount.Status.NEEDS_REAUTH
                account.save(update_fields=["status"])
                reauth_required_account_ids.add(account.id)
                continue

            try:
                response = requests.get(
                    f"https://graph.facebook.com/{settings.FB_GRAPH_VERSION}/{post.external_post_id}",
                    params={"fields": "id", "access_token": token},
                    timeout=20,
                )
            except requests.RequestException:
                continue

            try:
                payload = response.json()
                error_code = (payload.get("error") or {}).get("code")
            except (ValueError, AttributeError, TypeError):
                continue

            if error_code == 100:
                post.status = Post.Status.DELETED_ON_FACEBOOK
                post.save(update_fields=["status"])
            elif error_code == 190:
                account.status = SocialAccount.Status.NEEDS_REAUTH
                account.save(update_fields=["status"])
                reauth_required_account_ids.add(account.id)