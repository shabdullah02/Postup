import logging
import secrets
from datetime import timedelta
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from socials.models import SocialAccount
from socials.serializers import SocialAccountSerializer
from socials.throttles import AIGenerateThrottle

logger = logging.getLogger(__name__)


class SocialAccountListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        accounts = SocialAccount.objects.filter(user=request.user)
        serializer = SocialAccountSerializer(accounts, many=True)
        return Response(serializer.data)


class FacebookConnectView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not settings.FB_APP_ID or not settings.FB_APP_SECRET:
            return Response({"detail": "Facebook OAuth is not configured."}, status=503)

        state = secrets.token_urlsafe(16)
        request.session["facebook_oauth_state"] = state

        params = {
            "client_id": settings.FB_APP_ID,
            "redirect_uri": settings.FB_REDIRECT_URI,
            "scope": "pages_manage_posts,pages_show_list,pages_read_engagement",
            "response_type": "code",
            "state": state,
        }
        auth_url = (
            f"https://www.facebook.com/{settings.FB_GRAPH_VERSION}/dialog/oauth"
            f"?{urlencode(params)}"
        )
        return Response({"auth_url": auth_url})


class FacebookCallbackView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not settings.FB_APP_ID or not settings.FB_APP_SECRET:
            return Response({"detail": "Facebook OAuth is not configured."}, status=503)

        code = request.GET.get("code")
        state = request.GET.get("state")
        expected_state = request.session.get("facebook_oauth_state")
        if not code or not state or state != expected_state:
            request.session.pop("facebook_oauth_state", None)
            return Response({"detail": "Invalid Facebook OAuth state."}, status=400)

        request.session.pop("facebook_oauth_state", None)

        token_response = requests.post(
            f"https://graph.facebook.com/{settings.FB_GRAPH_VERSION}/oauth/access_token",
            data={
                "client_id": settings.FB_APP_ID,
                "client_secret": settings.FB_APP_SECRET,
                "redirect_uri": settings.FB_REDIRECT_URI,
                "code": code,
            },
            timeout=20,
        )
        if not token_response.ok:
            logger.warning("Facebook OAuth token exchange failed")
            return Response({"detail": "Facebook authentication failed."}, status=502)

        token_data = token_response.json()
        user_access_token = token_data.get("access_token")
        if not user_access_token:
            return Response({"detail": "Facebook authentication failed."}, status=400)

        pages_response = requests.get(
            f"https://graph.facebook.com/{settings.FB_GRAPH_VERSION}/me/accounts",
            params={"access_token": user_access_token},
            timeout=20,
        )
        if not pages_response.ok:
            logger.warning("Facebook pages lookup failed")
            return Response({"detail": "Unable to load Facebook Pages."}, status=502)

        pages = pages_response.json().get("data", [])
        if not pages:
            return Response({"detail": "No Facebook Pages are available for this account."}, status=400)

        selected_page_id = request.GET.get("page_id")
        selected_page = None
        if selected_page_id:
            selected_page = next(
                (page for page in pages if str(page.get("id")) == str(selected_page_id)),
                None,
            )
        elif len(pages) == 1:
            selected_page = pages[0]

        if selected_page is None:
            return Response(
                {
                    "detail": "Select a Facebook Page to link.",
                    "pages": [
                        {"id": page.get("id"), "name": page.get("name")}
                        for page in pages
                        if page.get("id") and page.get("name")
                    ],
                },
                status=400,
            )

        page_token = selected_page.get("access_token") or user_access_token
        expires_in = token_data.get("expires_in")
        expires_at = None
        if expires_in is not None:
            expires_at = timezone.now() + timedelta(seconds=int(expires_in))

        account, _ = SocialAccount.objects.get_or_create(
            user=request.user,
            platform="facebook",
            external_id=str(selected_page["id"]),
            defaults={
                "name": selected_page["name"],
                "token": page_token,
                "expires_at": expires_at,
            },
        )

        if account.name != selected_page["name"] or account.token != page_token:
            account.name = selected_page["name"]
            account.token = page_token
            account.expires_at = expires_at
            account.save(update_fields=["name", "token", "expires_at"])

        serializer = SocialAccountSerializer(account)
        return Response(serializer.data)


class AIGenerateView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [AIGenerateThrottle]

    def post(self, request):
        topic = request.data.get("topic")
        if not isinstance(topic, str) or not topic.strip():
            return Response({"topic": ["This field is required."]}, status=status.HTTP_400_BAD_REQUEST)

        try:
            from article_publisher import exceptions as article_exceptions
            from article_publisher import generate_article
        except ImportError as exc:
            logger.exception("Article publisher import failed")
            return Response(
                {"detail": "Generation failed. Try again later."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        try:
            content = generate_article(topic)
        except Exception as exc:
            exception_types = tuple(
                exception
                for exception in vars(article_exceptions).values()
                if isinstance(exception, type) and issubclass(exception, Exception)
            )
            if isinstance(exc, exception_types):
                logger.exception("Article generation failed")
                return Response(
                    {"detail": "Generation failed. Try again later."},
                    status=status.HTTP_502_BAD_GATEWAY,
                )
            raise

        return Response({"content": content})
