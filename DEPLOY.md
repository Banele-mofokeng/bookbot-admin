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
   ADMIN_TOKEN=<long-random-secret>          # REQUIRED — guards every /admin/* route
   ALLOWED_ORIGINS=https://your-admin-url.easypanel.host   # CORS allow-list (defaults to *)
   TZ=Africa/Johannesburg
   ```
   - `ADMIN_TOKEN` **must be set** or all admin endpoints return `503`. Generate one e.g. `openssl rand -hex 32`.
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

## Admin authentication

Every `/admin/*` route now requires an `x-admin-token` header matching the
server's `ADMIN_TOKEN`. The React dashboard shows a **login screen** on first
load: paste the same `ADMIN_TOKEN` value. It is stored in the browser's
`localStorage` (never baked into the build) and sent on every request. Use
**Sign out** in the sidebar to clear it.

Defense in depth: still deploy the admin UI on a private/separate domain. The
token is a shared secret — anyone who has it has full admin access.

`/health` stays public for uptime checks. `/webhook` stays public for Evolution.

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

