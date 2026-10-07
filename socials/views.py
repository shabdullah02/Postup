import logging

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from socials.models import SocialAccount
from socials.serializers import SocialAccountSerializer
from socials.throttles import AIGenerateThrottle

logger = logging.getLogger(__name__)


class SocialAccountListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        accounts = SocialAccount.objects.filter(user=request.user)
        serializer = SocialAccountSerializer(accounts, many=True)
        return Response(serializer.data)


class AIGenerateView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [AIGenerateThrottle]

    def post(self, request):
        topic = request.data.get("topic")
        if not isinstance(topic, str) or not topic.strip():
            return Response({"topic": ["This field is required."]}, status=status.HTTP_400_BAD_REQUEST)

        try:
            from article_publisher import exceptions as article_exceptions
            from article_publisher import generate_article
        except ImportError as exc:
            logger.exception("Article publisher import failed")
            return Response(
                {"detail": "Generation failed. Try again later."},
                status=status.HTTP_502_BAD_GATEWAY,
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
                return Response(
                    {"detail": "Generation failed. Try again later."},
                    status=status.HTTP_502_BAD_GATEWAY,
                )
            raise

        return Response({"content": content})
