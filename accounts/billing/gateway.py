import re
from dataclasses import dataclass, field
from datetime import date

from django.conf import settings
from rest_framework import status
from rest_framework.exceptions import APIException


@dataclass(frozen=True)
class PaymentResult:
    success: bool
    brand: str = ""
    last4: str = ""
    exp_month: int | None = None
    exp_year: int | None = None
    errors: dict[str, str] = field(default_factory=dict)
    error_code: str = "payment_failed"
    subscription_id: str | None = None
    client_secret: str | None = None


class PaymentGatewayError(APIException):
    status_code = status.HTTP_502_BAD_GATEWAY
    default_detail = "The payment provider could not complete the request."
    default_code = "payment_provider_error"


def _luhn_valid(number):
    digits = [int(digit) for digit in number]
    checksum = 0
    parity = len(digits) % 2
    for index, digit in enumerate(digits):
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


def _card_brand(number):
    if number.startswith("4"):
        return "Visa"
    if number[:2] in {"34", "37"}:
        return "Amex"
    if (
        51 <= int(number[:2]) <= 55
        or 2221 <= int(number[:4]) <= 2720
    ):
        return "Mastercard"
    if (
        number.startswith("6011")
        or number.startswith("65")
        or 644 <= int(number[:3]) <= 649
        or (
            number.startswith("622")
            and 622126 <= int(number[:6]) <= 622925
        )
    ):
        return "Discover"
    return "Other"


def _valid_expiry(month, year):
    if isinstance(month, bool) or isinstance(year, bool):
        return None
    try:
        month = int(month)
        year = int(year)
    except (TypeError, ValueError):
        return None
    if not 1 <= month <= 12:
        return None
    if 0 <= year < 100:
        year += 2000
    today = date.today()
    if (year, month) < (today.year, today.month):
        return None
    return month, year


def validate_card(card):
    if not isinstance(card, dict):
        return None, {"card_number": "Enter a valid card number."}
    errors = {}
    number = card.get("card_number", "")
    if not isinstance(number, str):
        number = ""
    number = re.sub(r"[ -]", "", number)
    if not number.isdigit() or not 13 <= len(number) <= 19 or not _luhn_valid(number):
        errors["card_number"] = "Enter a valid card number."
        brand = "Other"
    else:
        brand = _card_brand(number)

    expiry = _valid_expiry(card.get("exp_month"), card.get("exp_year"))
    if expiry is None:
        errors["expiry"] = "Enter a valid, unexpired expiration month and year."
        exp_month = exp_year = None
    else:
        exp_month, exp_year = expiry

    cvv = card.get("cvv", "")
    expected_cvv_length = 4 if brand == "Amex" else 3
    if (
        not isinstance(cvv, str)
        or not cvv.isdigit()
        or len(cvv) != expected_cvv_length
    ):
        errors["cvv"] = (
            f"Enter a {expected_cvv_length}-digit security code for this card."
        )

    if errors:
        return None, errors
    return {
        "number": number,
        "brand": brand,
        "last4": number[-4:],
        "exp_month": exp_month,
        "exp_year": exp_year,
    }, {}


def _process_mock_payment(user, card):
    validated, errors = validate_card(card)
    if errors:
        return PaymentResult(success=False, errors=errors)
    if validated["number"] == "4000000000000002":
        return PaymentResult(
            success=False,
            errors={"detail": "Card declined."},
        )
    return PaymentResult(
        success=True,
        brand=validated["brand"],
        last4=validated["last4"],
        exp_month=validated["exp_month"],
        exp_year=validated["exp_year"],
    )


def _stripe_value(resource, key, default=None):
    if isinstance(resource, dict):
        return resource.get(key, default)
    getter = getattr(resource, "get", None)
    return getter(key, default) if getter else getattr(resource, key, default)


def _stripe_id(resource):
    return resource if isinstance(resource, str) else _stripe_value(resource, "id")


