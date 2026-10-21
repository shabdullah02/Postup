from django.urls import path

from .views import (
    CsrfView,
    FacebookCallbackView,
    FacebookConnectView,
    FacebookSelectPagesView,
    LoginView,
    LogoutView,
    MeView,
    RegistrationView,
    SocialAccountsListView,
)

urlpatterns = [
    path("csrf/", CsrfView.as_view(), name="csrf"),
    path("login/", LoginView.as_view(), name="login"),
    path("register/", RegistrationView.as_view(), name="register"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("me/", MeView.as_view(), name="me"),
    path("connect/facebook/", FacebookConnectView.as_view(), name="facebook-connect"),
    path("connect/facebook/select/", FacebookSelectPagesView.as_view(), name="facebook-select-pages"),
    path("callback/facebook/", FacebookCallbackView.as_view(), name="facebook-callback"),
    path("social-accounts/", SocialAccountsListView.as_view(), name="social-accounts-list"),
]
