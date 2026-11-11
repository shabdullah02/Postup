from django.urls import path

from accounts.views import ThreadsConnectView
from socials.views import (
    AIGenerateView,
    SocialAccountDetailView,
)

urlpatterns = [
    path("ai/generate/", AIGenerateView.as_view(), name="ai-generate"),
    path(
        "socials/threads/connect/",
        ThreadsConnectView.as_view(),
        name="threads-connect",
    ),
    path(
        "accounts/<int:pk>/",
        SocialAccountDetailView.as_view(),
        name="social-account-detail",
    ),
]