def _process_stripe_payment(user, card):
    import stripe

    from billing.entitlements import get_subscription
    from billing.models import Subscription
    from billing.stripe_utils import get_stripe

    stripe_client = get_stripe()
    subscription = get_subscription(user)
    if subscription.stripe_subscription_id and subscription.status != Subscription.Status.CANCELED:
        return PaymentResult(
            success=False,
            errors={"detail": "A subscription is already linked to this account."},
        )
    if not settings.STRIPE_PRICE_ID_PRO and not settings.STRIPE_PRO_PRICE_ID:
        return PaymentResult(
            success=False,
            errors={"detail": "The Pro Stripe price is not configured."},
        )

    customer_id = subscription.stripe_customer_id
    if not customer_id:
        try:
            customer = stripe_client.Customer.create(
                email=user.email,
                name=user.get_username(),
                metadata={"user_id": str(user.pk)},
                idempotency_key=f"postup-customer-{user.pk}",
            )
        except stripe.StripeError as exc:
            raise PaymentGatewayError() from exc
        customer_id = customer.id
        subscription.stripe_customer_id = customer_id
        subscription.save(update_fields=["stripe_customer_id", "updated_at"])

    setup_intent_id = card.get("setup_intent_id")
    payment_method_id = None
    if setup_intent_id:
        try:
            setup_intent = stripe_client.SetupIntent.retrieve(setup_intent_id)
        except stripe.StripeError as exc:
            raise PaymentGatewayError() from exc
        if (
            _stripe_value(setup_intent, "status") != "succeeded"
            or _stripe_id(_stripe_value(setup_intent, "customer")) != customer_id
        ):
            return PaymentResult(
                success=False,
                errors={"detail": "The saved payment method could not be verified."},
                error_code="payment_method_required",
            )
        payment_method_id = _stripe_id(_stripe_value(setup_intent, "payment_method"))
        if payment_method_id:
            try:
                stripe_client.Customer.modify(
                    customer_id,
                    invoice_settings={"default_payment_method": payment_method_id},
                )
            except stripe.StripeError as exc:
                raise PaymentGatewayError() from exc
    else:
        try:
            customer = stripe_client.Customer.retrieve(customer_id)
        except stripe.StripeError as exc:
            raise PaymentGatewayError() from exc
        invoice_settings = _stripe_value(customer, "invoice_settings", {}) or {}
        payment_method_id = _stripe_id(
            _stripe_value(invoice_settings, "default_payment_method")
        )

    if not payment_method_id:
        return PaymentResult(
            success=False,
            errors={"detail": "Add and save a payment method before subscribing."},
            error_code="payment_method_required",
        )

    try:
        remote_subscription = stripe_client.Subscription.create(
            customer=customer_id,
            items=[{
                "price": settings.STRIPE_PRICE_ID_PRO or settings.STRIPE_PRO_PRICE_ID,
            }],
            default_payment_method=payment_method_id,
            payment_behavior="default_incomplete",
            expand=["latest_invoice.payment_intent"],
            metadata={"user_id": str(user.pk)},
            idempotency_key=f"postup-subscribe-{user.pk}-{subscription.updated_at.timestamp()}",
        )
    except stripe.StripeError as exc:
        raise PaymentGatewayError() from exc

    subscription_id = _stripe_id(remote_subscription)
    if subscription_id:
        subscription.stripe_subscription_id = subscription_id
        subscription.save(update_fields=["stripe_subscription_id", "updated_at"])
    latest_invoice = _stripe_value(remote_subscription, "latest_invoice")
    payment_intent = _stripe_value(latest_invoice, "payment_intent")
    return PaymentResult(
        success=True,
        subscription_id=subscription_id,
        client_secret=_stripe_value(payment_intent, "client_secret"),
    )


def process_payment(user, card):
    provider = settings.BILLING_PROVIDER
    if provider == "mock":
        return _process_mock_payment(user, card)
    if provider == "stripe":
        return _process_stripe_payment(user, card)
    raise PaymentGatewayError("Unsupported billing provider.")
