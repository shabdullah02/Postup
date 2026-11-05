from rest_framework import status
from rest_framework.authentication import SessionAuthentication
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import SocialAccount
from socials.adapters import AuthExpiredError

from .services import (
    AnalyticsUnavailableError,
    get_page_overview,
    get_page_posts,
)

ANALYTICS_UNAVAILABLE = {
    "detail": "Analytics unavailable. Try again later.",
    "code": "facebook_unavailable",
}


def _get_account(request):
    account_id = request.query_params.get("account")
    if not account_id:
        return None, Response(
            {"detail": "Account is required.", "code": "validation_error"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        account = SocialAccount.objects.get(
            pk=account_id,
            user=request.user,
            provider=SocialAccount.Provider.FACEBOOK,
        )
    except (SocialAccount.DoesNotExist, ValueError, TypeError):
        return None, Response(status=status.HTTP_404_NOT_FOUND)
    return account, None


def _check_account_access(account):
    if account.status == SocialAccount.Status.NEEDS_REAUTH:
        return Response(
            {"code": "needs_reauth"},
            status=status.HTTP_409_CONFLICT,
        )
    if "read_insights" not in {
        scope.strip() for scope in account.scopes.split(",") if scope.strip()
    }:
        return Response(
            {
                "detail": "Facebook analytics permission is required.",
                "code": "missing_permission",
            },
            status=status.HTTP_403_FORBIDDEN,
        )
    return None


class PageOverviewView(APIView):
    authentication_classes = [SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        account, error_response = _get_account(request)
        if error_response:
            return error_response
        access_error = _check_account_access(account)
        if access_error:
            return access_error

        days = request.query_params.get("days", "28")
        if days not in {"7", "28", "90"}:
            return Response(
                {"detail": "Days must be 7, 28, or 90.", "code": "validation_error"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            return Response(get_page_overview(account, int(days)))
        except AuthExpiredError:
            return Response(
                {"code": "needs_reauth"},
                status=status.HTTP_409_CONFLICT,
            )
        except AnalyticsUnavailableError:
            return Response(ANALYTICS_UNAVAILABLE, status=status.HTTP_502_BAD_GATEWAY)


class PagePostsView(APIView):
    authentication_classes = [SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        account, error_response = _get_account(request)
        if error_response:
            return error_response
        access_error = _check_account_access(account)
        if access_error:
            return access_error

        try:
            return Response(get_page_posts(account))
        except AuthExpiredError:
            return Response(
                {"code": "needs_reauth"},
                status=status.HTTP_409_CONFLICT,
            )
        except AnalyticsUnavailableError:
            return Response(ANALYTICS_UNAVAILABLE, status=status.HTTP_502_BAD_GATEWAY)
