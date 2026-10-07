from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from posts.models import Post
from posts.serializers import PostSerializer


class PostViewSet(viewsets.ModelViewSet):
    serializer_class = PostSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Post.objects.filter(user=self.request.user).select_related("account__user")

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

    def update(self, request, *args, **kwargs):
        post = self.get_object()
        if post.status not in {Post.Status.DRAFT, Post.Status.SCHEDULED}:
            return Response(
                {"detail": "Only draft or scheduled posts can be modified."},
                status=400,
            )
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        post = self.get_object()
        if post.status not in {Post.Status.DRAFT, Post.Status.SCHEDULED}:
            return Response(
                {"detail": "Only draft or scheduled posts can be modified."},
                status=400,
            )
        return super().destroy(request, *args, **kwargs)

    @action(detail=True, methods=["post"])
    def schedule(self, request, *args, **kwargs):
        post = self.get_object()
        scheduled_at = request.data.get("scheduled_at")
        if scheduled_at is None:
            return Response({"scheduled_at": ["This field is required."]}, status=400)

        parsed_at = parse_datetime(scheduled_at)
        if parsed_at is None:
            return Response({"scheduled_at": ["Invalid ISO datetime."]}, status=400)
        if timezone.is_naive(parsed_at):
            return Response({"scheduled_at": ["Datetime must be timezone-aware."]}, status=400)

        if parsed_at <= timezone.now():
            return Response({"scheduled_at": ["Scheduled time must be in the future."]}, status=400)

        if post.status not in {Post.Status.DRAFT, Post.Status.SCHEDULED}:
            return Response({"detail": "Post cannot be scheduled from its current status."}, status=400)

        post.scheduled_at = parsed_at
        post.status = Post.Status.SCHEDULED
        post.error = ""
        post.save(update_fields=["scheduled_at", "status", "error"])
        return Response(PostSerializer(post).data)
