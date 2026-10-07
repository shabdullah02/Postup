from rest_framework import serializers

from socials.models import SocialAccount


class SocialAccountSerializer(serializers.ModelSerializer):
    class Meta:
        model = SocialAccount
        fields = ("id", "platform", "external_id", "name", "expires_at", "created_at")
        read_only_fields = fields
