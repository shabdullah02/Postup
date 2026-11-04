from django.conf import settings
from django.db import transaction
from datetime import timedelta
import math
from django.utils.decorators import method_decorator
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.debug import sensitive_post_parameters
from rest_framework import status
from rest_framework.exceptions import APIException, NotFound, PermissionDenied, ValidationError
from rest_framework.permissions import AllowAny, IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from drf_spectacular.utils import OpenApiTypes, extend_schema_view, extend_schema

from accounts.billing.gateway import process_payment
from accounts.models import get_profile
from billing.entitlements import get_subscription, get_usage
from billing.models import AdminAuditLog, Subscription
from billing.plans import PLAN_LIMITS
from config.api import error_response
from billing.stripe_utils import (
    StripeConfigurationError,
    get_stripe,
    process_stripe_event,
)


class StripeRequestError(APIException):
    status_code = status.HTTP_502_BAD_GATEWAY
    default_detail = "Stripe could not complete the request."
    default_code = "stripe_error"


class StripeOnlyAPIView(APIView):
    def initial(self, request, *args, **kwargs):
        if settings.BILLING_PROVIDER != "stripe":
            raise NotFound()
        return super().initial(request, *args, **kwargs)


class MockOnlyAPIView(APIView):
    def initial(self, request, *args, **kwargs):
        if settings.BILLING_PROVIDER != "mock":
            raise NotFound()
        return super().initial(request, *args, **kwargs)


def _stripe_call(operation, *args, **kwargs):
    import stripe

    try:
        return operation(*args, **kwargs)
    except stripe.StripeError as exc:
        raise StripeRequestError() from exc


def _stripe_value(resource, key, default=None):
    if isinstance(resource, dict):
        return resource.get(key, default)
    getter = getattr(resource, "get", None)
    if getter:
        return getter(key, default)
    return getattr(resource, key, default)


def _stripe_id(resource):
    if isinstance(resource, str):
        return resource
    return _stripe_value(resource, "id")


def _require_verified_email(user):
    if not get_profile(user).email_verified:
        raise PermissionDenied(
            "Verify your email address before subscribing.",
            code="email_not_verified",
        )


def _get_or_create_stripe_customer(user, stripe):
    subscription = get_subscription(user)
    with transaction.atomic():
        subscription = Subscription.objects.select_for_update().get(pk=subscription.pk)
        if not subscription.stripe_customer_id:
            customer = _stripe_call(
                stripe.Customer.create,
                email=user.email,
                name=user.get_username(),
                metadata={"user_id": str(user.pk)},
                idempotency_key=f"postup-customer-{user.pk}",
            )
            subscription.stripe_customer_id = customer.id
            subscription.save(update_fields=["stripe_customer_id", "updated_at"])
    return subscription


def _stripe_pro_price_id():
    return settings.STRIPE_PRICE_ID_PRO or settings.STRIPE_PRO_PRICE_ID


def billing_me_data(user):
    subscription = get_subscription(user)
    limits = PLAN_LIMITS[subscription.plan]
    return {
        "plan": subscription.plan,
        "status": subscription.status,
        "usage": {
            metric: {"used": get_usage(user, metric), "limit": limit}
            for metric, limit in limits.items()
        },
        **_subscription_lifecycle_data(subscription),
    }


def _audit_subscription_change(actor, action, details=None):
    AdminAuditLog.objects.create(
        who=actor,
        who_identifier=f"{actor.pk}:{actor.get_username()}",
        target=actor,
        target_identifier=f"{actor.pk}:{actor.get_username()}",
        action=action,
        details=details or {},
    )


def _days_remaining(subscription):
    if (
        subscription.plan == Subscription.Plan.FREE
        or subscription.status == Subscription.Status.CANCELED
    ):
        return None
    end = (
        subscription.trial_ends_at
        if subscription.status == Subscription.Status.TRIALING
        else subscription.current_period_end
    )
    if not end:
        return None
    return max(0, math.ceil((end - timezone.now()).total_seconds() / 86400))


def _subscription_lifecycle_data(subscription):
    has_paid_plan = (
        subscription.plan != Subscription.Plan.FREE
        and subscription.status != Subscription.Status.CANCELED
    )
    return {
        "started_at": subscription.started_at,
        "current_period_end": subscription.current_period_end if has_paid_plan else None,
        "next_billing_date": subscription.next_billing_date if has_paid_plan else None,
        "cancel_at_period_end": (
            subscription.cancel_at_period_end
            if has_paid_plan and subscription.current_period_end
            else False
        ),
        "trial_ends_at": subscription.trial_ends_at,
        "days_remaining": _days_remaining(subscription),
    }


