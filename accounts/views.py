import base64
import hashlib
import secrets
from datetime import timedelta
from urllib.parse import quote, urlencode

import requests
from django.conf import settings
from django.contrib.auth import authenticate, get_user_model, login, logout
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.tokens import default_token_generator
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from django.db import IntegrityError, transaction
from django.core import signing
from django.core.cache import cache
from django.contrib.sessions.models import Session
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.utils.decorators import method_decorator
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from drf_spectacular.utils import OpenApiResponse, OpenApiTypes, extend_schema_view, extend_schema
from rest_framework import serializers, status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from accounts.emails import (
    send_password_changed_email,
    send_password_reset_email,
    send_verification_email,
)
from accounts.email_tokens import read_verification_token
from accounts.models import SocialAccount, get_profile
from accounts.providers.instagram import InstagramProvider
from accounts.serializers import (
    ChangePasswordSerializer,
    ProfileSerializer,
    RegistrationSerializer,
)
from accounts.utils.crypto import encrypt_token
from accounts.verification import ensure_email_verified
from accounts.two_factor import (
    PENDING_LOGIN_EXPIRES_KEY,
    PENDING_LOGIN_KEY,
    clear_totp_failures,
    consume_recovery_code,
    generate_recovery_codes,
    pending_login_expiry,
    record_totp_failure,
    store_totp_secret,
    totp_attempts_exhausted,
    verify_totp,
)
from accounts.throttles import SuperuserExemptScopedRateThrottle
from billing.authentication import SubscriptionSessionAuthentication
from billing.entitlements import (
    ensure_subscription_active,
    ensure_within_plan_limit,
    get_subscription,
    is_exempt,
)
from billing.models import AdminAuditLog
from billing.stripe_utils import get_stripe
from config.api import error_response
from providers.facebook import FacebookProvider


def _login_lockout_key(username, ip_address):
    normalized_username = str(username or "").casefold().encode()
    username_digest = hashlib.sha256(normalized_username).hexdigest()
    ip_digest = hashlib.sha256(str(ip_address or "").encode()).hexdigest()
    return f"postup:login-fail:{username_digest}:{ip_digest}"


def _threads_oauth_cache_key(state):
    digest = hashlib.sha256(state.encode("utf-8")).hexdigest()
    return f"postup:threads-oauth:{digest}"


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
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"

    @method_decorator(csrf_protect)
    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)

    def post(self, request):
        username = request.data.get("username")
        password = request.data.get("password")
        ip_address = request.META.get("REMOTE_ADDR", "")
        lockout_key = _login_lockout_key(username, ip_address)
        if cache.get(lockout_key, 0) >= 5:
            return error_response("Too many failed login attempts. Try again later.", "login_locked", 429)
        user = authenticate(request, username=username, password=password)

        if user is None:
            cache.set(lockout_key, cache.get(lockout_key, 0) + 1, 15 * 60)
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

            cache.delete(lockout_key)

        if not is_exempt(user) and get_subscription(user).status == "suspended":
            return Response(
                {"detail": "This account is suspended.", "code": "account_suspended"},
                status=status.HTTP_403_FORBIDDEN,
            )

        if not is_exempt(user) and get_profile(user).totp_enabled:
            logout(request)
            request.session[PENDING_LOGIN_KEY] = user.pk
            request.session[PENDING_LOGIN_EXPIRES_KEY] = pending_login_expiry()
            return Response({"two_factor_required": True})

        login(request, user)
        return Response({"id": user.id, "username": user.username})


