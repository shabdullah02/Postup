# Billing Setup

## Manual Plans

New users receive the free trial plan automatically. Staff can assign plans and statuses at `PATCH /api/admin/users/<id>/subscription/` or in Django admin. Current quotas are centralized in `billing/plans.py`.

## Stripe Test Mode

1. In Stripe test mode, create recurring prices for the Pro and Agency plans.
2. Set `STRIPE_SECRET_KEY`, `STRIPE_PRO_PRICE_ID`, and `STRIPE_AGENCY_PRICE_ID` in the environment.
3. Start the local webhook forwarder:

   ```sh
   stripe listen --forward-to localhost:8000/api/billing/webhook/
   ```

4. Copy the signing secret printed by the CLI into `STRIPE_WEBHOOK_SECRET` and restart `runserver`.
5. Subscribe the webhook endpoint to `customer.subscription.created`, `customer.subscription.updated`, `customer.subscription.deleted`, `invoice.payment_failed`, `invoice.paid`, and `invoice.payment_succeeded`.
6. Use `POST /api/billing/checkout/` with `{"plan":"pro"}` or `{"plan":"agency"}` to create a Checkout session. Use `POST /api/billing/portal/` for the customer portal.

Webhook event IDs are stored so Stripe retries do not apply an event twice. All Stripe calls must use test-mode keys during local development.