from rest_framework import serializers

from accounts.models import SocialAccount


class SocialAccountSerializer(serializers.ModelSerializer):
    class Meta:
        model = SocialAccount
        fields = ("id", "provider", "external_id", "name", "status", "scopes", "connected_at")
        read_only_fields = fields
