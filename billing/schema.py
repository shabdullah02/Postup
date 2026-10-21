from django.conf import settings
from drf_spectacular.extensions import OpenApiAuthenticationExtension


class SessionCookieAuthenticationScheme(OpenApiAuthenticationExtension):
    target_class = "billing.authentication.SubscriptionSessionAuthentication"
    name = "sessionCookie"

    def get_security_definition(self, auto_schema):
        return {
            "type": "apiKey",
            "in": "cookie",
            "name": settings.SESSION_COOKIE_NAME,
        }