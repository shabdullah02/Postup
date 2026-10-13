# Copilot Instructions

- `FB_APP_ID`, `FB_APP_SECRET`, `FB_REDIRECT_URI`, `FB_GRAPH_VERSION`, and `FRONTEND_URL` come from environment variables.
- Never print or log tokens or secrets.
- Redirects to the frontend use `settings.FRONTEND_URL`.
- One Meta app serves all users; user tokens live only in `accounts.SocialAccount.token_encrypted`.
