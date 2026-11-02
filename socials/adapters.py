import logging
from dataclasses import dataclass

import requests
from cryptography.fernet import InvalidToken
from django.conf import settings

from accounts.models import SocialAccount
from accounts.utils.crypto import decrypt_token

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FacebookPublishResult:
    external_post_id: str
    permalink_url: str | None = None


class PublishError(Exception):
    pass


class AuthExpiredError(PublishError):
    pass


def graph_error(response, token: str) -> dict:
    try:
        error_payload = response.json().get("error", {})
    except (ValueError, AttributeError, TypeError):
        error_payload = {}
    if not isinstance(error_payload, dict):
        error_payload = {}
    message = error_payload.get("message")
    if isinstance(message, str) and token:
        message = message.replace(token, "[redacted]")
    return {**error_payload, "message": message}


class FacebookAdapter:
    def post(self, account: SocialAccount, content: str) -> FacebookPublishResult:
        if account.provider != SocialAccount.Provider.FACEBOOK:
            raise PublishError(f"Unsupported provider: {account.provider}")

        try:
            token = decrypt_token(account.token_encrypted)
        except InvalidToken as exc:
            account.status = SocialAccount.Status.NEEDS_REAUTH
            account.save(update_fields=["status"])
            raise AuthExpiredError("Stored Facebook token is invalid; reauth required.") from exc

        url = (
            f"{settings.FB_GRAPH_BASE_URL}/{settings.FB_GRAPH_VERSION}/"
            f"{account.external_id}/feed"
        )
        payload = {"message": content, "access_token": token}
        if isinstance(content, dict) and "link" in content:
            payload["link"] = content["link"]
        response = requests.post(url, data=payload, timeout=20)

        if not response.ok:
            error_payload = graph_error(response, token)
            code = error_payload.get("code")
            if code == 190:
                account.status = SocialAccount.Status.NEEDS_REAUTH
                account.save(update_fields=["status"])
                raise AuthExpiredError("Facebook token expired or invalid; reauth required.")
            raise PublishError(error_payload.get("message") or response.text)

        external_post_id = response.json()["id"]
        permalink_url = None
        try:
            permalink_response = requests.get(
                f"{settings.FB_GRAPH_BASE_URL}/{settings.FB_GRAPH_VERSION}/{external_post_id}",
                params={"fields": "permalink_url", "access_token": token},
                timeout=20,
            )
            if permalink_response.ok:
                permalink_url = permalink_response.json().get("permalink_url")
        except (requests.RequestException, ValueError, AttributeError):
            pass

        return FacebookPublishResult(external_post_id, permalink_url)


ADAPTERS = {"facebook": FacebookAdapter()}
