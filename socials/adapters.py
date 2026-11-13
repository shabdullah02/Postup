import logging
from dataclasses import dataclass

import requests
from cryptography.fernet import InvalidToken
from django.conf import settings
from django.utils import timezone

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


class ThreadsAdapter:
    BASE_URL = "https://graph.threads.net/v1.0"

    def post(self, account: SocialAccount, content: str) -> FacebookPublishResult:
        if account.provider != SocialAccount.Provider.THREADS:
            raise PublishError(f"Unsupported provider: {account.provider}")

        if account.token_expires_at and account.token_expires_at <= timezone.now():
            account.status = SocialAccount.Status.NEEDS_REAUTH
            account.save(update_fields=["status"])
            raise AuthExpiredError(
                "Threads token expired or invalid; reauth required."
            )

        try:
            token = decrypt_token(account.token_encrypted)
        except InvalidToken as exc:
            account.status = SocialAccount.Status.NEEDS_REAUTH
            account.save(update_fields=["status"])
            raise AuthExpiredError(
                "Stored Threads token is invalid; reauth required."
            ) from exc

        profile_url = f"{self.BASE_URL}/{account.external_id}"
        creation_response = self._post(
            f"{profile_url}/threads",
            data={
                "media_type": "TEXT",
                "text": content,
                "access_token": token,
            },
        )
        creation = self._response_data(
            creation_response, token, account, "media creation"
        )
        creation_id = creation.get("id")
        if not isinstance(creation_id, (str, int)) or not str(creation_id).strip():
            raise PublishError("Threads did not return a media container ID.")
        creation_id = str(creation_id).strip()

        publish_response = self._post(
            f"{profile_url}/threads_publish",
            data={"creation_id": creation_id, "access_token": token},
        )
        published = self._response_data(
            publish_response, token, account, "media publish"
        )
        media_id = published.get("id")
        if not isinstance(media_id, (str, int)) or not str(media_id).strip():
            raise PublishError("Threads did not return a published media ID.")
        media_id = str(media_id).strip()

        verification_response = self._get(
            f"{self.BASE_URL}/{media_id}",
            params={
                "fields": "id,permalink,text",
                "access_token": token,
            },
        )
        verified = self._response_data(
            verification_response, token, account, "media verification"
        )
        if str(verified.get("id", "")).strip() != media_id:
            raise PublishError("Threads could not verify the published media ID.")
        permalink = verified.get("permalink")
        if not isinstance(permalink, str) or not permalink.strip():
            raise PublishError("Threads did not return a permalink for published media.")

        return FacebookPublishResult(media_id, permalink)

    @staticmethod
    def _post(url, data):
        try:
            return requests.post(url, data=data, timeout=20)
        except requests.RequestException as exc:
            raise PublishError("Threads Graph API request failed.") from exc

    @staticmethod
    def _get(url, params):
        try:
            return requests.get(url, params=params, timeout=20)
        except requests.RequestException as exc:
            raise PublishError("Threads media verification request failed.") from exc

    @staticmethod
    def _response_data(response, token, account, operation):
        body = response.text
        if token:
            body = body.replace(token, "[redacted]")
        logger.debug(
            "Threads %s response status=%s body=%s",
            operation,
            response.status_code,
            body,
        )
        try:
            payload = response.json()
        except (ValueError, AttributeError) as exc:
            raise PublishError("Threads returned an invalid Graph API response.") from exc
        if not response.ok or (isinstance(payload, dict) and "error" in payload):
            error_payload = graph_error(response, token)
            if error_payload.get("code") == 190:
                account.status = SocialAccount.Status.NEEDS_REAUTH
                account.save(update_fields=["status"])
                raise AuthExpiredError(
                    "Threads token expired or invalid; reauth required."
                )
            raise PublishError(
                error_payload.get("message") or f"Threads {operation} failed."
            )
        if not isinstance(payload, dict):
            raise PublishError("Threads returned an invalid Graph API response.")
        return payload


ADAPTERS = {
    "facebook": FacebookAdapter(),
    "threads": ThreadsAdapter(),
}
