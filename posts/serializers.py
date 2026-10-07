from django.utils import timezone
from rest_framework import serializers

from posts.models import Post


class PostSerializer(serializers.ModelSerializer):
    class Meta:
        model = Post
        fields = (
            "id",
            "user",
            "account",
            "content",
            "scheduled_at",
            "status",
            "error",
            "external_post_id",
            "posted_at",
            "created_at",
        )
        read_only_fields = (
            "id",
            "user",
            "status",
            "error",
            "external_post_id",
            "posted_at",
            "created_at",
        )

    def validate_account(self, value):
        if value.user != self.context["request"].user:
            raise serializers.ValidationError("Account does not belong to the user.")
        return value

    def validate_scheduled_at(self, value):
        if value is None:
            return value
        if timezone.is_naive(value):
            raise serializers.ValidationError("Scheduled datetime must be timezone-aware.")
        if value <= timezone.now():
            raise serializers.ValidationError("Scheduled datetime must be in the future.")
        return value
