from urllib.parse import urlencode

from django.conf import settings


class FacebookProvider:
    def auth_url(
        self,
        state: str,
        rerequest: bool = False,
        scopes: list[str] | None = None,
    ) -> str:
        params = {
            "client_id": settings.FB_APP_ID,
            "redirect_uri": settings.FB_REDIRECT_URI,
            "state": state,
            "scope": ",".join(
                scopes or [
                    "pages_show_list",
                    "pages_manage_posts",
                    "pages_read_engagement",
                    "read_insights",
                ]
            ),
        }
        if rerequest:
            params["auth_type"] = "rerequest"
        return (
            f"{settings.FB_DIALOG_BASE_URL}/{settings.FB_GRAPH_VERSION}/dialog/oauth"
            f"?{urlencode(params)}"
        )
