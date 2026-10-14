class InstagramProvider:
    """Scaffold only: Facebook Login -> /me/accounts -> each Page's
    instagram_business_account field -> POST /{ig_user_id}/media followed by
    POST /{ig_user_id}/media_publish.
    """

    def auth_scopes(self):
        return [
            "instagram_basic",
            "instagram_content_publish",
            "pages_show_list",
            "pages_read_engagement",
        ]

    def list_instagram_accounts(self, user_token):
        raise NotImplementedError("Instagram is not enabled yet")

    def publish(self, account, content):
        raise NotImplementedError("Instagram is not enabled yet")
