import logging

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from drf_spectacular.utils import OpenApiTypes, extend_schema_view, extend_schema

from socials.throttles import AIGenerateThrottle

logger = logging.getLogger(__name__)


@extend_schema_view(
    post=extend_schema(
        request=OpenApiTypes.OBJECT,
        responses={
            200: OpenApiTypes.OBJECT,
            400: OpenApiTypes.OBJECT,
            429: OpenApiTypes.OBJECT,
            500: OpenApiTypes.OBJECT,
            502: OpenApiTypes.OBJECT,
            503: OpenApiTypes.OBJECT,
        },
    )
)
class AIGenerateView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [AIGenerateThrottle]

    def post(self, request):
        topic = request.data.get("topic")
        if not isinstance(topic, str) or not topic.strip():
            return Response(
                {"detail": "Topic is required.", "code": "validation_error"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        topic = " ".join(topic.split())

        try:
            from article_publisher import exceptions as article_exceptions
            from article_publisher import generate_article
        except ImportError as exc:
            logger.exception("Article publisher import failed")
            return Response(
                {
                    "detail": "Generation failed. Try again later.",
                    "code": "generation_import_error",
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        try:
            content = generate_article(topic)
        except Exception as exc:
            exception_types = tuple(
                exception
                for exception in vars(article_exceptions).values()
                if isinstance(exception, type) and issubclass(exception, Exception)
            )
            if isinstance(exc, exception_types):
                logger.exception("Article generation failed")
                if _is_gemini_overloaded(exc):
                    response = Response(
                        {
                            "detail": "The generation service is busy. Try again later.",
                            "code": "generation_overloaded",
                        },
                        status=status.HTTP_503_SERVICE_UNAVAILABLE,
                    )
                    response["Retry-After"] = "60"
                    return response
                return Response(
                    {
                        "detail": "Generation failed. Try again later.",
                        "code": "generation_failed",
                    },
                    status=status.HTTP_502_BAD_GATEWAY,
                )
            raise

        return Response({"content": content})


def _is_gemini_overloaded(exc):
    seen = set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        code = getattr(current, "code", None)
        if code in {429, 500, 503}:
            return True
        current = current.__cause__ or current.__context__
    return False
