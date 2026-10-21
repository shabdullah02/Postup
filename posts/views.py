from django.utils import timezone
from django.utils.dateparse import parse_datetime
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from posts.models import Post
from posts.serializers import PostSerializer
from billing.entitlements import ensure_within_plan_limit
from config.api import error_response


@extend_schema_view(
    retrieve=extend_schema(parameters=[OpenApiParameter("pk", int, OpenApiParameter.PATH)]),
    update=extend_schema(parameters=[OpenApiParameter("pk", int, OpenApiParameter.PATH)]),
    partial_update=extend_schema(parameters=[OpenApiParameter("pk", int, OpenApiParameter.PATH)]),
    destroy=extend_schema(parameters=[OpenApiParameter("pk", int, OpenApiParameter.PATH)]),
    schedule=extend_schema(parameters=[OpenApiParameter("pk", int, OpenApiParameter.PATH)]),
)
class PostViewSet(viewsets.ModelViewSet):
    serializer_class = PostSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = None

    def get_queryset(self):
        return Post.objects.filter(user=self.request.user).select_related("account__user")

    def perform_create(self, serializer):
        ensure_within_plan_limit(self.request.user, "posts_per_month")
        serializer.save(user=self.request.user)

    def update(self, request, *args, **kwargs):
        post = self.get_object()
        if post.status not in {Post.Status.DRAFT, Post.Status.SCHEDULED}:
            return error_response(
                "Only draft or scheduled posts can be modified.",
                "invalid_post_state",
                400,
            )
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        post = self.get_object()
        if post.status not in {Post.Status.DRAFT, Post.Status.SCHEDULED}:
            return error_response(
                "Only draft or scheduled posts can be modified.",
                "invalid_post_state",
                400,
            )
        return super().destroy(request, *args, **kwargs)

    @action(detail=True, methods=["post"])
    def schedule(self, request, *args, **kwargs):
        post = self.get_object()
        scheduled_at = request.data.get("scheduled_at")
        if scheduled_at is None:
            return error_response("scheduled_at is required.", "validation_error", 400)

        parsed_at = parse_datetime(scheduled_at)
        if parsed_at is None:
            return error_response("Invalid ISO datetime.", "validation_error", 400)
        if timezone.is_naive(parsed_at):
            return error_response("Datetime must be timezone-aware.", "validation_error", 400)

        if parsed_at <= timezone.now():
            return error_response("Scheduled time must be in the future.", "validation_error", 400)

        if post.status not in {Post.Status.DRAFT, Post.Status.SCHEDULED}:
            return error_response(
                "Post cannot be scheduled from its current status.",
                "invalid_post_state",
                400,
            )

        now = timezone.localtime()
        period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        additional = int(
            post.status != Post.Status.SCHEDULED
            and timezone.localtime(post.created_at) < period_start
        )
        ensure_within_plan_limit(
            request.user, "posts_per_month", additional=additional
        )

        post.scheduled_at = parsed_at
        post.status = Post.Status.SCHEDULED
        post.error = ""
        post.save(update_fields=["scheduled_at", "status", "error"])
        return Response(PostSerializer(post).data)
