from django.urls import path

from socials.views import AIGenerateView, SocialAccountListView

urlpatterns = [
    path("accounts/", SocialAccountListView.as_view(), name="social-account-list"),
    path("ai/generate/", AIGenerateView.as_view(), name="ai-generate"),
]
