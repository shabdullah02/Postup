from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from posts.models import Post
from socials.models import SocialAccount


class APIEndpointTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.alice = User.objects.create_user(username="alice", password="password")
        cls.bob = User.objects.create_user(username="bob", password="password")

        cls.alice_account = SocialAccount.objects.create(
            user=cls.alice,
            platform="facebook",
            external_id="alice-external-id",
            name="Alice",
            token="alice-token",
        )
        cls.bob_account = SocialAccount.objects.create(
            user=cls.bob,
            platform="facebook",
            external_id="bob-external-id",
            name="Bob",
            token="bob-token",
        )
        cls.alice_post = Post.objects.create(
            user=cls.alice,
            account=cls.alice_account,
            content="Alice post",
        )

    def setUp(self):
        self.client = APIClient()

    def test_anonymous_get_posts_returns_403(self):
        response = self.client.get("/api/posts/")

        self.assertEqual(response.status_code, 403)

    def test_bob_get_posts_returns_empty_list(self):
        self.client.force_authenticate(user=self.bob)

        response = self.client.get("/api/posts/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, [])

    def test_bob_get_alice_post_returns_404(self):
        self.client.force_authenticate(user=self.bob)

        response = self.client.get(f"/api/posts/{self.alice_post.pk}/")

        self.assertEqual(response.status_code, 404)

    def test_bob_patch_and_delete_alice_post_return_404(self):
        self.client.force_authenticate(user=self.bob)

        patch_response = self.client.patch(
            f"/api/posts/{self.alice_post.pk}/",
            {"content": "Bob patch"},
            format="json",
        )
        delete_response = self.client.delete(f"/api/posts/{self.alice_post.pk}/")

        self.assertEqual(patch_response.status_code, 404)
        self.assertEqual(delete_response.status_code, 404)

    def test_alice_post_using_bobs_account_returns_400(self):
        self.client.force_authenticate(user=self.alice)

        response = self.client.post(
            "/api/posts/",
            {"account": self.bob_account.pk, "content": "Alice content"},
            format="json",
        )

        self.assertEqual(response.status_code, 400)

    def test_patch_on_posted_post_returns_400(self):
        self.client.force_authenticate(user=self.alice)
        self.alice_post.status = Post.Status.POSTED
        self.alice_post.save(update_fields=["status"])

        response = self.client.patch(
            f"/api/posts/{self.alice_post.pk}/",
            {"content": "Updated content"},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.data["detail"],
            "Only draft or scheduled posts can be modified.",
        )

    def test_delete_on_failed_post_returns_400(self):
        self.client.force_authenticate(user=self.alice)
        self.alice_post.status = Post.Status.FAILED
        self.alice_post.save(update_fields=["status"])

        response = self.client.delete(f"/api/posts/{self.alice_post.pk}/")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.data["detail"],
            "Only draft or scheduled posts can be modified.",
        )

    def test_delete_on_draft_returns_204(self):
        self.client.force_authenticate(user=self.alice)

        response = self.client.delete(f"/api/posts/{self.alice_post.pk}/")

        self.assertEqual(response.status_code, 204)
        self.assertFalse(Post.objects.filter(pk=self.alice_post.pk).exists())

    def test_patch_content_on_scheduled_post_returns_200(self):
        self.client.force_authenticate(user=self.alice)
        self.alice_post.status = Post.Status.SCHEDULED
        self.alice_post.save(update_fields=["status"])

        response = self.client.patch(
            f"/api/posts/{self.alice_post.pk}/",
            {"content": "Scheduled content updated"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["content"], "Scheduled content updated")

    def test_posts_create_with_past_scheduled_at_returns_400(self):
        self.client.force_authenticate(user=self.alice)
        past_time = timezone.now() - timezone.timedelta(minutes=1)

        response = self.client.post(
            "/api/posts/",
            {
                "account": self.alice_account.pk,
                "content": "Scheduled post",
                "scheduled_at": past_time.isoformat(),
            },
            format="json",
        )

        self.assertEqual(response.status_code, 400)

    def test_posts_create_with_naive_datetime_returns_400(self):
        self.client.force_authenticate(user=self.alice)

        response = self.client.post(
            "/api/posts/",
            {
                "account": self.alice_account.pk,
                "content": "Scheduled post",
                "scheduled_at": "2026-10-07T12:00:00",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 400)

    def test_posts_create_with_future_aware_datetime_returns_201_and_status_draft(self):
        self.client.force_authenticate(user=self.alice)
        future_time = timezone.now() + timezone.timedelta(minutes=10)

        response = self.client.post(
            "/api/posts/",
            {
                "account": self.alice_account.pk,
                "content": "Future scheduled post",
                "scheduled_at": future_time.isoformat(),
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["status"], Post.Status.DRAFT)

    def test_accounts_as_alice_never_contain_token_key(self):
        self.client.force_authenticate(user=self.alice)

        response = self.client.get("/api/accounts/")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("token", response.data[0])

    def test_stale_publishing_row_is_marked_failed(self):
        stale_post = Post.objects.create(
            user=self.alice,
            account=self.alice_account,
            content="Stale post",
            status=Post.Status.PUBLISHING,
            claimed_at=timezone.now() - timezone.timedelta(minutes=11),
        )

        call_command("publish_due")

        stale_post.refresh_from_db()
        self.assertEqual(stale_post.status, Post.Status.FAILED)
        self.assertEqual(
            stale_post.error,
            "Publish interrupted; verify on the platform before retrying.",
        )

    def test_fresh_publishing_row_is_left_alone(self):
        fresh_post = Post.objects.create(
            user=self.alice,
            account=self.alice_account,
            content="Fresh post",
            status=Post.Status.PUBLISHING,
            claimed_at=timezone.now() - timezone.timedelta(minutes=9),
        )

        call_command("publish_due")

        fresh_post.refresh_from_db()
        self.assertEqual(fresh_post.status, Post.Status.PUBLISHING)

    def test_due_scheduled_post_becomes_posted_when_adapter_is_patched(self):
        due_post = Post.objects.create(
            user=self.alice,
            account=self.alice_account,
            content="Due post",
            status=Post.Status.SCHEDULED,
            scheduled_at=timezone.now() - timezone.timedelta(minutes=1),
        )

        class FakeAdapter:
            def post(self, account, content):
                return "external-id"

        with patch.dict("socials.adapters.ADAPTERS", {"facebook": FakeAdapter()}):
            call_command("publish_due")

        due_post.refresh_from_db()
        self.assertEqual(due_post.status, Post.Status.POSTED)
        self.assertEqual(due_post.external_post_id, "external-id")
        self.assertIsNotNone(due_post.posted_at)

    def test_adapter_failure_produces_failed_status_and_error_text(self):
        due_post = Post.objects.create(
            user=self.alice,
            account=self.alice_account,
            content="Failed post",
            status=Post.Status.SCHEDULED,
            scheduled_at=timezone.now() - timezone.timedelta(minutes=1),
        )

        class FakeAdapter:
            def post(self, account, content):
                raise RuntimeError("Adapter failed")

        with patch.dict("socials.adapters.ADAPTERS", {"facebook": FakeAdapter()}):
            call_command("publish_due")

        due_post.refresh_from_db()
        self.assertEqual(due_post.status, Post.Status.FAILED)
        self.assertEqual(due_post.error, "Adapter failed")