@extend_schema_view(
    post=extend_schema(
        request=OpenApiTypes.OBJECT,
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT, 429: OpenApiTypes.OBJECT},
    )
)
class TwoFactorLoginView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    @method_decorator(csrf_protect)
    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)

    def post(self, request):
        user_id = request.session.get(PENDING_LOGIN_KEY)
        expires_at = parse_datetime(request.session.get(PENDING_LOGIN_EXPIRES_KEY, ""))
        if not user_id or not expires_at or expires_at <= timezone.now():
            request.session.pop(PENDING_LOGIN_KEY, None)
            request.session.pop(PENDING_LOGIN_EXPIRES_KEY, None)
            return error_response("The two-factor login challenge expired.", "challenge_expired", 400)
        user = get_user_model().objects.filter(pk=user_id, is_active=True).first()
        if not user or is_exempt(user) or not get_profile(user).totp_enabled:
            return error_response("The two-factor login challenge is invalid.", "invalid_challenge", 400)
        ip_address = request.META.get("REMOTE_ADDR", "")
        if totp_attempts_exhausted(user.pk, ip_address):
            return error_response("Too many invalid codes. Try again later.", "throttled", 429)
        code = request.data.get("code")
        recovery_code = request.data.get("recovery_code")
        profile = get_profile(user)
        valid = verify_totp(profile, code) if code else consume_recovery_code(profile, recovery_code)
        if not valid:
            record_totp_failure(user.pk, ip_address)
            return error_response("The two-factor code is invalid.", "invalid_two_factor_code", 400)
        clear_totp_failures(user.pk, ip_address)
        request.session.pop(PENDING_LOGIN_KEY, None)
        request.session.pop(PENDING_LOGIN_EXPIRES_KEY, None)
        user.backend = "django.contrib.auth.backends.ModelBackend"
        login(request, user)
        return Response({"id": user.id, "username": user.username})


