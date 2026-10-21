from django.urls import path

from billing.views import (
    BillingMeView,
    CancelSubscriptionView,
    CheckoutView,
    CustomerPortalView,
    StripeWebhookView,
    TestSubscribeView,
)


urlpatterns = [
    path("me/", BillingMeView.as_view(), name="billing-me"),
    path("test-subscribe/", TestSubscribeView.as_view(), name="billing-test-subscribe"),
    path("cancel/", CancelSubscriptionView.as_view(), name="billing-cancel"),
    path("checkout/", CheckoutView.as_view(), name="billing-checkout"),
    path("portal/", CustomerPortalView.as_view(), name="billing-portal"),
    path("webhook/", StripeWebhookView.as_view(), name="stripe-webhook"),
]