@extend_schema_view(get=extend_schema(responses={200: OpenApiTypes.OBJECT}))
class SubscriptionDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        subscription = get_subscription(request.user)
        return Response({
            "plan": subscription.plan,
            "status": subscription.status,
            **_subscription_lifecycle_data(subscription),
        })


@extend_schema_view(
    post=extend_schema(
        request=OpenApiTypes.NONE,
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT, 503: OpenApiTypes.OBJECT}
    )
)
class CancelAtPeriodEndView(APIView):
    permission_classes = [IsAdminUser]

    def post(self, request):
        subscription = request.user.subscription
        if (
            subscription.plan == Subscription.Plan.FREE
            or not subscription.current_period_end
        ):
            raise ValidationError({"detail": "No active subscription is linked."})
        if settings.BILLING_PROVIDER == "mock":
            subscription.cancel_at_period_end = True
            subscription.save(update_fields=["cancel_at_period_end", "updated_at"])
            _audit_subscription_change(request.user, "subscription_cancel_at_period_end")
            return Response({"cancel_at_period_end": True})
        if not subscription.stripe_subscription_id:
            raise ValidationError({"detail": "No active Stripe subscription is linked."})
        stripe = get_stripe()
        stripe.Subscription.modify(
            subscription.stripe_subscription_id,
            cancel_at_period_end=True,
        )
        subscription.cancel_at_period_end = True
        subscription.save(update_fields=["cancel_at_period_end", "updated_at"])
        _audit_subscription_change(request.user, "subscription_cancel_at_period_end")
        return Response({"cancel_at_period_end": True})


@extend_schema_view(
    post=extend_schema(
        request=OpenApiTypes.NONE,
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT, 503: OpenApiTypes.OBJECT}
    )
)
class ResumeSubscriptionView(StripeOnlyAPIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        subscription = request.user.subscription
        if (
            subscription.plan == Subscription.Plan.FREE
            or not subscription.cancel_at_period_end
        ):
            raise ValidationError({"detail": "No subscription is scheduled to end."})
        if not subscription.stripe_subscription_id:
            raise ValidationError({"detail": "No Stripe subscription is linked."})
        stripe = get_stripe()
        stripe.Subscription.modify(
            subscription.stripe_subscription_id,
            cancel_at_period_end=False,
        )
        subscription.cancel_at_period_end = False
        subscription.save(update_fields=["cancel_at_period_end", "updated_at"])
        _audit_subscription_change(request.user, "subscription_resumed")
        return Response({"cancel_at_period_end": False})


@extend_schema_view(
    post=extend_schema(
        request=OpenApiTypes.OBJECT,
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT, 503: OpenApiTypes.OBJECT},
    )
)
class ChangePlanView(StripeOnlyAPIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        plan = request.data.get("plan")
        if plan not in {Subscription.Plan.PRO, Subscription.Plan.AGENCY}:
            raise ValidationError({"plan": ["Choose pro or agency."]})
        subscription = request.user.subscription
        if subscription.plan == plan:
            raise ValidationError({"plan": ["The requested plan is already active."]})
        if not subscription.stripe_subscription_id:
            raise ValidationError({"detail": "No Stripe subscription is linked."})
        price_id = _stripe_pro_price_id() if plan == Subscription.Plan.PRO else settings.STRIPE_AGENCY_PRICE_ID
        if not price_id:
            raise StripeConfigurationError("The selected plan has no Stripe price configured.")
        stripe = get_stripe()
        remote = stripe.Subscription.retrieve(subscription.stripe_subscription_id)
        items = remote.get("items", {}).get("data", [])
        if not items:
            raise ValidationError({"detail": "The Stripe subscription has no plan item."})
        stripe.Subscription.modify(
            subscription.stripe_subscription_id,
            items=[{"id": items[0]["id"], "price": price_id}],
            proration_behavior="create_prorations",
        )
        _audit_subscription_change(
            request.user,
            "subscription_plan_changed",
            {"from": subscription.plan, "to": plan},
        )
        return Response({"plan": subscription.plan, "requested_plan": plan})


@extend_schema_view(
    get=extend_schema(responses={200: OpenApiTypes.OBJECT, 503: OpenApiTypes.OBJECT})
)
class InvoiceListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        subscription = get_subscription(request.user)
        if settings.BILLING_PROVIDER == "mock" or not subscription.stripe_customer_id:
            return Response({"results": []})
        stripe = get_stripe()
        invoices = stripe.Invoice.list(customer=subscription.stripe_customer_id, limit=100)
        results = []
        for invoice in invoices.get("data", []):
            results.append({
                "id": invoice.get("id"),
                "amount": invoice.get("amount_paid", invoice.get("total")),
                "currency": invoice.get("currency"),
                "status": invoice.get("status"),
                "date": invoice.get("created"),
                "hosted_invoice_url": invoice.get("hosted_invoice_url"),
            })
        return Response({"results": results})


@extend_schema_view(get=extend_schema(responses={200: OpenApiTypes.OBJECT}))
class BillingMeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(billing_me_data(request.user))


@extend_schema_view(
    post=extend_schema(
        request=OpenApiTypes.NONE,
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT, 503: OpenApiTypes.OBJECT},
    )
)
class CancelSubscriptionView(CancelAtPeriodEndView):
    pass


