from urllib.parse import urlencode

from django.conf import settings


class FacebookProvider:
    def auth_url(self, state: str) -> str:
        params = {
            "client_id": settings.FB_APP_ID,
            "redirect_uri": settings.FB_REDIRECT_URI,
            "state": state,
            "scope": "pages_show_list,pages_manage_posts,pages_read_engagement",
        }
        return (
            f"https://www.facebook.com/{settings.FB_GRAPH_VERSION}/dialog/oauth"
            f"?{urlencode(params)}"
        )
