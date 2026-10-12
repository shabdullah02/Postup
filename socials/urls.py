from django.urls import path

from socials.views import (
    AIGenerateView,
    FacebookCallbackView,
    FacebookConnectView,
    SocialAccountListView,
)

urlpatterns = [
    path("accounts/", SocialAccountListView.as_view(), name="social-account-list"),
    path("facebook/connect/", FacebookConnectView.as_view(), name="facebook-connect"),
    path("facebook/callback/", FacebookCallbackView.as_view(), name="facebook-callback"),
    path("ai/generate/", AIGenerateView.as_view(), name="ai-generate"),
]
