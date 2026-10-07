import sys
from types import ModuleType
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient


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
