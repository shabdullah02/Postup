from django.urls import path

from socials.views import (
    AIGenerateView,
    SocialAccountDetailView,
)

urlpatterns = [
    path("ai/generate/", AIGenerateView.as_view(), name="ai-generate"),
    path(
        "accounts/<int:pk>/",
        SocialAccountDetailView.as_view(),
        name="social-account-detail",
    ),
]
