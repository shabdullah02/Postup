from django.utils import timezone
from rest_framework import serializers

from accounts.models import SocialAccount
from posts.models import Post

THREADS_MAX_TEXT_LENGTH = 500


class PostSerializer(serializers.ModelSerializer):
    class Meta:
        model = Post
        fields = (
            "id",
            "user",
            "account",
            "content",
            "image_url",
            "scheduled_at",
            "status",
            "error",
            "external_post_id",
            "permalink_url",
            "posted_at",
            "created_at",
        )
        read_only_fields = (
            "id",
            "user",
            "status",
            "error",
            "external_post_id",
            "permalink_url",
            "posted_at",
            "created_at",
        )

    def validate_account(self, value):
        if value.user != self.context["request"].user:
            raise serializers.ValidationError("Account does not belong to the user.")
        return value

    def validate(self, attrs):
        attrs = super().validate(attrs)
        account = attrs.get("account") or getattr(self.instance, "account", None)
        content = attrs.get("content")
        if content is None and self.instance is not None:
            content = self.instance.content
        if account is not None and account.provider == SocialAccount.Provider.THREADS:
            if not (content or "").strip():
                raise serializers.ValidationError(
                    {"content": "Threads posts cannot be empty."}
                )
            if len(content) > THREADS_MAX_TEXT_LENGTH:
                raise serializers.ValidationError(
                    {
                        "content": (
                            f"Threads posts are limited to "
                            f"{THREADS_MAX_TEXT_LENGTH} characters."
                        )
                    }
                )
        return attrs

    def validate_scheduled_at(self, value):
        if value is None:
            return value
        if timezone.is_naive(value):
            raise serializers.ValidationError("Scheduled datetime must be timezone-aware.")
        if value <= timezone.now():
            raise serializers.ValidationError("Scheduled datetime must be in the future.")
        return value
