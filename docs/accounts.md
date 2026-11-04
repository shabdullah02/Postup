# Account Security and Email

## Email Configuration

Email is sent through Django's `EMAIL_BACKEND`. In `DEBUG`, messages use the console backend. Otherwise configure SMTP using:

- `EMAIL_HOST`
- `EMAIL_PORT` (defaults to `587`)
- `EMAIL_HOST_USER`
- `EMAIL_HOST_PASSWORD`
- `EMAIL_USE_TLS` (defaults to `true`)
- `DEFAULT_FROM_EMAIL`

The values are read from the environment. Keep SMTP credentials and `TOKEN_ENCRYPTION_KEY` out of source control. Registration sends an email-verification message. Verification links are signed and expire after 24 hours. An email change clears verification and sends a new link. Unverified users can log in, but cannot connect/reconnect social accounts or schedule posts. Superusers are exempt from that gate.

## Profile and Password Endpoints

All authenticated account-management requests use the existing session authentication and CSRF rules.

- `GET /api/auth/profile/`: profile fields and `email_verified`.
- `PATCH /api/auth/profile/`: updates `username`, `email`, `first_name`, and/or `last_name`. Usernames are unique; email uniqueness is case-insensitive. Changing email requires verification again.
- `POST /api/auth/change-password/` with `current_password` and `new_password`: applies Django password validators, retains the current session, revokes other sessions, and sends a notification.
- `POST /api/auth/delete-account/` with `password` and `confirm` equal to the username: cancels the Stripe subscription, disconnects social accounts, removes sessions, records an audit event, and deletes the account. Superusers cannot self-delete.
- `POST /api/auth/verify-email/` with `token`: verifies the signed email token.
- `POST /api/auth/resend-verification/`: resends a verification message; throttled to three requests per hour.
- `POST /api/auth/password-reset/` with `email`: always returns `{"ok":true}` to prevent account enumeration; throttled to five requests per hour.
- `POST /api/auth/password-reset/confirm/` with `uid`, `token`, and `new_password`: validates Django's password-reset token and password policy, changes the password, and revokes active sessions. Also throttled to five requests per hour.

## Two-Factor Authentication

TOTP secrets are encrypted using the configured Fernet key. Recovery codes are stored as password hashes and only returned in plaintext once.

- `POST /api/auth/2fa/setup/`: returns the provisioning URI and secret. Scan/configure the authenticator, then confirm with the enable endpoint. Setup does not enable enforcement.
- `POST /api/auth/2fa/enable/` with `code`: verifies the current TOTP, enables 2FA, and returns ten one-time recovery codes.
- `POST /api/auth/2fa/disable/` with `password` and `code`: disables 2FA and invalidates stored recovery codes.
- `POST /api/auth/2fa/recovery-codes/regenerate/` with `password` and `code`: replaces all recovery codes and returns the new set once.
- `POST /api/auth/login/` remains the normal login endpoint. For an enabled non-superuser it returns `{"two_factor_required":true}` without authenticating the session.
- `POST /api/auth/login/2fa/` with either `code` or `recovery_code`: completes that pending login. The pending challenge expires after five minutes. Five invalid codes per user/IP are allowed per 15-minute window. Recovery codes can be used once.

Superusers are never required to enable or complete 2FA. Their `is_staff` status does not grant this exemption.

## Sessions and Login Protection

- `GET /api/auth/sessions/`: lists non-expired sessions for the current user, including a `current` flag.
- `DELETE /api/auth/sessions/<key>/`: revokes one of the current user's sessions; other users' session keys return 404.
- `POST /api/auth/sessions/revoke-others/`: revokes every other active session and keeps the current one.

Login attempts are throttled to ten per minute per IP. Five failed password attempts for the same username/IP lock that pair for 15 minutes. TOTP login attempts use a separate user/IP limit.

## Operations and Cron

Run these management commands from the project root using the configured Python environment:

- `python manage.py publish_due`: publish due scheduled posts; run every five minutes.
- `python manage.py sync_posts`: synchronize published posts with the provider; run every six hours.
- `python manage.py check_tokens`: check provider credentials; run daily.
- `python manage.py expire_subscriptions`: expire ended subscriptions and send seven-day/one-day reminders; run daily.

Example cron schedule (adjust the Python path and project directory for deployment):

```cron
*/5 * * * * cd /srv/postup && /srv/postup/.venv/bin/python manage.py publish_due
0 */6 * * * cd /srv/postup && /srv/postup/.venv/bin/python manage.py sync_posts
0 9 * * * cd /srv/postup && /srv/postup/.venv/bin/python manage.py check_tokens
5 0 * * * cd /srv/postup && /srv/postup/.venv/bin/python manage.py expire_subscriptions
```

Configure the host timezone to match the deployment's intended schedule. The application timezone is configured by `TIME_ZONE` in Django settings.