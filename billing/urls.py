from django.urls import path

from billing.views import (
    BillingMeView,
    CancelAtPeriodEndView,
    CancelSubscriptionView,
    ChangePlanView,
    CheckoutView,
    CustomerPortalView,
    InvoiceListView,
    ResumeSubscriptionView,
    StripeWebhookView,
    SubscriptionDetailView,
    TestSubscribeView,
)


urlpatterns = [
    path("me/", BillingMeView.as_view(), name="billing-me"),
    path("subscription/", SubscriptionDetailView.as_view(), name="billing-subscription"),
    path("cancel-at-period-end/", CancelAtPeriodEndView.as_view(), name="billing-cancel-at-period-end"),
    path("resume/", ResumeSubscriptionView.as_view(), name="billing-resume"),
    path("change-plan/", ChangePlanView.as_view(), name="billing-change-plan"),
    path("invoices/", InvoiceListView.as_view(), name="billing-invoices"),
    path("test-subscribe/", TestSubscribeView.as_view(), name="billing-test-subscribe"),
    path("cancel/", CancelSubscriptionView.as_view(), name="billing-cancel"),
    path("checkout/", CheckoutView.as_view(), name="billing-checkout"),
    path("portal/", CustomerPortalView.as_view(), name="billing-portal"),
    path("webhook/", StripeWebhookView.as_view(), name="stripe-webhook"),
]