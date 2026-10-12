from django.contrib import admin

from .models import SocialAccount


@admin.register(SocialAccount)
class SocialAccountAdmin(admin.ModelAdmin):
    exclude = ("token_encrypted",)
    list_display = ("user", "provider", "external_id", "name", "status", "connected_at")
    list_filter = ("provider", "status")
    search_fields = ("external_id", "name", "user__username")
