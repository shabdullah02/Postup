import time

import requests
from cryptography.fernet import InvalidToken
from django.conf import settings

from accounts.models import SocialAccount
from accounts.utils.crypto import decrypt_token
from socials.adapters import (
    AuthExpiredError,
    FacebookPublishResult,
    PublishError,
    graph_error,
)


class InstagramProvider:
    """Publish media to Instagram Professional accounts through the Graph API."""

    MAX_STATUS_POLLS = 10
    STATUS_POLL_INTERVAL = 2
    STATUS_POLL_TIMEOUT = 60

    def auth_scopes(self):
        return [
            "instagram_basic",
            "instagram_content_publish",
            "pages_show_list",
            "pages_read_engagement",
        ]

    def list_instagram_accounts(self, user_token):
        response = self._request(
            requests.get,
            f"{settings.FB_GRAPH_BASE_URL}/{settings.FB_GRAPH_VERSION}/me/accounts",
            params={
                "fields": "id,name,access_token,instagram_business_account{id,username}",
                "access_token": user_token,
            },
            timeout=20,
        )
        if not response.ok:
            error_payload = graph_error(response, user_token)
            raise PublishError(
                error_payload.get("message") or "Instagram Graph API request failed."
            )

        try:
            pages = response.json().get("data", [])
        except (ValueError, AttributeError) as exc:
            raise PublishError("Instagram account response was invalid.") from exc
        if not isinstance(pages, list):
            raise PublishError("Instagram account response was invalid.")

        accounts = []
        for page in pages:
            if not isinstance(page, dict):
                continue
            instagram_account = page.get("instagram_business_account")
            if not isinstance(instagram_account, dict):
                continue
            account_id = instagram_account.get("id")
            username = instagram_account.get("username")
            page_token = page.get("access_token")
            if account_id and username and page_token:
                accounts.append({
                    "id": str(account_id),
                    "username": username,
                    "page_id": str(page.get("id", "")),
                    "access_token": page_token,
                })
        return accounts

    def publish(self, account: SocialAccount, content: str, image_url: str | None = None):
        if not image_url:
            raise PublishError("Instagram posts require an image URL.")

        try:
            token = decrypt_token(account.token_encrypted)
        except (InvalidToken, ValueError) as exc:
            account.status = SocialAccount.Status.NEEDS_REAUTH
            account.save(update_fields=["status"])
            raise AuthExpiredError(
                "Stored Instagram token is invalid; reauth required."
            ) from exc

        graph_url = f"{settings.FB_GRAPH_BASE_URL}/{settings.FB_GRAPH_VERSION}"
        media_response = self._request(
            requests.post,
            f"{graph_url}/{account.external_id}/media",
            data={
                "caption": content,
                "image_url": image_url,
                "access_token": token,
            },
            timeout=20,
        )
        media = self._response_data(media_response, token, account)
        creation_id = media.get("id")
        if not creation_id:
            raise PublishError("Instagram did not return a media container ID.")

        deadline = time.monotonic() + self.STATUS_POLL_TIMEOUT
        for poll_index in range(self.MAX_STATUS_POLLS):
            remaining_time = deadline - time.monotonic()
            if remaining_time <= 0:
                raise PublishError("Instagram media container processing timed out.")
            status_response = self._request(
                requests.get,
                f"{graph_url}/{creation_id}",
                params={"fields": "status_code", "access_token": token},
                timeout=min(20, remaining_time),
            )
            container = self._response_data(status_response, token, account)
            status_code = container.get("status_code")
            if status_code == "FINISHED":
                break
            if status_code in {"ERROR", "EXPIRED"}:
                raise PublishError(
                    f"Instagram media container processing failed ({status_code})."
                )
            if (
                poll_index + 1 >= self.MAX_STATUS_POLLS
                or time.monotonic() >= deadline
            ):
                raise PublishError("Instagram media container processing timed out.")
            time.sleep(min(self.STATUS_POLL_INTERVAL, deadline - time.monotonic()))
        else:
            raise PublishError("Instagram media container processing timed out.")

        publish_response = self._request(
            requests.post,
            f"{graph_url}/{account.external_id}/media_publish",
            data={"creation_id": creation_id, "access_token": token},
            timeout=20,
        )
        published_media = self._response_data(publish_response, token, account)
        media_id = published_media.get("id")
        if not media_id:
            raise PublishError("Instagram did not return a published media ID.")

        permalink_url = None
        try:
            permalink_response = self._request(
                requests.get,
                f"{graph_url}/{media_id}",
                params={"fields": "permalink", "access_token": token},
                timeout=20,
            )
            if permalink_response.ok:
                permalink_url = permalink_response.json().get("permalink")
            else:
                self._response_data(permalink_response, token, account)
        except (requests.RequestException, ValueError, AttributeError):
            pass

        return FacebookPublishResult(str(media_id), permalink_url)

    @staticmethod
    def _request(request_method, *args, **kwargs):
        try:
            return request_method(*args, **kwargs)
        except requests.RequestException as exc:
            raise PublishError("Instagram Graph API request failed.") from exc

    @staticmethod
    def _response_data(response, token, account=None):
        try:
            payload = response.json()
        except (ValueError, AttributeError) as exc:
            raise PublishError("Instagram returned an invalid Graph API response.") from exc
        if not response.ok or (isinstance(payload, dict) and "error" in payload):
            error_payload = graph_error(response, token)
            if error_payload.get("code") == 190:
                if account is not None:
                    account.status = SocialAccount.Status.NEEDS_REAUTH
                    account.save(update_fields=["status"])
                raise AuthExpiredError(
                    "Instagram token expired or invalid; reauth required."
                )
            message = error_payload.get("message")
            raise PublishError(message or "Instagram Graph API request failed.")
        if not isinstance(payload, dict):
            raise PublishError("Instagram returned an invalid Graph API response.")
        return payload