@extend_schema_view(post=extend_schema(request=OpenApiTypes.NONE, responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT}))
class TwoFactorSetupView(APIView):
    authentication_classes = [SubscriptionSessionAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        profile = get_profile(request.user)
        if profile.totp_enabled:
            return error_response("Two-factor authentication is already enabled.", "two_factor_enabled", 400)
        import pyotp

        secret = pyotp.random_base32()
        store_totp_secret(profile, secret)
        uri = pyotp.TOTP(secret).provisioning_uri(
            name=request.user.email or request.user.get_username(),
            issuer_name="Postup",
        )
        return Response({"otpauth_uri": uri, "secret": secret})


@extend_schema_view(
    post=extend_schema(
        request=OpenApiTypes.OBJECT,
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT},
    )
)
class TwoFactorEnableView(APIView):
    authentication_classes = [SubscriptionSessionAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        profile = get_profile(request.user)
        if profile.totp_enabled or not verify_totp(profile, request.data.get("code")):
            return error_response("The two-factor code is invalid.", "invalid_two_factor_code", 400)
        plaintext_codes, encoded_codes = generate_recovery_codes()
        profile.totp_enabled = True
        profile.recovery_codes = encoded_codes
        profile.save(update_fields=["totp_enabled", "recovery_codes"])
        _audit_account_action(request.user, request.user, "two_factor_enabled")
        return Response({"enabled": True, "recovery_codes": plaintext_codes})


@extend_schema_view(
    post=extend_schema(
        request=OpenApiTypes.OBJECT,
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT},
    )
)
class TwoFactorDisableView(APIView):
    authentication_classes = [SubscriptionSessionAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        profile = get_profile(request.user)
        if not request.user.check_password(request.data.get("password", "")):
            return error_response("Password is incorrect.", "invalid_password", 400)
        if not profile.totp_enabled or not verify_totp(profile, request.data.get("code")):
            return error_response("The two-factor code is invalid.", "invalid_two_factor_code", 400)
        profile.totp_enabled = False
        profile.totp_secret_encrypted = b""
        profile.recovery_codes = []
        profile.save(update_fields=["totp_enabled", "totp_secret_encrypted", "recovery_codes"])
        _audit_account_action(request.user, request.user, "two_factor_disabled")
        return Response({"enabled": False})


@extend_schema_view(
    post=extend_schema(
        request=OpenApiTypes.OBJECT,
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT},
    )
)
class TwoFactorRecoveryCodesView(APIView):
    authentication_classes = [SubscriptionSessionAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        profile = get_profile(request.user)
        if not request.user.check_password(request.data.get("password", "")):
            return error_response("Password is incorrect.", "invalid_password", 400)
        if not profile.totp_enabled or not verify_totp(profile, request.data.get("code")):
            return error_response("The two-factor code is invalid.", "invalid_two_factor_code", 400)
        plaintext_codes, profile.recovery_codes = generate_recovery_codes()
        profile.save(update_fields=["recovery_codes"])
        return Response({"recovery_codes": plaintext_codes})


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
        send_verification_email(user)
        login(request, authenticated_user)
        return Response(
            {"id": user.id, "username": user.get_username(), "email": user.email},
            status=status.HTTP_201_CREATED,
        )


@extend_schema_view(post=extend_schema(request=OpenApiTypes.NONE, responses={200: OpenApiTypes.OBJECT}))
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
        profile = get_profile(request.user)
        return Response(
            {
                "id": request.user.id,
                "username": request.user.username,
                "is_staff": request.user.is_staff,
                "is_superuser": request.user.is_superuser,
                "email_verified": profile.email_verified,
                "totp_enabled": profile.totp_enabled,
            }
        )


def _audit_account_action(actor, target, action, details=None):
    AdminAuditLog.objects.create(
        who=actor,
        who_identifier=f"{actor.pk}:{actor.get_username()}",
        target=target,
        target_identifier=f"{target.pk}:{target.get_username()}",
        action=action,
        details=details or {},
    )


@extend_schema_view(
    get=extend_schema(responses={200: OpenApiTypes.OBJECT}),
    patch=extend_schema(
        request=ProfileSerializer,
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT},
    ),
)
class ProfileView(APIView):
    authentication_classes = [SubscriptionSessionAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        data = ProfileSerializer(request.user).data
        data["email_verified"] = get_profile(request.user).email_verified
        return Response(data)

    def patch(self, request):
        user = request.user
        old_email = user.email
        serializer = ProfileSerializer(user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        changed_email = user.email != old_email
        if changed_email:
            profile = get_profile(user)
            profile.email_verified = False
            profile.email_verified_at = None
            profile.save(update_fields=["email_verified", "email_verified_at"])
            for key in ("fb_oauth_state", "fb_pending_pages", "fb_provider", "fb_reconnect_id"):
                request.session.pop(key, None)
            send_verification_email(user)
            _audit_account_action(request.user, user, "email_changed")
        data = ProfileSerializer(user).data
        data["email_verified"] = get_profile(user).email_verified
        return Response(data)


@extend_schema_view(
    post=extend_schema(
        request=ChangePasswordSerializer,
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT},
    )
)
class ChangePasswordView(APIView):
    authentication_classes = [SubscriptionSessionAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = ChangePasswordSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        user = request.user
        if not user.check_password(serializer.validated_data["current_password"]):
            return error_response("Current password is incorrect.", "invalid_password", 400)
        user.set_password(serializer.validated_data["new_password"])
        user.save(update_fields=["password"])
        _delete_user_sessions(user.pk, except_session_key=request.session.session_key)
        update_session_auth_hash(request, user)
        send_password_changed_email(user)
        _audit_account_action(user, user, "password_changed")
        return Response({"ok": True})


def _delete_user_sessions(user_id, except_session_key=None):
    from django.contrib.sessions.models import Session

    for session in Session.objects.exclude(session_key=except_session_key).iterator():
        try:
            session_user_id = session.get_decoded().get("_auth_user_id")
        except Exception:
            continue
        if str(session_user_id) == str(user_id):
            session.delete()


@extend_schema_view(
    post=extend_schema(
        request=OpenApiTypes.OBJECT,
        responses={204: None, 400: OpenApiTypes.OBJECT, 503: OpenApiTypes.OBJECT},
    )
)
class DeleteAccountView(APIView):
    authentication_classes = [SubscriptionSessionAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        if user.is_superuser:
            return error_response("Superusers cannot delete their own account.", "protected_user", 400)
        if not user.check_password(request.data.get("password", "")):
            return error_response("Password is incorrect.", "invalid_password", 400)
        if request.data.get("confirm") != user.get_username():
            return error_response("Provide your username to confirm deletion.", "confirmation_required", 400)
        subscription = getattr(user, "subscription", None)
        if (
            settings.BILLING_PROVIDER == "stripe"
            and subscription
            and subscription.stripe_subscription_id
        ):
            try:
                stripe_subscription = get_stripe().Subscription
                if hasattr(stripe_subscription, "cancel"):
                    stripe_subscription.cancel(subscription.stripe_subscription_id)
                else:
                    stripe_subscription.delete(subscription.stripe_subscription_id)
            except Exception:
                return error_response(
                    "The subscription could not be canceled. Try again later.",
                    "subscription_cancel_failed",
                    503,
                )
        with transaction.atomic():
            SocialAccount.objects.filter(user=user).update(
                status=SocialAccount.Status.DISCONNECTED,
                token_encrypted=b"",
            )
            _delete_user_sessions(user.pk)
            _audit_account_action(user, user, "account_deleted")
            user.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


def _sessions_for_user(user_id):
    sessions = []
    for session in Session.objects.filter(expire_date__gt=timezone.now()).iterator():
        try:
            data = session.get_decoded()
        except Exception:
            continue
        if str(data.get("_auth_user_id")) == str(user_id):
            sessions.append(session)
    return sessions


@extend_schema_view(get=extend_schema(responses={200: OpenApiTypes.OBJECT}))
class SessionsView(APIView):
    authentication_classes = [SubscriptionSessionAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        sessions = _sessions_for_user(request.user.pk)
        return Response({
            "results": [
                {
                    "key": session.session_key,
                    "expire_date": session.expire_date,
                    "current": session.session_key == request.session.session_key,
                }
                for session in sessions
            ]
        })


@extend_schema_view(
    delete=extend_schema(
        responses={204: None, 404: OpenApiTypes.OBJECT},
    )
)
class RevokeSessionView(APIView):
    authentication_classes = [SubscriptionSessionAuthentication]
    permission_classes = [IsAuthenticated]

    def delete(self, request, key):
        session = next(
            (item for item in _sessions_for_user(request.user.pk) if item.session_key == key),
            None,
        )
        if not session:
            raise Http404
        session.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema_view(
    post=extend_schema(request=OpenApiTypes.NONE, responses={200: OpenApiTypes.OBJECT}),
)
class RevokeOtherSessionsView(APIView):
    authentication_classes = [SubscriptionSessionAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        current_key = request.session.session_key
        sessions = [
            session for session in _sessions_for_user(request.user.pk)
            if session.session_key != current_key
        ]
        Session.objects.filter(session_key__in=[session.session_key for session in sessions]).delete()
        return Response({"revoked": len(sessions)})


@extend_schema_view(
    post=extend_schema(
        request=OpenApiTypes.OBJECT,
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT},
    )
)
class VerifyEmailView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        token = request.data.get("token")
        if not isinstance(token, str) or not token:
            return error_response("A verification token is required.", "invalid_verification_token", 400)
        try:
            payload = read_verification_token(token)
            user = get_user_model().objects.get(pk=payload.get("user_id"))
        except (signing.BadSignature, signing.SignatureExpired, get_user_model().DoesNotExist, TypeError):
            return error_response("The verification token is invalid or expired.", "invalid_verification_token", 400)
        if payload.get("email") != user.email:
            return error_response("The verification token is no longer valid.", "invalid_verification_token", 400)
        profile = get_profile(user)
        profile.email_verified = True
        profile.email_verified_at = timezone.now()
        profile.save(update_fields=["email_verified", "email_verified_at"])
        return Response({"email_verified": True})


@extend_schema_view(
    post=extend_schema(
        request=OpenApiTypes.NONE,
        responses={200: OpenApiTypes.OBJECT, 429: OpenApiTypes.OBJECT, 503: OpenApiTypes.OBJECT},
    )
)
class ResendVerificationView(APIView):
    authentication_classes = [SubscriptionSessionAuthentication]
    permission_classes = [IsAuthenticated]
    throttle_classes = [SuperuserExemptScopedRateThrottle]
    throttle_scope = "email_verification"

    def post(self, request):
        profile = get_profile(request.user)

        if profile.email_verified:
            return Response({
                "ok": True,
                "email_sent": False,
                "already_verified": True,
            })

        sent = send_verification_email(request.user)

        if not sent:
            return Response(
                {
                    "ok": False,
                    "email_sent": False,
                    "detail": "Could not send verification email. Please try again.",
                },
                status=503,
            )

        return Response({
            "ok": True,
            "email_sent": True,
        })


@extend_schema_view(
    post=extend_schema(
        request=OpenApiTypes.OBJECT,
        responses={200: OpenApiTypes.OBJECT, 429: OpenApiTypes.OBJECT},
    )
)
class PasswordResetView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "password_reset"

    def post(self, request):
        email = request.data.get("email")
        if isinstance(email, str):
            user = get_user_model().objects.filter(email__iexact=email.strip()).first()
            if user and user.is_active:
                uid = urlsafe_base64_encode(str(user.pk).encode())
                token = default_token_generator.make_token(user)
                send_password_reset_email(user, uid, token)
        return Response({"ok": True})


@extend_schema_view(
    post=extend_schema(
        request=OpenApiTypes.OBJECT,
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT, 429: OpenApiTypes.OBJECT},
    )
)
class PasswordResetConfirmView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "password_reset"

    def post(self, request):
        uid = request.data.get("uid")
        token = request.data.get("token")
        new_password = request.data.get("new_password")
        try:
            user_id = urlsafe_base64_decode(uid).decode()
            user = get_user_model().objects.get(pk=user_id)
        except (TypeError, ValueError, UnicodeDecodeError, get_user_model().DoesNotExist):
            return error_response("The password reset link is invalid or expired.", "invalid_reset_token", 400)
        if not isinstance(token, str) or not default_token_generator.check_token(user, token):
            return error_response("The password reset link is invalid or expired.", "invalid_reset_token", 400)
        serializer = ChangePasswordSerializer(
            data={"current_password": "unused", "new_password": new_password},
            context={"request": type("RequestContext", (), {"user": user})()},
        )
        try:
            serializer.fields["new_password"].run_validation(new_password)
            validate_password(new_password, user=user)
        except (DjangoValidationError, serializers.ValidationError) as exc:
            messages = exc.messages if isinstance(exc, DjangoValidationError) else exc.detail
            raise serializers.ValidationError({"new_password": messages}) from exc
        user.set_password(new_password)
        user.save(update_fields=["password"])
        _delete_user_sessions(user.pk)
        send_password_changed_email(user)
        return Response({"ok": True})


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
        requested_provider = request.GET.get("provider", SocialAccount.Provider.FACEBOOK)
        if requested_provider == SocialAccount.Provider.INSTAGRAM:
            if not settings.INSTAGRAM_ENABLED:
                return error_response("Instagram is not enabled.", "provider_disabled", 404)
            provider = SocialAccount.Provider.INSTAGRAM
            scopes = InstagramProvider().auth_scopes()
        elif requested_provider == SocialAccount.Provider.FACEBOOK:
            provider = SocialAccount.Provider.FACEBOOK
            scopes = None
        else:
            return error_response("Unsupported provider.", "invalid_provider", 400)

        ensure_subscription_active(request.user)
        reconnect_id = request.GET.get("reconnect")
        ensure_email_verified(request.user)
        if reconnect_id:
            get_object_or_404(
                SocialAccount,
                id=reconnect_id,
                user=request.user,
                provider=provider,
            )
            request.session["fb_reconnect_id"] = reconnect_id
        else:
            ensure_within_plan_limit(request.user, "connected_accounts")
            request.session.pop("fb_reconnect_id", None)
        request.session["fb_provider"] = provider

        state = secrets.token_urlsafe(32)
        request.session["fb_oauth_state"] = state
        rerequest = bool(request.session.get("fb_rerequest"))
        return HttpResponseRedirect(
            FacebookProvider().auth_url(state, rerequest=rerequest, scopes=scopes)
        )


class ThreadsConnectView(APIView):
    authentication_classes = [SubscriptionSessionAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        ensure_subscription_active(request.user)
        ensure_email_verified(request.user)
        ensure_within_plan_limit(request.user, "connected_accounts")

        state = secrets.token_urlsafe(32)
        cache.set(
            _threads_oauth_cache_key(state),
            request.user.pk,
            timeout=600,
        )
        params = urlencode(
            {
                "client_id": settings.THREADS_APP_ID,
                "redirect_uri": settings.THREADS_REDIRECT_URI,
                "scope": (
                    "threads_basic,threads_content_publish,"
                    "threads_manage_insights"
                ),
                "response_type": "code",
                "state": state,
            }
        )
        return HttpResponseRedirect(f"https://threads.net/oauth/authorize?{params}")


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
        ensure_email_verified(request.user)
        ensure_subscription_active(request.user)
        pages = request.session.get("fb_pending_pages")
        if not pages:
            return error_response("No pending Facebook Pages.", "no_pending_pages", 400)
        if (
            not settings.INSTAGRAM_ENABLED
            and any(page.get("provider") == SocialAccount.Provider.INSTAGRAM for page in pages)
        ):
            return error_response("Instagram is not enabled.", "provider_disabled", 404)

        selected = request.data.get("page_ids")

        if not isinstance(selected, list):
            return error_response("page_ids must be a list.", "validation_error", 400)
        if not selected:
            return error_response("page_ids must not be empty.", "validation_error", 400)

        selected_pages = [
            page for page in pages if str(page["id"]) in {str(item) for item in selected}
        ]

        existing_accounts = set(
            SocialAccount.objects.filter(user=request.user).values_list(
                "provider", "external_id"
            )
        )
        additional_accounts = sum(
            (
                page.get("provider", SocialAccount.Provider.FACEBOOK),
                page["id"],
            ) not in existing_accounts
            for page in selected_pages
        )
        if additional_accounts:
            ensure_within_plan_limit(
                request.user, "connected_accounts", additional=additional_accounts
            )

        saved = []
        for page in selected_pages:
            provider = page.get("provider", SocialAccount.Provider.FACEBOOK)
            account, _ = SocialAccount.objects.update_or_create(
                user=request.user,
                provider=provider,
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


def _pages_from_granular_scopes(long_token):
    """Fallback when /me/accounts is empty: fetch Pages the token was granted."""
    debug = requests.get(
        f"https://graph.facebook.com/{settings.FB_GRAPH_VERSION}/debug_token",
        params={
            "input_token": long_token,
            "access_token": f"{settings.FB_APP_ID}|{settings.FB_APP_SECRET}",
        },
        timeout=20,
    )
    if not debug.ok:
        return []
    page_ids = []
    for item in debug.json().get("data", {}).get("granular_scopes") or []:
        if item.get("scope") == "pages_show_list":
            page_ids = [str(i) for i in item.get("target_ids") or []]
    pages = []
    for page_id in page_ids:
        response = requests.get(
            f"https://graph.facebook.com/{settings.FB_GRAPH_VERSION}/{page_id}",
            params={"fields": "id,name,access_token", "access_token": long_token},
            timeout=20,
        )
        if not response.ok:
            continue
        page = response.json()
        if isinstance(page, dict) and page.get("access_token"):
            pages.append(page)
    return pages


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
        provider = request.session.get("fb_provider", SocialAccount.Provider.FACEBOOK)
        is_instagram = provider == SocialAccount.Provider.INSTAGRAM
        if is_instagram and not settings.INSTAGRAM_ENABLED:
            return self._frontend_error_redirect("provider_disabled")

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
                f"{settings.FB_GRAPH_BASE_URL}/{settings.FB_GRAPH_VERSION}/oauth/access_token",
                params={
                    "client_id": settings.FB_APP_ID,
                    "client_secret": settings.FB_APP_SECRET,
                    "redirect_uri": settings.FB_REDIRECT_URI,
                    "code": code,
                },
                timeout=20,
            )
            if not short_token_response.ok:
                return self._frontend_error_redirect(
                    "instagram_account_not_available" if is_instagram else "facebook_unavailable"
                )

            short_token = short_token_response.json().get("access_token")
            if not short_token:
                return self._frontend_error_redirect("facebook_unavailable")

            long_token_response = requests.get(
                f"{settings.FB_GRAPH_BASE_URL}/{settings.FB_GRAPH_VERSION}/oauth/access_token",
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
                f"{settings.FB_GRAPH_BASE_URL}/{settings.FB_GRAPH_VERSION}/me/permissions",
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
        required_permissions = (
            {"instagram_content_publish", "pages_show_list"}
            if is_instagram
            else {"pages_manage_posts", "pages_show_list"}
        )
        if not required_permissions.issubset(permissions):
            request.session["fb_rerequest"] = True
            return self._frontend_error_redirect("permissions_declined")
        request.session.pop("fb_rerequest", None)
        granted_scopes = ", ".join(granted_permissions)

        try:
            pages_response = requests.get(
                f"{settings.FB_GRAPH_BASE_URL}/{settings.FB_GRAPH_VERSION}/me/accounts",
                params={
                    "fields": "id,name,access_token,instagram_business_account{id,username}",
                    "access_token": long_token,
                },
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
        if is_instagram:
            pages = [
                page for page in pages
                if isinstance(page.get("instagram_business_account"), dict)
            ]
            if not pages:
                return self._frontend_error_redirect("no_instagram_accounts")
        if not pages:
            try:
                pages = _pages_from_granular_scopes(long_token)
            except (requests.RequestException, ValueError, AttributeError, TypeError):
                pages = []
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
                provider=provider,
            )

        if reconnect_account and not any(
            (
                str(page.get("instagram_business_account", {}).get("id"))
                if is_instagram else str(page.get("id"))
            ) == reconnect_account.external_id
            for page in pages
        ):
            return self._frontend_error_redirect(
                "instagram_account_not_available" if is_instagram else "page_not_available"
            )

        pending_pages = []
        for page in pages:
            instagram_account = page.get("instagram_business_account", {})
            page_id = instagram_account.get("id") if is_instagram else page.get("id")
            page_name = instagram_account.get("username") if is_instagram else page.get("name")
            page_token = page.get("access_token")
            linked_page_id = page.get("id")
            if not page_id or not page_name or not page_token:
                continue
            pending_item = {
                "id": str(page_id),
                "name": page_name,
                "token_encrypted": base64.b64encode(encrypt_token(page_token)).decode(),
                "scopes": granted_scopes,
            }
            if is_instagram:
                pending_item["provider"] = SocialAccount.Provider.INSTAGRAM
                pending_item["linked_page_id"] = str(linked_page_id)
            pending_pages.append(pending_item)

        if not pending_pages:
            return self._frontend_error_redirect(
                "no_instagram_accounts" if is_instagram else "facebook_unavailable"
            )

        if reconnect_account:
            matching_page = next(
                (page for page in pending_pages if page["id"] == reconnect_account.external_id),
                None,
            )
            if not matching_page:
                return self._frontend_error_redirect("facebook_unavailable")

            SocialAccount.objects.update_or_create(
                user=request.user,
                provider=provider,
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
            request.session.pop("fb_provider", None)
            return HttpResponseRedirect(
                f"{settings.FRONTEND_URL.rstrip('/')}/accounts?fb_connected=1"
            )

        request.session["fb_pending_pages"] = pending_pages
        return HttpResponseRedirect(f"{settings.FRONTEND_URL.rstrip('/')}/accounts/select")


class ThreadsCallbackView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def _frontend_error_redirect(self, error):
        return HttpResponseRedirect(
            f"{settings.FRONTEND_URL.rstrip('/')}/accounts?threads_error={quote(error, safe='')}"
        )

    def get(self, request):
        state = request.GET.get("state", "")

        if not state:
            return self._frontend_error_redirect("invalid_state")

        cache_key = _threads_oauth_cache_key(state)
        user_id = cache.get(cache_key)

        if user_id is None:
            return self._frontend_error_redirect("invalid_state")

        # Consume the state so it cannot be reused.
        cache.delete(cache_key)

        user = get_user_model().objects.filter(
            pk=user_id,
            is_active=True,
        ).first()

        if not user:
            return self._frontend_error_redirect("invalid_state")

        if request.GET.get("error") or request.GET.get("error_reason"):
            return self._frontend_error_redirect("threads_denied")

        code = request.GET.get("code", "")

        if code.endswith("#_"):
            code = code[:-2]

        if not code:
            return self._frontend_error_redirect("missing_code")

        try:
            short_token_response = requests.post(
                "https://graph.threads.net/oauth/access_token",
                data={
                    "client_id": settings.THREADS_APP_ID,
                    "client_secret": settings.THREADS_APP_SECRET,
                    "grant_type": "authorization_code",
                    "redirect_uri": settings.THREADS_REDIRECT_URI,
                    "code": code,
                },
                timeout=20,
            )
            if not short_token_response.ok:
                return self._frontend_error_redirect("threads_unavailable")
            short_token_data = short_token_response.json()
            short_token = short_token_data.get("access_token")
            if not isinstance(short_token, str) or not short_token:
                return self._frontend_error_redirect("threads_unavailable")

            long_token_response = requests.get(
                "https://graph.threads.net/access_token",
                params={
                    "grant_type": "th_exchange_token",
                    "client_secret": settings.THREADS_APP_SECRET,
                    "access_token": short_token,
                },
                timeout=20,
            )
            if not long_token_response.ok:
                return self._frontend_error_redirect("threads_unavailable")
            long_token_data = long_token_response.json()
            long_token = long_token_data.get("access_token")
            expires_in = int(long_token_data.get("expires_in", 0))
            if not isinstance(long_token, str) or not long_token or expires_in <= 0:
                return self._frontend_error_redirect("threads_unavailable")

            profile_response = requests.get(
                "https://graph.threads.net/v1.0/me",
                params={
                    "fields": "id,username,threads_profile_picture_url",
                    "access_token": long_token,
                },
                timeout=20,
            )
            if not profile_response.ok:
                return self._frontend_error_redirect("threads_unavailable")
            profile = profile_response.json()
            external_id = profile.get("id")
            username = profile.get("username")
            if (
                not isinstance(external_id, str)
                or not external_id
                or not isinstance(username, str)
                or not username
            ):
                return self._frontend_error_redirect("threads_unavailable")
            token_expires_at = timezone.now() + timedelta(seconds=expires_in)
        except (
            requests.RequestException,
            ValueError,
            AttributeError,
            TypeError,
            OverflowError,
        ):
            return self._frontend_error_redirect("threads_unavailable")

        SocialAccount.objects.update_or_create(
            user=user,
            provider=SocialAccount.Provider.THREADS,
            external_id=str(external_id),
            defaults={
                "name": username,
                "token_encrypted": encrypt_token(long_token),
                "token_expires_at": token_expires_at,
                "status": SocialAccount.Status.ACTIVE,
                "scopes": (
                    "threads_basic, threads_content_publish, "
                    "threads_manage_insights"
                ),
            },
        )
        return HttpResponseRedirect(
            f"{settings.FRONTEND_URL.rstrip('/')}/accounts?threads_connected=1"
        )