@extend_schema_view(
    post=extend_schema(
        request=OpenApiTypes.NONE,
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT, 503: OpenApiTypes.OBJECT},
    )
)
class SetupIntentView(StripeOnlyAPIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "billing_setup_intent"

    def post(self, request):
        if not settings.STRIPE_PUBLISHABLE_KEY:
            raise StripeConfigurationError("Stripe publishable key is not configured.")
        stripe = get_stripe()
        subscription = _get_or_create_stripe_customer(request.user, stripe)
        setup_intent = _stripe_call(
            stripe.SetupIntent.create,
            customer=subscription.stripe_customer_id,
            payment_method_types=["card"],
            usage="off_session",
        )
        return Response(
            {
                "setup_intent_id": setup_intent.id,
                "client_secret": setup_intent.client_secret,
                "publishable_key": settings.STRIPE_PUBLISHABLE_KEY,
            }
        )


@extend_schema_view(
    get=extend_schema(responses={200: OpenApiTypes.OBJECT, 503: OpenApiTypes.OBJECT})
)
class PaymentMethodView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        subscription = get_subscription(request.user)
        if subscription.plan == Subscription.Plan.FREE:
            return Response(None)
        if settings.BILLING_PROVIDER == "mock":
            if not subscription.card_brand:
                return Response(None)
            return Response(
                {
                    "brand": subscription.card_brand,
                    "last4": subscription.card_last4,
                    "exp_month": subscription.card_exp_month,
                    "exp_year": subscription.card_exp_year,
                }
            )
        if not subscription.stripe_customer_id:
            return Response(None)

        stripe = get_stripe()
        customer = _stripe_call(
            stripe.Customer.retrieve,
            subscription.stripe_customer_id,
        )
        invoice_settings = _stripe_value(customer, "invoice_settings", {}) or {}
        default_payment_method = _stripe_value(
            invoice_settings, "default_payment_method"
        )
        if not default_payment_method:
            return Response(None)

        payment_method = (
            default_payment_method
            if _stripe_value(default_payment_method, "card") is not None
            else _stripe_call(
                stripe.PaymentMethod.retrieve,
                _stripe_id(default_payment_method),
            )
        )
        card = _stripe_value(payment_method, "card", {}) or {}
        if not card:
            return Response(None)
        return Response(
            {
                "brand": _stripe_value(card, "brand"),
                "last4": _stripe_value(card, "last4"),
                "exp_month": _stripe_value(card, "exp_month"),
                "exp_year": _stripe_value(card, "exp_year"),
            }
        )


@extend_schema_view(
    post=extend_schema(
        request=OpenApiTypes.OBJECT,
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT, 503: OpenApiTypes.OBJECT},
    )
)
@method_decorator(sensitive_post_parameters(), name="dispatch")
class SubscribeView(StripeOnlyAPIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "billing_subscribe"

    def post(self, request):
        _require_verified_email(request.user)
        if not isinstance(request.data, dict):
            raise ValidationError({"setup_intent_id": ["Provide a valid SetupIntent ID."]})
        result = process_payment(request.user, request.data)
        if not result.success:
            detail = result.errors.get(
                "detail", "The payment could not be completed."
            )
            return Response(
                {
                    "detail": detail,
                    "code": result.error_code,
                    "errors": {
                        key: value
                        for key, value in result.errors.items()
                        if key != "detail"
                    },
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(
            {
                "subscription_id": result.subscription_id,
                "client_secret": result.client_secret,
            },
            status=status.HTTP_201_CREATED,
        )


@method_decorator(sensitive_post_parameters(), name="dispatch")
class MockSubscribeView(MockOnlyAPIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "billing_subscribe"

    def post(self, request):
        _require_verified_email(request.user)
        if not isinstance(request.data, dict):
            return Response(
                {
                    "detail": "Provide the card details.",
                    "code": "invalid_card",
                    "errors": {"card_number": "Enter a valid card number."},
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        plan = request.data.get("plan", Subscription.Plan.PRO)
        valid_plans = {Subscription.Plan.PRO, Subscription.Plan.AGENCY}
        if not isinstance(plan, str) or plan not in valid_plans:
            return Response(
                {
                    "detail": "Choose a valid paid plan.",
                    "code": "invalid_plan",
                    "errors": {"plan": "Choose Pro or Agency."},
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        card_data = {
            key: request.data.get(key)
            for key in ("card_number", "exp_month", "exp_year", "cvv")
        }
        result = process_payment(request.user, card_data)
        if not result.success:
            detail = result.errors.get("detail", "The payment could not be completed.")
            return Response(
                {
                    "detail": detail,
                    "code": "payment_failed",
                    "errors": {
                        key: value
                        for key, value in result.errors.items()
                        if key != "detail"
                    },
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        subscription = get_subscription(request.user)
        now = timezone.now()
        subscription.plan = plan
        subscription.status = Subscription.Status.ACTIVE
        subscription.current_period_end = now + timedelta(days=30)
        subscription.next_billing_date = subscription.current_period_end
        subscription.started_at = subscription.started_at or now
        subscription.last_payment_at = now
        subscription.cancel_at_period_end = False
        subscription.card_brand = result.brand
        subscription.card_last4 = result.last4
        subscription.card_exp_month = result.exp_month
        subscription.card_exp_year = result.exp_year
        subscription.save(
            update_fields=[
                "plan",
                "status",
                "current_period_end",
                "next_billing_date",
                "started_at",
                "last_payment_at",
                "cancel_at_period_end",
                "card_brand",
                "card_last4",
                "card_exp_month",
                "card_exp_year",
                "updated_at",
            ]
        )
        return Response(billing_me_data(request.user), status=status.HTTP_200_OK)


@extend_schema_view(
    post=extend_schema(
        request=OpenApiTypes.OBJECT,
        responses={201: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT, 503: OpenApiTypes.OBJECT},
    )
)
class CheckoutView(StripeOnlyAPIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        _require_verified_email(request.user)
        plan = request.data.get("plan")
        if plan not in {Subscription.Plan.PRO, Subscription.Plan.AGENCY}:
            raise ValidationError({"plan": ["Choose pro or agency."]})
        price_id = (
            _stripe_pro_price_id()
            if plan == Subscription.Plan.PRO
            else settings.STRIPE_AGENCY_PRICE_ID
        )
        if not price_id:
            raise StripeConfigurationError("The selected plan has no Stripe price configured.")

        stripe = get_stripe()
        subscription = _get_or_create_stripe_customer(request.user, stripe)
        customer_id = subscription.stripe_customer_id

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
        request=OpenApiTypes.NONE,
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT, 503: OpenApiTypes.OBJECT}
    )
)
class CustomerPortalView(StripeOnlyAPIView):
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
@method_decorator(csrf_exempt, name="dispatch")
class StripeWebhookView(StripeOnlyAPIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        stripe = get_stripe()
        if not settings.STRIPE_WEBHOOK_SECRET:
            raise StripeConfigurationError("The Stripe webhook secret is not configured.")
        signature = request.headers.get("Stripe-Signature", "")
        try:
            event = stripe.Webhook.construct_event(
                request.body,
                signature,
                settings.STRIPE_WEBHOOK_SECRET,
            )
        except (ValueError, stripe.SignatureVerificationError):
            return Response(
                {"detail": "Invalid Stripe webhook signature.", "code": "invalid_signature"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            processed = process_stripe_event(event)
        return Response({"received": True, "duplicate": not processed})