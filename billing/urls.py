from django.urls import path

from billing.views import (
    BillingMeView,
    CancelAtPeriodEndView,
    CancelSubscriptionView,
    ChangePlanView,
    CheckoutView,
    CustomerPortalView,
    InvoiceListView,
    PaymentMethodView,
    ResumeSubscriptionView,
    SetupIntentView,
    SubscribeView,
    StripeWebhookView,
    SubscriptionDetailView,
    MockSubscribeView,
)

urlpatterns = [
    path("me/", BillingMeView.as_view(), name="billing-me"),
    path("subscription/", SubscriptionDetailView.as_view(), name="billing-subscription"),
    path("cancel/", CancelSubscriptionView.as_view(), name="billing-cancel"),
    path("cancel-at-period-end/", CancelAtPeriodEndView.as_view(), name="billing-cancel-at-period-end"),
    path("payment-method/", PaymentMethodView.as_view(), name="billing-payment-method"),
    path("mock-subscribe/", MockSubscribeView.as_view(), name="billing-mock-subscribe"),
    path("setup-intent/", SetupIntentView.as_view(), name="billing-setup-intent"),
    path("subscribe/", SubscribeView.as_view(), name="billing-subscribe"),
    path("webhook/", StripeWebhookView.as_view(), name="stripe-webhook"),
    path("checkout/", CheckoutView.as_view(), name="billing-checkout"),
    path("portal/", CustomerPortalView.as_view(), name="billing-portal"),
    path("resume/", ResumeSubscriptionView.as_view(), name="billing-resume"),
    path("change-plan/", ChangePlanView.as_view(), name="billing-change-plan"),
    path("invoices/", InvoiceListView.as_view(), name="billing-invoices"),
]