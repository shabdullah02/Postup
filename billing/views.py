from django.conf import settings
from django.db import transaction
from datetime import timedelta
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import AllowAny, IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from drf_spectacular.utils import OpenApiTypes, extend_schema_view, extend_schema

from billing.entitlements import get_usage
from billing.models import Subscription
from billing.plans import PLAN_LIMITS
from billing.stripe_utils import (
    StripeConfigurationError,
    get_stripe,
    process_stripe_event,
)


def billing_me_data(user):
    subscription = user.subscription
    limits = PLAN_LIMITS[subscription.plan]
    return {
        "plan": subscription.plan,
        "status": subscription.status,
        "usage": {
            metric: {"used": get_usage(user, metric), "limit": limit}
            for metric, limit in limits.items()
        },
        "current_period_end": subscription.current_period_end,
    }


@extend_schema_view(get=extend_schema(responses={200: OpenApiTypes.OBJECT}))
class BillingMeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(billing_me_data(request.user))


@extend_schema_view(
    post=extend_schema(
        request=OpenApiTypes.OBJECT,
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT, 404: OpenApiTypes.OBJECT},
    )
)
class TestSubscribeView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not settings.DEBUG:
            raise NotFound()

        plan = request.data.get("plan")
        if plan not in Subscription.Plan.values:
            raise ValidationError({"plan": ["Choose free, pro, or agency."]})

        subscription = request.user.subscription
        subscription.plan = plan
        subscription.status = Subscription.Status.ACTIVE
        subscription.current_period_end = timezone.now() + timedelta(days=30)
        subscription.save(update_fields=["plan", "status", "current_period_end", "updated_at"])
        return Response(billing_me_data(request.user))


@extend_schema_view(post=extend_schema(responses={200: OpenApiTypes.OBJECT}))
class CancelSubscriptionView(APIView):
    permission_classes = [IsAdminUser]

    def post(self, request):
        subscription = request.user.subscription
        subscription.status = Subscription.Status.CANCELED
        subscription.save(update_fields=["status", "updated_at"])
        return Response(billing_me_data(request.user))


@extend_schema_view(
    post=extend_schema(
        request=OpenApiTypes.OBJECT,
        responses={201: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT, 503: OpenApiTypes.OBJECT},
    )
)
class CheckoutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        plan = request.data.get("plan")
        if plan not in {Subscription.Plan.PRO, Subscription.Plan.AGENCY}:
            raise ValidationError({"plan": ["Choose pro or agency."]})
        price_id = (
            settings.STRIPE_PRO_PRICE_ID
            if plan == Subscription.Plan.PRO
            else settings.STRIPE_AGENCY_PRICE_ID
        )
        if not price_id:
            raise StripeConfigurationError("The selected plan has no Stripe price configured.")

        stripe = get_stripe()
        subscription = request.user.subscription
        customer_id = subscription.stripe_customer_id
        if not customer_id:
            customer = stripe.Customer.create(
                email=request.user.email,
                name=request.user.get_username(),
                metadata={"user_id": str(request.user.pk)},
            )
            customer_id = customer.id
            subscription.stripe_customer_id = customer_id
            subscription.save(update_fields=["stripe_customer_id", "updated_at"])

        frontend = settings.FRONTEND_URL.rstrip("/")
        session = stripe.checkout.Session.create(
            mode="subscription",
            customer=customer_id,
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=f"{frontend}/accounts?billing=success",
            cancel_url=f"{frontend}/accounts?billing=canceled",
            metadata={"user_id": str(request.user.pk), "plan": plan},
            subscription_data={"metadata": {"user_id": str(request.user.pk), "plan": plan}},
        )
        return Response({"url": session.url}, status=status.HTTP_201_CREATED)


@extend_schema_view(
    post=extend_schema(
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT, 503: OpenApiTypes.OBJECT}
    )
)
class CustomerPortalView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        subscription = request.user.subscription
        if not subscription.stripe_customer_id:
            raise ValidationError({"detail": "No Stripe customer is linked to this account."})
        stripe = get_stripe()
        session = stripe.billing_portal.Session.create(
            customer=subscription.stripe_customer_id,
            return_url=f"{settings.FRONTEND_URL.rstrip('/')}/accounts",
        )
        return Response({"url": session.url})


@extend_schema_view(
    post=extend_schema(
        request=OpenApiTypes.BINARY,
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT, 503: OpenApiTypes.OBJECT},
    )
)
class StripeWebhookView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        if not settings.STRIPE_WEBHOOK_SECRET:
            raise StripeConfigurationError("The Stripe webhook secret is not configured.")
        stripe = get_stripe()
        signature = request.headers.get("Stripe-Signature", "")
        try:
            event = stripe.Webhook.construct_event(
                request.body,
                signature,
                settings.STRIPE_WEBHOOK_SECRET,
            )
        except Exception as exc:
            if isinstance(exc, ValueError) or exc.__class__.__name__ == "SignatureVerificationError":
                return Response(
                    {"detail": "Invalid Stripe webhook signature.", "code": "invalid_signature"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            raise

        with transaction.atomic():
            processed = process_stripe_event(event)
        return Response({"received": True, "duplicate": not processed})