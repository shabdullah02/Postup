from rest_framework.throttling import UserRateThrottle


class AIGenerateThrottle(UserRateThrottle):
    scope = "ai"
