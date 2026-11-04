# Billing Setup

## Manual Plans

New users receive a 14-day free trial automatically. Existing trial subscriptions receive a 14-day grace period when the lifecycle migration is applied. Expired trials and expired paid periods are blocked from plan-gated actions with HTTP 402 (`plan_limit`). Superusers bypass subscription and quota enforcement; staff users do not.

Staff can assign plans and statuses at `PATCH /api/admin/users/<id>/subscription/` or in Django admin. Current quotas are centralized in `billing/plans.py`.

## Subscription API

Authenticated routes are session-authenticated under `/api/billing/`. `BILLING_PROVIDER` defaults to `mock`; Stripe-only routes return 404 in mock mode. The frontend UI uses `NEXT_PUBLIC_BILLING_PROVIDER`, which must match the backend provider but is not a security boundary. Both providers use the same subscription, plan limits, and billing summary endpoints.

- `GET /subscription/`: plan, status, start/end and next billing timestamps, cancellation-at-period-end, trial end, and days remaining. Free-plan responses suppress the paid-period fields and report no pending cancellation.
- `GET /me/`: plan, status, usage, limits, and the same subscription lifecycle fields used by the subscription endpoint.
- `GET /payment-method/`: returns `null` for a Free plan, or only the saved card's brand, last four digits, and expiration month/year for a paid plan.
- `GET /invoices/`: returns Stripe invoices, or `{"results":[]}` in mock mode or when no Stripe customer is linked.
- `POST /cancel/` and `/cancel-at-period-end/`: staff-only routes that mark the current paid subscription to end at its period end. In Stripe mode they ask Stripe to do so and wait for the webhook before updating local state.
- `POST /mock-subscribe/` (mock mode only): requires a verified email and accepts `plan` (`pro` or `agency`), `card_number`, `exp_month`, `exp_year`, and `cvv`. Card number spaces and dashes are ignored. Valid cards are accepted except `4000 0000 0000 0002`, which is declined. Card data is validated in the backend and is never persisted or logged. Only nullable `card_brand`, `card_last4`, `card_exp_month`, and `card_exp_year` are saved.
- `/setup-intent/`, `/subscribe/`, and `/webhook/` are Stripe-only and return 404 in mock mode. Stripe SetupIntent and invoice handling remain available by selecting the Stripe provider.
- `/resume/`, `/change-plan/`, `/checkout/`, and `/portal/` are Stripe-only and return 404 in mock mode. Resume also requires a paid subscription already marked to end at period end.

## Stripe Configuration

To re-enable Stripe, set `BILLING_PROVIDER=stripe` and configure the optional Stripe keys:

1. In Stripe test mode, create a recurring Pro price.
2. Set `STRIPE_SECRET_KEY`, `STRIPE_PUBLISHABLE_KEY`, `STRIPE_WEBHOOK_SECRET`, and `STRIPE_PRICE_ID_PRO` in `.env`.
3. Start the local webhook forwarder:

   ```sh
   stripe listen --forward-to localhost:8000/api/billing/webhook/
   ```

4. Copy the signing secret printed by the CLI into `STRIPE_WEBHOOK_SECRET` and restart `runserver`.
5. Subscribe the webhook endpoint to `setup_intent.succeeded`, `customer.subscription.created`, `customer.subscription.updated`, `customer.subscription.deleted`, `customer.subscription.trial_will_end`, `invoice.payment_failed`, `invoice.paid`, and `invoice.payment_succeeded`.
6. To collect a card, call `POST /api/billing/setup-intent/` and confirm the SetupIntent in Stripe.js. Then pass its returned `setup_intent_id` to `POST /api/billing/subscribe/`. This lets the backend verify and set the confirmed card immediately, without depending on webhook delivery timing. If subscribe returns a PaymentIntent `client_secret`, confirm it with Stripe.js to complete any required 3D Secure step.
7. `STRIPE_AGENCY_PRICE_ID` remains available for the existing agency checkout and plan-change routes. The card SetupIntent flow currently subscribes to Pro only.

The existing `/checkout/` and `/portal/` routes continue to use the same Stripe Customer and Subscription model. Use test-mode keys for local payment testing.

## Admin API

The existing staff-protected user endpoints retain their current response contracts. `GET /api/admin/users/<id>/` additionally reports `email_verified`, `totp_enabled`, `trial_ends_at`, and `cancel_at_period_end`.

- `POST /api/admin/users/<id>/reset-2fa/`: clears the user's TOTP secret and recovery codes and revokes their sessions.
- `POST /api/admin/users/<id>/verify-email/`: marks the user's email verified.
- `GET /api/admin/audit-log/`: paginated audit history (`page` and `page_size`).

These actions require staff permission, reject protected superuser targets, and are audit logged.

## Renewal Reminders

Run `python manage.py expire_subscriptions` daily to send reminders seven and one days before a period end. The command does not mutate subscription state; Stripe webhooks synchronize paid plan and status changes. Expired periods remain blocked by the plan entitlement checks even if a cancellation webhook is delayed.

Run `python manage.py clean_free_subscriptions` once to clear stale cancellation flags and billing-period dates from Free subscriptions.