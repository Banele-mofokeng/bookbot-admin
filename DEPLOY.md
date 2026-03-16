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
   ```
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

## Locking down the admin (recommended)

Since there's no login, restrict `/admin/*` routes via EasyPanel:
- Go to your bot service → **Domains** → add a separate internal domain for admin
- Or add a simple API key middleware to `main.py` when you're ready

