import base64
import secrets
from urllib.parse import quote

import requests
from django.conf import settings
from django.contrib.auth import authenticate, get_user_model, login, logout
from django.db import IntegrityError, transaction
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from drf_spectacular.utils import OpenApiResponse, OpenApiTypes, extend_schema_view, extend_schema
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from accounts.models import SocialAccount
from accounts.serializers import RegistrationSerializer
from accounts.utils.crypto import encrypt_token
from billing.authentication import SubscriptionSessionAuthentication
from billing.entitlements import ensure_subscription_active, ensure_within_plan_limit
from config.api import error_response
from providers.facebook import FacebookProvider


@extend_schema_view(get=extend_schema(responses={200: OpenApiTypes.OBJECT}))
class CsrfView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    @method_decorator(ensure_csrf_cookie)
    def get(self, request):
        return Response({"ok": True})


@extend_schema_view(
    post=extend_schema(
        request=OpenApiTypes.OBJECT,
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT, 403: OpenApiTypes.OBJECT},
    )
)
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
            candidate = get_user_model().objects.filter(username=username).first()
            if candidate and not candidate.is_active:
                return Response(
                    {"detail": "This account is deactivated.", "code": "account_deactivated"},
                    status=status.HTTP_403_FORBIDDEN,
                )
            return Response(
                {"detail": "Invalid credentials", "code": "invalid_credentials"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if user.subscription.status == "suspended":
            return Response(
                {"detail": "This account is suspended.", "code": "account_suspended"},
                status=status.HTTP_403_FORBIDDEN,
            )

        login(request, user)
        return Response({"id": user.id, "username": user.username})


@extend_schema_view(
    post=extend_schema(
        request=RegistrationSerializer,
        responses={201: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT, 429: OpenApiTypes.OBJECT},
    )
)
class RegistrationView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "registration"

    @method_decorator(csrf_protect)
    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)

    def post(self, request):
        serializer = RegistrationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            with transaction.atomic():
                user = serializer.save()
        except IntegrityError as exc:
            raise serializers.ValidationError(
                {"email": ["A user with that email already exists."]}
            ) from exc

        authenticated_user = authenticate(
            request,
            username=user.get_username(),
            password=serializer.validated_data["password"],
        )
        if authenticated_user is None:
            raise serializers.ValidationError("Registration could not sign in the new user.")
        login(request, authenticated_user)
        return Response(
            {"id": user.id, "username": user.get_username(), "email": user.email},
            status=status.HTTP_201_CREATED,
        )


@extend_schema_view(post=extend_schema(responses={200: OpenApiTypes.OBJECT}))
class LogoutView(APIView):
    authentication_classes = [SubscriptionSessionAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        logout(request)
        return Response({"ok": True})


@extend_schema_view(get=extend_schema(responses={200: OpenApiTypes.OBJECT}))
class MeView(APIView):
    authentication_classes = [SubscriptionSessionAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({
            "id": request.user.id,
            "username": request.user.username,
            "is_staff": request.user.is_staff,
        })


@extend_schema_view(get=extend_schema(responses={200: OpenApiTypes.OBJECT}))
class SocialAccountsListView(APIView):
    authentication_classes = [SubscriptionSessionAuthentication]
    permission_classes = [IsAuthenticated]
    pagination_class = None

    def get(self, request):
        accounts = SocialAccount.objects.filter(user=request.user).order_by("-connected_at")
        data = [
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
        ]
        return Response(data)


@extend_schema_view(
    get=extend_schema(
        responses={302: OpenApiResponse(description="Redirect to Facebook OAuth.")}
    )
)
class FacebookConnectView(APIView):
    authentication_classes = [SubscriptionSessionAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if request.GET.get("provider") == "instagram":
            if not settings.INSTAGRAM_ENABLED:
                return error_response("Instagram is not enabled.", "provider_disabled", 404)
            return error_response("Instagram not implemented yet.", "not_implemented", 501)

        ensure_subscription_active(request.user)
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
            ensure_within_plan_limit(request.user, "connected_accounts")
            request.session.pop("fb_reconnect_id", None)

        state = secrets.token_urlsafe(32)
        request.session["fb_oauth_state"] = state
        provider = FacebookProvider()
        rerequest = bool(request.session.get("fb_rerequest"))
        return HttpResponseRedirect(provider.auth_url(state, rerequest=rerequest))


@extend_schema_view(
    get=extend_schema(responses={200: OpenApiTypes.OBJECT}),
    post=extend_schema(request=OpenApiTypes.OBJECT, responses={200: OpenApiTypes.OBJECT}),
)
class FacebookSelectPagesView(APIView):
    authentication_classes = [SubscriptionSessionAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        pages = request.session.get("fb_pending_pages") or []
        return Response([
            {"id": page["id"], "name": page["name"]}
            for page in pages
        ])

    def post(self, request):
        ensure_subscription_active(request.user)
        pages = request.session.get("fb_pending_pages")
        if not pages:
            return error_response("No pending Facebook Pages.", "no_pending_pages", 400)

        selected = request.data.get("page_ids")

        if not isinstance(selected, list):
            return error_response("page_ids must be a list.", "validation_error", 400)
        if not selected:
            return error_response("page_ids must not be empty.", "validation_error", 400)

        selected_pages = [
            page for page in pages if str(page["id"]) in {str(item) for item in selected}
        ]

        existing_ids = set(
            SocialAccount.objects.filter(
                user=request.user,
                provider=SocialAccount.Provider.FACEBOOK,
                external_id__in=[page["id"] for page in selected_pages],
            ).values_list("external_id", flat=True)
        )
        additional_accounts = sum(
            page["id"] not in existing_ids for page in selected_pages
        )
        if additional_accounts:
            ensure_within_plan_limit(
                request.user, "connected_accounts", additional=additional_accounts
            )

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


@extend_schema_view(
    get=extend_schema(
        responses={302: OpenApiResponse(description="Redirect to the frontend.")}
    )
)
class FacebookCallbackView(APIView):
    authentication_classes = [SubscriptionSessionAuthentication]
    permission_classes = [IsAuthenticated]

    def _frontend_error_redirect(self, error):
        return HttpResponseRedirect(
            f"{settings.FRONTEND_URL.rstrip('/')}/accounts?fb_error={quote(error, safe='')}"
        )

    def get(self, request):
        if "error" in request.GET:
            return self._frontend_error_redirect("facebook_denied")

        state = request.GET.get("state")
        saved_state = request.session.pop("fb_oauth_state", None)
        if not state or not saved_state or state != saved_state:
            return self._frontend_error_redirect("invalid_state")

        code = request.GET.get("code")
        if not code:
            return self._frontend_error_redirect("missing_code")

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
