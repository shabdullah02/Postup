import sys
from types import ModuleType
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from socials.models import SocialAccount


class FacebookOAuthAPITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="alice",
            password="password",
        )

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    @patch("socials.views.requests.post")
    def test_facebook_oauth_callback_links_a_page_account(self, mock_post):
        connect_response = self.client.get("/api/facebook/connect/")
        auth_url = connect_response.data["auth_url"]
        state = parse_qs(urlsplit(auth_url).query)["state"][0]

        mock_post.return_value.json.return_value = {
            "access_token": "page-access-token",
            "token_type": "bearer",
        }

        with patch("socials.views.requests.get") as mock_get:
            mock_get.return_value.json.return_value = {
                "data": [
                    {
                        "id": "page-123",
                        "name": "Alice Brand",
                        "access_token": "page-token-value",
                        "category": "Business",
                        "tasks": ["ANALYZE", "MODERATE", "MANAGE"],
                    }
                ]
            }
            response = self.client.get(
                "/api/facebook/callback/",
                {"code": "facebook-code-123", "state": state},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["platform"], "facebook")
        self.assertEqual(response.data["external_id"], "page-123")
        self.assertEqual(response.data["name"], "Alice Brand")
        self.assertNotIn("page-token-value", response.content.decode())
        self.assertTrue(
            SocialAccount.objects.filter(
                user=self.user,
                platform="facebook",
                external_id="page-123",
            ).exists()
        )

        saved = SocialAccount.objects.get(user=self.user, external_id="page-123")
        self.assertNotEqual(saved.token, "page-token-value")
        self.assertEqual(saved.get_decrypted_token(), "page-token-value")

    def test_token_is_encrypted_before_save(self):
        account = SocialAccount.objects.create(
            user=self.user,
            platform="facebook",
            external_id="page-456",
            name="Acme",
            token="plain-text-token",
        )

        self.assertNotEqual(account.token, "plain-text-token")
        self.assertEqual(account.get_decrypted_token(), "plain-text-token")


class AIGenerateAPITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="alice",
            password="password",
        )

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_twenty_first_request_in_hour_returns_429(self):
        fake_module = ModuleType("article_publisher")
        fake_module.exceptions = type("Exceptions", (), {})
        fake_module.generate_article = Mock(return_value="generated content")

        with patch.dict(sys.modules, {"article_publisher": fake_module}):
            for _ in range(20):
                response = self.client.post(
                    "/api/ai/generate/",
                    {"topic": "test topic"},
                    format="json",
                )
                self.assertNotEqual(response.status_code, 429)

            response = self.client.post(
                "/api/ai/generate/",
                {"topic": "test topic"},
                format="json",
            )

        self.assertEqual(response.status_code, 429)

    def test_failing_generate_article_returns_generic_message_without_exception_text(self):
        fake_module = ModuleType("article_publisher")

        class GenerationError(Exception):
            pass

        fake_module.exceptions = type("Exceptions", (), {"GenerationError": GenerationError})
        fake_module.generate_article = Mock(
            side_effect=GenerationError("Secret error details")
        )

        with patch.dict(sys.modules, {"article_publisher": fake_module}):
            response = self.client.post(
                "/api/ai/generate/",
                {"topic": "test topic"},
                format="json",
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.data["detail"],
            "Generation failed. Try again later.",
        )
        self.assertNotIn("Secret error details", response.content.decode())
