import sys
from types import ModuleType
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import SocialAccount
from accounts.utils.crypto import decrypt_token, encrypt_token


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

    def test_facebook_oauth_callback_links_a_page_account(self):
        connect_response = self.client.get("/api/auth/connect/facebook/")
        auth_url = connect_response["Location"]
        state = parse_qs(urlsplit(auth_url).query)["state"][0]

        short_token_response = Mock(ok=True)
        short_token_response.json.return_value = {"access_token": "short-token"}
        long_token_response = Mock(ok=True)
        long_token_response.json.return_value = {"access_token": "long-token"}
        pages_response = Mock(ok=True)
        pages_response.json.return_value = {
            "data": [{"id": "page-123", "name": "Alice Brand", "access_token": "page-token-value"}]
        }

        with patch(
            "accounts.views.requests.get",
            side_effect=[short_token_response, long_token_response, pages_response],
        ):
            response = self.client.get(
                "/api/auth/callback/facebook/",
                {"code": "facebook-code-123", "state": state},
            )

        self.assertEqual(response.status_code, 302)
        self.assertNotIn("page-token-value", response.content.decode())
        selection_response = self.client.post(
            "/api/auth/connect/facebook/select/",
            {"page_ids": ["page-123"]},
            format="json",
        )

        self.assertEqual(selection_response.status_code, 200)
        self.assertEqual(selection_response.data[0]["name"], "Alice Brand")
        self.assertTrue(
            SocialAccount.objects.filter(
                user=self.user,
                provider="facebook",
                external_id="page-123",
            ).exists()
        )

        saved = SocialAccount.objects.get(user=self.user, external_id="page-123")
        self.assertNotEqual(bytes(saved.token_encrypted), b"page-token-value")
        self.assertEqual(decrypt_token(saved.token_encrypted), "page-token-value")

    def test_token_is_encrypted_before_save(self):
        account = SocialAccount.objects.create(
            user=self.user,
            provider="facebook",
            external_id="page-456",
            name="Acme",
            token_encrypted=encrypt_token("plain-text-token"),
        )

        self.assertNotEqual(bytes(account.token_encrypted), b"plain-text-token")
        self.assertEqual(decrypt_token(account.token_encrypted), "plain-text-token")


class AIGenerateAPITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="alice",
            password="password",
        )

    def setUp(self):
        cache.clear()
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
