from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone
from drf_spectacular.utils import OpenApiTypes, extend_schema, extend_schema_view
from rest_framework import generics, serializers, status
from rest_framework.exceptions import NotFound
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import SocialAccount
from billing.models import AdminAuditLog, Subscription
from billing.stripe_utils import get_stripe
from posts.models import Post


User = get_user_model()


class AdminSubscriptionSerializer(serializers.Serializer):
    plan = serializers.CharField()
    status = serializers.CharField()
    current_period_end = serializers.DateTimeField(allow_null=True)


class AdminUserSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    username = serializers.CharField()
    email = serializers.EmailField(allow_blank=True)
    plan = serializers.CharField()
    status = serializers.CharField()
    is_active = serializers.BooleanField()
    is_staff = serializers.BooleanField()
    date_joined = serializers.DateTimeField()
    subscription = AdminSubscriptionSerializer(allow_null=True)


class AdminUserPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"


class ProtectedUserValidationError(serializers.ValidationError):
    pass


class ProtectedUserAPIView(APIView):
    def handle_exception(self, exc):
        if isinstance(exc, ProtectedUserValidationError):
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        return super().handle_exception(exc)


def _user_queryset():
    return User.objects.select_related("subscription")


def _get_user(user_id):
    try:
        return _user_queryset().get(pk=user_id)
    except User.DoesNotExist as exc:
        raise NotFound("User not found.") from exc


def _protected_user_error(target):
    if target.is_superuser:
        raise ProtectedUserValidationError({
            "detail": "Superuser accounts cannot be suspended, canceled, deactivated or deleted from the admin panel.",
            "code": "protected_user",
        })


def _self_admin_error(target, request, action):
    if target.pk == request.user.pk:
        raise ProtectedUserValidationError({
            "detail": f"You cannot {action} your own account from the admin panel.",
            "code": "protected_user",
        })


def _serialize_user(user):
    subscription = getattr(user, "subscription", None)
    return {
        "id": user.pk,
        "username": user.get_username(),
        "email": user.email,
        "plan": subscription.plan if subscription else "free",
        "status": subscription.status if subscription else "trialing",
        "is_active": user.is_active,
        "is_staff": user.is_staff,
        "date_joined": user.date_joined,
        "subscription": {
            "plan": subscription.plan,
            "status": subscription.status,
            "current_period_end": subscription.current_period_end,
        } if subscription else None,
    }


def _audit(actor, target, action, details=None):
    AdminAuditLog.objects.create(
        who=actor,
        who_identifier=f"{actor.pk}:{actor.get_username()}",
        target=target,
        target_identifier=f"{target.pk}:{target.get_username()}",
        action=action,
        details=details or {},
    )


class AdminUsersView(generics.ListAPIView):
    permission_classes = [IsAdminUser]
    serializer_class = AdminUserSerializer
    pagination_class = AdminUserPagination

    def get_queryset(self):
        queryset = _user_queryset()
        search = self.request.query_params.get("search", "").strip()
        if search:
            queryset = queryset.filter(
                Q(username__icontains=search) | Q(email__icontains=search)
            )
        plan = self.request.query_params.get("plan")
        if plan:
            queryset = queryset.filter(subscription__plan=plan)
        subscription_status = self.request.query_params.get("status")
        if subscription_status:
            queryset = queryset.filter(subscription__status=subscription_status)
        return queryset.order_by("pk")

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        return self.get_paginated_response([_serialize_user(user) for user in page])


class AdminUserDetailView(ProtectedUserAPIView):
    permission_classes = [IsAdminUser]

    @extend_schema(operation_id="admin_user_detail", responses={200: AdminUserSerializer})
    def get(self, request, user_id):
        return Response(_serialize_user(_get_user(user_id)))

    @extend_schema(responses={204: None, 400: OpenApiTypes.OBJECT})
    def delete(self, request, user_id):
        user = _get_user(user_id)
        _protected_user_error(user)
        _self_admin_error(user, request, "delete")
        if request.data.get("confirm") != user.get_username():
            raise serializers.ValidationError({
                "confirm": ["Provide the target username to confirm deletion."]
            })
        _audit(request.user, user, "user_hard_deleted", {"username": user.get_username()})
        _delete_user_sessions(user.pk)
        user.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema_view(
    patch=extend_schema(
        request=OpenApiTypes.OBJECT,
        responses={200: AdminUserSerializer, 400: OpenApiTypes.OBJECT, 404: OpenApiTypes.OBJECT},
    )
)
class AdminSubscriptionUpdateView(ProtectedUserAPIView):
    permission_classes = [IsAdminUser]

    def patch(self, request, user_id):
        user = _get_user(user_id)
        subscription = user.subscription
        plan = request.data.get("plan", subscription.plan)
        subscription_status = request.data.get("status", subscription.status)
        if (
            subscription_status in {Subscription.Status.SUSPENDED, Subscription.Status.CANCELED}
            or ("plan" in request.data and plan != subscription.plan)
        ):
            _protected_user_error(user)
        if plan not in Subscription.Plan.values:
            raise serializers.ValidationError({"plan": ["Invalid plan."]})
        if subscription_status not in Subscription.Status.values:
            raise serializers.ValidationError({"status": ["Invalid status."]})

        extend_days = request.data.get("extend_days", 0)
        if isinstance(extend_days, bool) or not isinstance(extend_days, int) or extend_days < 0 or extend_days > 3650:
            raise serializers.ValidationError({"extend_days": ["Must be an integer from 0 to 3650."]})

        before = {
            "plan": subscription.plan,
            "status": subscription.status,
            "current_period_end": subscription.current_period_end.isoformat()
            if subscription.current_period_end else None,
        }
        subscription.plan = plan
        subscription.status = subscription_status
        if extend_days:
            base = subscription.current_period_end or timezone.now()
            if base < timezone.now():
                base = timezone.now()
            subscription.current_period_end = base + timedelta(days=extend_days)
        if subscription_status == Subscription.Status.CANCELED:
            subscription.plan = Subscription.Plan.FREE
            subscription.current_period_end = None
            subscription.stripe_subscription_id = ""
        subscription.save()
        _audit(request.user, user, "subscription_updated", {
            "before": before,
            "after": {
                "plan": subscription.plan,
                "status": subscription.status,
                "current_period_end": subscription.current_period_end.isoformat()
                if subscription.current_period_end else None,
            },
            "extend_days": extend_days,
        })
        return Response(_serialize_user(user))


