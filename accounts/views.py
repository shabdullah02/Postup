import base64
import secrets

import requests
from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from rest_framework import status
from rest_framework.authentication import SessionAuthentication
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import SocialAccount
from accounts.utils.crypto import encrypt_token
from providers.facebook import FacebookProvider


class CsrfView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    @method_decorator(ensure_csrf_cookie)
    def get(self, request):
        return Response({"ok": True})


class LoginView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    @method_decorator(csrf_protect)
    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)

    def post(self, request):
        username = request.data.get("username")
        password = request.data.get("password")
        user = authenticate(request, username=username, password=password)

        if user is None:
            return Response(
                {"detail": "Invalid credentials"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        login(request, user)
        return Response({"id": user.id, "username": user.username})


class LogoutView(APIView):
    authentication_classes = [SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        logout(request)
        return Response({"ok": True})


class MeView(APIView):
    authentication_classes = [SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({"id": request.user.id, "username": request.user.username})


class SocialAccountsListView(APIView):
    authentication_classes = [SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        accounts = SocialAccount.objects.filter(user=request.user).order_by("-connected_at")
        return Response([
            {
                "id": account.id,
                "provider": account.provider,
                "external_id": account.external_id,
                "name": account.name,
                "status": account.status,
                "scopes": account.scopes,
                "connected_at": account.connected_at,
            }
            for account in accounts
        ])


class FacebookConnectView(APIView):
    authentication_classes = [SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        reconnect_id = request.GET.get("reconnect")
        if reconnect_id:
            get_object_or_404(
                SocialAccount,
                id=reconnect_id,
                user=request.user,
                provider=SocialAccount.Provider.FACEBOOK,
            )
            request.session["fb_reconnect_id"] = reconnect_id
        else:
            request.session.pop("fb_reconnect_id", None)

        state = secrets.token_urlsafe(32)
        request.session["fb_oauth_state"] = state
        provider = FacebookProvider()
        rerequest = bool(request.session.get("fb_rerequest"))
        return HttpResponseRedirect(provider.auth_url(state, rerequest=rerequest))


class FacebookSelectPagesView(APIView):
    authentication_classes = [SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        pages = request.session.get("fb_pending_pages") or []
        return Response([
            {"id": page["id"], "name": page["name"]}
            for page in pages
        ])

    def post(self, request):
        selected = request.data.get("page_ids") or []
        pages = request.session.get("fb_pending_pages") or []

        if not isinstance(selected, list):
            return Response({"detail": "page_ids must be a list."}, status=400)

        selected_pages = [
            page for page in pages if str(page["id"]) in {str(item) for item in selected}
        ]

        saved = []
        for page in selected_pages:
            account, _ = SocialAccount.objects.update_or_create(
                user=request.user,
                provider=SocialAccount.Provider.FACEBOOK,
                external_id=page["id"],
                defaults={
                    "name": page["name"],
                    "token_encrypted": base64.b64decode(page["token_encrypted"]),
                    "status": SocialAccount.Status.ACTIVE,
                    "scopes": page.get("scopes", ""),
                },
            )
            saved.append({
                "id": account.id,
                "name": account.name,
                "status": account.status,
            })

        request.session.pop("fb_pending_pages", None)
        return Response(saved)


class FacebookCallbackView(APIView):
    authentication_classes = [SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def _frontend_error_redirect(self, error):
        return HttpResponseRedirect(
            f"{settings.FRONTEND_URL.rstrip('/')}/accounts?fb_error={error}"
        )

    def get(self, request):
        if request.GET.get("error"):
            description = request.GET.get("error_description") or request.GET.get("error")
            return Response({"detail": description}, status=400)

        state = request.GET.get("state")
        saved_state = request.session.pop("fb_oauth_state", None)
        if not state or not saved_state or state != saved_state:
            return Response({"detail": "Invalid Facebook OAuth state."}, status=400)

        code = request.GET.get("code")
        if not code:
            return Response({"detail": "Missing Facebook OAuth code."}, status=400)

        try:
            short_token_response = requests.get(
                f"https://graph.facebook.com/{settings.FB_GRAPH_VERSION}/oauth/access_token",
                params={
                    "client_id": settings.FB_APP_ID,
                    "client_secret": settings.FB_APP_SECRET,
                    "redirect_uri": settings.FB_REDIRECT_URI,
                    "code": code,
                },
                timeout=20,
            )
            if not short_token_response.ok:
                return self._frontend_error_redirect("facebook_unavailable")

            short_token = short_token_response.json().get("access_token")
            if not short_token:
                return self._frontend_error_redirect("facebook_unavailable")

            long_token_response = requests.get(
                f"https://graph.facebook.com/{settings.FB_GRAPH_VERSION}/oauth/access_token",
                params={
                    "grant_type": "fb_exchange_token",
                    "client_id": settings.FB_APP_ID,
                    "client_secret": settings.FB_APP_SECRET,
                    "fb_exchange_token": short_token,
                },
                timeout=20,
            )
            if not long_token_response.ok:
                return self._frontend_error_redirect("facebook_unavailable")

            long_token = long_token_response.json().get("access_token")
            if not long_token:
                return self._frontend_error_redirect("facebook_unavailable")

            permissions_response = requests.get(
                f"https://graph.facebook.com/{settings.FB_GRAPH_VERSION}/me/permissions",
                params={"access_token": long_token},
                timeout=20,
            )
            if not permissions_response.ok:
                return self._frontend_error_redirect("facebook_unavailable")

            permission_data = permissions_response.json().get("data", [])
            if not isinstance(permission_data, list):
                return self._frontend_error_redirect("facebook_unavailable")
        except (requests.RequestException, ValueError, AttributeError, TypeError):
            return self._frontend_error_redirect("facebook_unavailable")

        granted_permissions = [
            permission["permission"]
            for permission in permission_data
            if isinstance(permission, dict)
            and permission.get("status") == "granted"
            and isinstance(permission.get("permission"), str)
        ]
        permissions = set(granted_permissions)
        required_permissions = {"pages_manage_posts", "pages_show_list"}
        if not required_permissions.issubset(permissions):
            request.session["fb_rerequest"] = True
            return self._frontend_error_redirect("permissions_declined")
        request.session.pop("fb_rerequest", None)
        granted_scopes = ", ".join(granted_permissions)

        try:
            pages_response = requests.get(
                f"https://graph.facebook.com/{settings.FB_GRAPH_VERSION}/me/accounts",
                params={"access_token": long_token},
                timeout=20,
            )
            if not pages_response.ok:
                return self._frontend_error_redirect("facebook_unavailable")

            pages = pages_response.json().get("data", [])
            if not isinstance(pages, list):
                return self._frontend_error_redirect("facebook_unavailable")
        except (requests.RequestException, ValueError, AttributeError, TypeError):
            return self._frontend_error_redirect("facebook_unavailable")
        if any(not isinstance(page, dict) for page in pages):
            return self._frontend_error_redirect("facebook_unavailable")
        if not pages:
            return self._frontend_error_redirect("no_pages")

        reconnect_id = request.session.get("fb_reconnect_id")
        reconnect_account = None
        if reconnect_id:
            try:
                reconnect_account_id = int(reconnect_id)
            except (TypeError, ValueError) as exc:
                raise Http404 from exc
            reconnect_account = get_object_or_404(
                SocialAccount,
                id=reconnect_account_id,
                user=request.user,
                provider=SocialAccount.Provider.FACEBOOK,
            )

        if reconnect_account and not any(
            str(page.get("id")) == reconnect_account.external_id for page in pages
        ):
            return self._frontend_error_redirect("page_not_available")

        pending_pages = []
        for page in pages:
            page_id = page.get("id")
            page_name = page.get("name")
            page_token = page.get("access_token")
            if not page_id or not page_name or not page_token:
                continue
            pending_pages.append(
                {
                    "id": str(page_id),
                    "name": page_name,
                    "token_encrypted": base64.b64encode(encrypt_token(page_token)).decode(),
                    "scopes": granted_scopes,
                }
            )

        if not pending_pages:
            return self._frontend_error_redirect("facebook_unavailable")

        if reconnect_account:
            matching_page = next(
                (page for page in pending_pages if page["id"] == reconnect_account.external_id),
                None,
            )
            if not matching_page:
                return self._frontend_error_redirect("facebook_unavailable")

            SocialAccount.objects.update_or_create(
                user=request.user,
                provider=SocialAccount.Provider.FACEBOOK,
                external_id=reconnect_account.external_id,
                defaults={
                    "name": matching_page["name"],
                    "token_encrypted": base64.b64decode(matching_page["token_encrypted"]),
                    "status": SocialAccount.Status.ACTIVE,
                    "scopes": granted_scopes,
                },
            )
            request.session.pop("fb_reconnect_id", None)
            request.session.pop("fb_rerequest", None)
            request.session.pop("fb_pending_pages", None)
            request.session.pop("fb_oauth_state", None)
            return HttpResponseRedirect(
                f"{settings.FRONTEND_URL.rstrip('/')}/accounts?fb_connected=1"
            )

        request.session["fb_pending_pages"] = pending_pages
        return HttpResponseRedirect(f"{settings.FRONTEND_URL.rstrip('/')}/accounts/select")
