# Aryan Cloud — Flask SaaS starter

A Render-deployable subscription dashboard based on the uploaded VPS-HOST-WEB project concept. Includes registration/login, admin mode in the same dashboard route, plans, payment orders, signed FamGateway webhooks, server-side payment verification, upload management, and a guarded runner adapter.

## Important security boundary: Python execution

Render's ordinary Flask web service is **not** a safe sandbox for arbitrary customer Python. This project deliberately does not run uploaded code inside the website process or on the same host. `Run` forwards the file to a separately deployed isolated runner (`RUNNER_URL` + `RUNNER_TOKEN`, configured in Admin). Do not connect a runner unless it enforces per-job containers/VMs, CPU/RAM/PID/time/disk quotas, non-root execution, read-only base filesystem, no host mounts, no Docker socket, and default-deny network egress. A generic Render background worker by itself is not a security boundary. Build/operate that isolated runner separately before enabling public execution.

The current upload/run flow is intentionally disabled until the runner is configured. This is safer than claiming that subprocess execution on the Flask dyno is secure.

## Local setup

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env: SECRET_KEY, ADMIN_EMAIL, DATABASE_URL (or leave DATABASE_URL unset for local SQLite)
python app.py
```

Open `http://127.0.0.1:5000/register`. Register using the exact `ADMIN_EMAIL` address to receive admin rights. The first admin is not hardcoded.

## Render deploy

1. Upload this project to a new private GitHub repository. Do not upload `.env`.
2. In Render, create a Blueprint from that repository; Render reads `render.yaml`.
3. Set `ADMIN_EMAIL` in the Render service environment to the email you will register with.
4. Wait for deployment, open the service URL and register that email.
5. Attach the Render Postgres database. The Blueprint provisions one and wires `DATABASE_URL`.
6. The persistent disk stores uploads at `/var/data/uploads`. Keep it for uploaded files. Postgres stores users/payments/subscriptions/plans.
7. Configure custom domain and HTTPS in Render if needed.

Build command: `pip install -r requirements.txt`
Start command: `gunicorn app:app --workers 2 --threads 4 --timeout 120`

## FamGateway setup

1. Create/activate your merchant API key at https://famgateway.in.
2. Login as admin, open `/admin`, set Base URL to `https://famgateway.in`, and paste the API key.
3. Ensure the deployed public webhook URL is reachable at `https://YOUR-DOMAIN/api/fam-webhook`.
4. The app creates orders with `POST /api/create-order`, sends `X-Api-Key`, and uses `checkout_url`.
5. Webhooks are verified using HMAC-SHA256 over the raw body with the FamGateway API key; the app additionally calls `/api/verify-order.php` server-to-server before activating a subscription.
6. Test with a low-value plan first. Confirm webhook delivery, exact amount match, UTR storage and subscription activation before announcing payments.

Never put API keys in HTML, JavaScript, Git, screenshots, or query-string URLs. Rotate any credentials that were present in the old ZIP; they must be treated as compromised.

## Admin panel

Admin mode is part of the same website, at `/admin`. Set `ADMIN_EMAIL` before first registration. Admin can add plans, toggle plans/users, set FamGateway URL/key and isolated runner endpoint/token. Secret values are server-side only. Do not make public users admins.

## Plans included by default

Starter ₹49, Pro ₹99, Premium ₹199, Ultra ₹399 per 30 days. These are sample prices and can be changed by adding plans in Admin. Plan editing is intentionally limited to add/toggle in this starter; database-backed edit/delete UI should be added before a full commercial launch.

## Current implemented features

1. Registration
2. Login/logout
3. Password hashing
4. Admin role via configured email
5. Shared user/admin website
6. Dashboard
7. Subscription plans
8. FamGateway dynamic API URL/key
9. Dynamic payment order creation
10. Signed payment webhook
11. Server-side order verification
12. Amount matching
13. Idempotent payment fulfillment
14. Subscription expiry timestamps
15. File upload
16. ZIP/Python/requirements upload validation
17. User-owned file records
18. File deletion
19. Runner API adapter
20. Health endpoint

## Future roadmap (not yet implemented)

GitHub auto-deploy, console streaming, bot process controls, stop/restart, scheduled tasks, custom domains, multi-Python runtimes, team accounts, referral rewards, coupons, affiliate payouts, email verification, password reset, audit logs, backups, resource graphs, usage metering, API tokens, notifications, invoice PDFs, SSO/social login.

## Notes

- No payment credentials are included. Configure them in Admin.
- The legacy ZIP contained hardcoded third-party credentials. Revoke/rotate them immediately; do not copy them into this project.
- This starter is not a complete audited fintech or multi-tenant code execution product. Complete security review, gateway merchant tests, runner isolation tests, rate limits, CSRF protection, email verification and legal/privacy review before accepting public customers.
