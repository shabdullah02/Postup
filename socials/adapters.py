import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


class PublishError(Exception):
    pass


class FacebookAdapter:
    def post(self, account, content: str) -> str:
        url = (
            f"https://graph.facebook.com/{settings.FACEBOOK_GRAPH_VERSION}/"
            f"{account.external_id}/feed"
        )
        response = requests.post(
            url,
            data={"message": content, "access_token": account.token},
            timeout=20,
        )

        if not response.ok:
            raise PublishError(f"{response.status_code}: {response.text}")

        return response.json()["id"]


ADAPTERS = {"facebook": FacebookAdapter()}