def _delete_user_sessions(user_id):
    for session in Session.objects.all().iterator():
        if session.get_decoded().get("_auth_user_id") == str(user_id):
            session.delete()


def _cancel_stripe_subscription(subscription_id):
    if not subscription_id:
        return None
    stripe = get_stripe()
    stripe_subscription = stripe.Subscription
    if hasattr(stripe_subscription, "cancel"):
        stripe_subscription.cancel(subscription_id)
    else:
        stripe_subscription.delete(subscription_id)
    return True


@extend_schema_view(
    post=extend_schema(responses={200: OpenApiTypes.OBJECT, 404: OpenApiTypes.OBJECT})
)
class AdminDeactivateUserView(ProtectedUserAPIView):
    permission_classes = [IsAdminUser]

    def post(self, request, user_id):
        user = _get_user(user_id)
        _protected_user_error(user)
        _self_admin_error(user, request, "deactivate")
        subscription = user.subscription
        stripe_subscription_id = subscription.stripe_subscription_id
        with transaction.atomic():
            user.is_active = False
            user.save(update_fields=["is_active"])
            subscription.status = Subscription.Status.SUSPENDED
            subscription.save(update_fields=["status", "updated_at"])
            Post.objects.filter(user=user, status=Post.Status.SCHEDULED).update(
                status=Post.Status.DRAFT,
                scheduled_at=None,
                error="Cancelled because the user account was deactivated.",
            )
            SocialAccount.objects.filter(user=user).update(
                status=SocialAccount.Status.DISCONNECTED,
                token_encrypted=b"",
            )
            _delete_user_sessions(user.pk)

        stripe_cancelled = None
        if stripe_subscription_id:
            try:
                _cancel_stripe_subscription(stripe_subscription_id)
                stripe_cancelled = True
            except Exception:
                stripe_cancelled = False
        _audit(request.user, user, "user_deactivated", {
            "stripe_cancelled": stripe_cancelled,
        })
        return Response({"is_active": False, "stripe_cancelled": stripe_cancelled})


@extend_schema_view(
    post=extend_schema(responses={200: OpenApiTypes.OBJECT, 404: OpenApiTypes.OBJECT})
)
class AdminReactivateUserView(APIView):
    permission_classes = [IsAdminUser]

    def post(self, request, user_id):
        user = _get_user(user_id)
        user.is_active = True
        user.save(update_fields=["is_active"])
        subscription = user.subscription
        if subscription.status == Subscription.Status.SUSPENDED:
            subscription.status = Subscription.Status.ACTIVE
            if subscription.current_period_end and subscription.current_period_end <= timezone.now():
                subscription.current_period_end = timezone.now() + timedelta(days=30)
            subscription.save(update_fields=["status", "current_period_end", "updated_at"])
        _audit(request.user, user, "user_reactivated", {
            "subscription_status": subscription.status,
        })
        return Response({"is_active": True, "subscription_status": subscription.status})


@extend_schema_view(get=extend_schema(responses={200: OpenApiTypes.OBJECT}))
class AdminStatsView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request):
        active_subscriptions = Subscription.objects.filter(
            status__in=[Subscription.Status.ACTIVE, Subscription.Status.TRIALING]
        )
        failed_since = timezone.now() - timedelta(days=7)
        return Response({
            "users": User.objects.count(),
            "active_subscriptions_by_plan": {
                row["plan"]: row["count"]
                for row in active_subscriptions.values("plan").annotate(count=Count("id"))
            },
            "posts_by_status": {
                row["status"]: row["count"]
                for row in Post.objects.values("status").annotate(count=Count("id"))
            },
            "failed_posts_last_7_days": Post.objects.filter(
                status=Post.Status.FAILED,
            ).filter(
                Q(claimed_at__gte=failed_since)
                | Q(claimed_at__isnull=True, created_at__gte=failed_since)
            ).count(),
        })