from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import SocialAccount
from accounts.utils.crypto import encrypt_token
from posts.models import Post
from socials.adapters import AuthExpiredError, FacebookPublishResult


class FakeAdapter:
    def __init__(self, result="external-id", error=None):
        self.result = result
        self.error = error
        self.calls = []

    def post(self, account, content):
        self.calls.append((account, content))
        if self.error:
            raise self.error
        return self.result


class APIEndpointTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.alice = User.objects.create_user(username="alice", password="password")
        cls.bob = User.objects.create_user(username="bob", password="password")

        cls.alice_account = SocialAccount.objects.create(
            user=cls.alice,
            provider="facebook",
            external_id="alice-external-id",
            name="Alice",
            token_encrypted=encrypt_token("alice-token"),
        )
        cls.bob_account = SocialAccount.objects.create(
            user=cls.bob,
            provider="facebook",
            external_id="bob-external-id",
            name="Bob",
            token_encrypted=encrypt_token("bob-token"),
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

    def test_posts_list_returns_more_than_default_page_size(self):
        self.client.force_authenticate(user=self.alice)
        Post.objects.bulk_create([
            Post(user=self.alice, account=self.alice_account, content=f"Post {number}")
            for number in range(25)
        ])

        response = self.client.get("/api/posts/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 26)

    def test_posts_list_exposes_saved_permalink_url(self):
        self.client.force_authenticate(user=self.alice)
        self.alice_post.permalink_url = "https://www.facebook.com/posts/alice-post"
        self.alice_post.save(update_fields=["permalink_url"])

        response = self.client.get("/api/posts/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.data[0]["permalink_url"],
            self.alice_post.permalink_url,
        )

    def test_bob_get_alice_post_returns_404(self):
        self.client.force_authenticate(user=self.bob)

        response = self.client.get(f"/api/posts/{self.alice_post.pk}/")

        self.assertEqual(response.status_code, 404)

    def test_bob_schedule_alice_post_returns_404(self):
        self.client.force_authenticate(user=self.bob)

        response = self.client.post(
            f"/api/posts/{self.alice_post.pk}/schedule/",
            {"scheduled_at": (timezone.now() + timezone.timedelta(days=1)).isoformat()},
            format="json",
        )

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

        response = self.client.get("/api/social-accounts/")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("token", response.data[0])

    def test_bob_social_account_list_does_not_include_alices_account(self):
        self.client.force_authenticate(user=self.bob)

        response = self.client.get("/api/social-accounts/")

        self.assertEqual(response.status_code, 200)
        returned_ids = {account["id"] for account in response.data}
        self.assertEqual(returned_ids, {self.bob_account.pk})
        self.assertNotIn(self.alice_account.pk, returned_ids)

    def test_social_accounts_list_returns_more_than_default_page_size(self):
        self.client.force_authenticate(user=self.alice)
        SocialAccount.objects.bulk_create([
            SocialAccount(
                user=self.alice,
                provider="facebook",
                external_id=f"alice-page-{number}",
                name=f"Alice Page {number}",
                token_encrypted=b"encrypted",
            )
            for number in range(25)
        ])

        response = self.client.get("/api/social-accounts/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 26)

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

    def test_publish_now_publishes_draft_and_returns_result(self):
        self.client.force_authenticate(user=self.alice)
        adapter = FakeAdapter(result="immediate-post-id")

        with patch.dict("socials.adapters.ADAPTERS", {"facebook": adapter}):
            response = self.client.post(f"/api/posts/{self.alice_post.pk}/publish_now/")

        self.alice_post.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], Post.Status.POSTED)
        self.assertEqual(self.alice_post.status, Post.Status.POSTED)
        self.assertEqual(self.alice_post.external_post_id, "immediate-post-id")
        self.assertIsNotNone(self.alice_post.posted_at)
        self.assertEqual(len(adapter.calls), 1)

    def test_publish_now_publishes_scheduled_post(self):
        self.client.force_authenticate(user=self.alice)
        scheduled_post = Post.objects.create(
            user=self.alice,
            account=self.alice_account,
            content="Scheduled immediately",
            status=Post.Status.SCHEDULED,
            scheduled_at=timezone.now() + timezone.timedelta(days=1),
        )
        adapter = FakeAdapter()

        with patch.dict("socials.adapters.ADAPTERS", {"facebook": adapter}):
            response = self.client.post(
                f"/api/posts/{scheduled_post.pk}/publish_now/"
            )

        scheduled_post.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(scheduled_post.status, Post.Status.POSTED)
        self.assertEqual(scheduled_post.external_post_id, "external-id")

    def test_publish_now_saves_permalink_result(self):
        self.client.force_authenticate(user=self.alice)
        adapter = FakeAdapter(
            result=FacebookPublishResult(
                "permalink-post-id", "https://www.facebook.com/posts/permalink-post-id"
            )
        )

        with patch.dict("socials.adapters.ADAPTERS", {"facebook": adapter}):
            response = self.client.post(
                f"/api/posts/{self.alice_post.pk}/publish_now/"
            )

        self.alice_post.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.alice_post.permalink_url,
            "https://www.facebook.com/posts/permalink-post-id",
        )
        self.assertEqual(response.data["permalink_url"], self.alice_post.permalink_url)

    def test_publish_now_adapter_failure_returns_failed_post(self):
        self.client.force_authenticate(user=self.alice)
        adapter = FakeAdapter(error=RuntimeError("Adapter failed"))

        with patch.dict("socials.adapters.ADAPTERS", {"facebook": adapter}):
            response = self.client.post(
                f"/api/posts/{self.alice_post.pk}/publish_now/"
            )

        self.alice_post.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], Post.Status.FAILED)
        self.assertEqual(self.alice_post.status, Post.Status.FAILED)
        self.assertEqual(self.alice_post.error, "Adapter failed")

    def test_publish_now_auth_expired_returns_reauth_error(self):
        self.client.force_authenticate(user=self.alice)
        adapter = FakeAdapter(error=AuthExpiredError("Expired"))

        with patch.dict("socials.adapters.ADAPTERS", {"facebook": adapter}):
            response = self.client.post(
                f"/api/posts/{self.alice_post.pk}/publish_now/"
            )

        self.alice_post.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.alice_post.status, Post.Status.FAILED)
        self.assertEqual(self.alice_post.error, "failed: reauth required")

    def test_publish_now_skips_account_needing_reauth(self):
        self.client.force_authenticate(user=self.alice)
        self.alice_account.status = SocialAccount.Status.NEEDS_REAUTH
        self.alice_account.save(update_fields=["status"])
        adapter = FakeAdapter()

        with patch.dict("socials.adapters.ADAPTERS", {"facebook": adapter}):
            response = self.client.post(
                f"/api/posts/{self.alice_post.pk}/publish_now/"
            )

        self.alice_post.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.alice_post.status, Post.Status.FAILED)
        self.assertEqual(self.alice_post.error, "failed: reauth required")
        self.assertEqual(adapter.calls, [])

    @override_settings(INSTAGRAM_ENABLED=False)
    def test_publish_now_instagram_disabled_fails_without_requests(self):
        self.client.force_authenticate(user=self.alice)
        instagram_account = SocialAccount.objects.create(
            user=self.alice,
            provider=SocialAccount.Provider.INSTAGRAM,
            external_id="alice-instagram-id",
            name="Alice Instagram",
            token_encrypted=encrypt_token("instagram-secret"),
        )
        instagram_post = Post.objects.create(
            user=self.alice,
            account=instagram_account,
            content="Instagram post",
        )

        with (
            patch("accounts.providers.instagram.requests.get") as mock_get,
            patch("accounts.providers.instagram.requests.post") as mock_post,
        ):
            response = self.client.post(
                f"/api/posts/{instagram_post.pk}/publish_now/"
            )

        instagram_post.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(instagram_post.status, Post.Status.FAILED)
        self.assertEqual(instagram_post.error, "failed: instagram disabled")
        mock_get.assert_not_called()
        mock_post.assert_not_called()

    def test_publish_now_rejects_non_publishable_statuses(self):
        self.client.force_authenticate(user=self.alice)
        adapter = FakeAdapter()
        for status in (Post.Status.POSTED, Post.Status.FAILED, Post.Status.PUBLISHING):
            with self.subTest(status=status):
                post = Post.objects.create(
                    user=self.alice,
                    account=self.alice_account,
                    content=f"{status} post",
                    status=status,
                )
                with patch.dict("socials.adapters.ADAPTERS", {"facebook": adapter}):
                    response = self.client.post(
                        f"/api/posts/{post.pk}/publish_now/"
                    )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(
                    response.data["detail"],
                    "Only draft or scheduled posts can be published.",
                )
        self.assertEqual(adapter.calls, [])

    def test_publish_now_hides_other_users_posts_and_requires_authentication(self):
        self.client.force_authenticate(user=self.bob)
        response = self.client.post(
            f"/api/posts/{self.alice_post.pk}/publish_now/"
        )
        self.assertEqual(response.status_code, 404)

        self.client.force_authenticate(user=None)
        response = self.client.post(
            f"/api/posts/{self.alice_post.pk}/publish_now/"
        )
        self.assertEqual(response.status_code, 403)

    def test_publish_now_response_does_not_include_tokens(self):
        self.client.force_authenticate(user=self.alice)
        adapter = FakeAdapter()

        with patch.dict("socials.adapters.ADAPTERS", {"facebook": adapter}):
            response = self.client.post(
                f"/api/posts/{self.alice_post.pk}/publish_now/"
            )

        response_body = response.content.decode()
        self.assertNotIn("alice-token", response_body)
        self.assertNotIn("token_encrypted", response_body)

    def test_publish_now_second_call_does_not_publish_again(self):
        self.client.force_authenticate(user=self.alice)
        adapter = FakeAdapter()

        with patch.dict("socials.adapters.ADAPTERS", {"facebook": adapter}):
            first_response = self.client.post(
                f"/api/posts/{self.alice_post.pk}/publish_now/"
            )
            second_response = self.client.post(
                f"/api/posts/{self.alice_post.pk}/publish_now/"
            )

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 400)
        self.assertEqual(len(adapter.calls), 1)
