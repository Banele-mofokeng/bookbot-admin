# BookBot Admin — Deployment Guide

## What's in this folder

| File | Purpose |
|------|---------|
| `main.py` | FastAPI bot — webhook + admin API |
| `requirements.txt` | Python dependencies |
| `Dockerfile.bot` | Docker image for the bot |
| `src/` | React admin frontend source |
| `Dockerfile.frontend` | Docker image for the admin UI |
| `nginx.conf` | SPA routing config for nginx |

---

## EasyPanel Setup — Two Services

### Service 1: Bot (main.py)

1. Create a new **App** service called `whatsapp-bot`
2. Set **Build**: Dockerfile → `Dockerfile.bot`
3. Set **Port**: `9000`
4. Add environment variables:
   ```
   DATABASE_URL=postgres://postgres:<password>@<project>_booking-db:5432/whatsapp_bot
   REDIS_URL=redis://default:<password>@<project>_evolution-api-redis:6379
   JWT_SECRET=<long-random-secret>           # REQUIRED — signs login tokens
   SUPERADMIN_EMAIL=you@example.com          # your platform-operator login (seeded on boot)
   SUPERADMIN_PASSWORD=<strong-password>     # change after first login
   ALLOWED_ORIGINS=https://your-admin-url.easypanel.host   # CORS allow-list (defaults to *)
   TZ=Africa/Johannesburg
   ```
   - `JWT_SECRET` **must be set** or auth returns `503`. Generate one e.g. `openssl rand -hex 32`.
   - `SUPERADMIN_EMAIL`/`SUPERADMIN_PASSWORD` seed your super-admin account on startup
     (only if it doesn't already exist). Without them, no one can log in.
   - `ALLOWED_ORIGINS` is comma-separated; set it to your admin URL in production instead of `*`.
5. Deploy

6. Once live, hit `POST https://your-bot-url/admin/seed` to insert your first tenant.

---

### Service 2: Admin Frontend (React)

1. Create a new **App** service called `bookbot-admin`
2. Set **Build**: Dockerfile → `Dockerfile.frontend`
3. Set **Build Arg**:
   ```
   VITE_API_URL=https://your-bot-url.easypanel.host
   ```
4. Set **Port**: `80`
5. Deploy

6. Open `https://your-admin-url.easypanel.host` — you should see the dashboard.

---

## Adding a new business tenant

1. In Evolution API Manager → create a new instance → scan QR → set webhook to `https://your-bot-url/webhook`
2. In BookBot Admin → **Add Business** → fill in the form → Register
3. Done — that business's customers can now book via WhatsApp.

---

## Authentication & multi-tenant access

Email + password logins, scoped per business:

- **Super-admin (you):** seeded from `SUPERADMIN_EMAIL`/`SUPERADMIN_PASSWORD`.
  Sees every business, creates businesses, and provisions client logins.
- **Tenant user (each client):** only sees and manages their **own** business —
  queue, services, agents. Cannot see other tenants or the Businesses page.

Login flow: dashboard shows an email/password screen → `POST /auth/login`
returns a JWT, stored in `localStorage` (never baked into the build) and sent as
`Authorization: Bearer …` on every request. **Sign out** clears it. Tokens
expire after `JWT_EXP_HOURS` (default 12).

### Onboarding a client (model B)
1. **Businesses → + Add Business** — register the tenant + its Evolution config.
2. On that business row, **Add login** — set the client's email + password
   (8+ chars). Send them the credentials.
3. They log in and see only their own queue. Reset/deactivate via the API
   (`PATCH /admin/users/{id}`).

Every `/admin/*` route is authenticated and tenant-scoped server-side, so the
isolation holds even if the UI is bypassed. `/health` and `/webhook` stay public.

---

## Testing

```
pip install -r requirements-dev.txt
pytest                       # runs unit + sqlite-backed integration tests
```

No Postgres/Redis needed for the test suite (uses a temp SQLite DB; health-check
redis errors are tolerated).

---

## Known limitations (not yet hardened)

- **WhatsApp send is synchronous** (`requests`) inside the async webhook, so
  concurrent webhooks serialise. Fine at small-business volume; move to a
  background task / async client if throughput grows.
- **No per-customer concurrency lock.** Duplicate *retries* are dropped via
  message-id idempotency, but two genuinely simultaneous messages from one
  customer are not serialised.
- **Legacy data:** family bookings created before the party-linkage fix may not
  fully cancel as one. New bookings are fine.

