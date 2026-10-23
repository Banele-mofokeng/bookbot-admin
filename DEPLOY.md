# BookBot Admin — Deployment Guide

## What's in this folder

| File | Purpose |
|------|---------|
| `main.py` | Composition root — builds the FastAPI app, mounts routers, runs the scheduler |
| `app/models.py` | Every database table |
| `app/queue_engine.py` | Working windows, backlogs, ETAs, agent assignment |
| `app/webhook.py` | The queue-booking WhatsApp conversation |
| `app/orders.py` | Ordering: pricing, kitchen backlog, ready-time quotes |
| `app/orders_flow.py` | The takeaway WhatsApp conversation |
| `app/appointments_flow.py` | The appointment-booking WhatsApp conversation |
| `app/messaging.py` | Outbox table, delivery worker, menu builders |
| `app/jobs.py` | Scheduled notifications and the midnight sweep |
| `app/api/` | Admin routes, one module per area |
| `tests/` | pytest suite — no Postgres or Redis needed |
| `requirements.txt` | Python dependencies |
| `Dockerfile.bot` | Docker image for the bot — copies `main.py` **and** `app/` |
| `src/` | React admin frontend source |
| `Dockerfile.frontend` | Docker image for the admin UI — bakes `VITE_API_URL` in at build time |
| `nginx.conf` | SPA routing config for nginx |
| `wrangler.jsonc` | Cloudflare Workers config — the dashboard as static assets |
| `scripts/require-api-url.mjs` | Build guard: refuses to build without `VITE_API_URL` |

Deployed on **Railway**: two services built from the Dockerfiles above, plus
Railway's Postgres and Redis. Both redeploy automatically on a push to `main`.

The admin frontend can also be deployed to **Cloudflare Workers** instead of
Railway — see [Cloudflare Setup](#cloudflare-setup). The bot stays on Railway
either way; it needs Postgres, Redis and a long-lived process, which Workers
does not provide.

---

## Running locally

Four moving parts, same as production: Postgres, Redis, the bot on `:9000`, the
dashboard on `:5173`.

**Postgres is required — SQLite is not a shortcut here.** `create_db_and_tables()`
reads `information_schema.columns` to decide which `ADDED_COLUMNS` are missing,
and SQLite has no such table, so the app dies on boot against it. The test suite
runs on SQLite only because `conftest.py` calls `create_all()` directly and never
takes that path. Run the real database locally and the schema you develop against
is the schema you deploy to.

### 1. Postgres and Redis

```bash
docker run -d --name qb-postgres -e POSTGRES_PASSWORD=password -e POSTGRES_DB=queuebot -p 5432:5432 postgres:16
docker run -d --name qb-redis -p 6379:6379 redis:7
```

Those credentials are not arbitrary — they are exactly the fallbacks in
`app/config.py`, so `DATABASE_URL` and `REDIS_URL` can stay unset locally. Set
them explicitly if you run Postgres some other way.

### 2. The bot

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
```

`psycopg2-binary` ships wheels for released Python versions only. On a very new
interpreter pip falls back to building from source and needs the PostgreSQL
client libraries; an older interpreter in the venv is the cheaper fix.

Nothing in the app reads a `.env` file — `app/config.py` calls `os.getenv`
directly, so these have to be in the shell that starts uvicorn:

```bash
export JWT_SECRET=dev-secret-not-for-prod
export SUPERADMIN_EMAIL=you@example.com
export SUPERADMIN_PASSWORD=devpassword123
uvicorn main:app --reload --port 9000
```

Without `JWT_SECRET`, auth returns `503` and nothing in the dashboard works.
Without the super-admin pair, no account is seeded and there is nobody to log in
as. `WEBHOOK_SECRET` is worth skipping locally: leaving it unset is what lets you
POST to `/webhook` by hand without a token, and startup prints the warning that
would matter in production. Startup also creates the tables, adds the columns and
builds the indexes, so the first run against an empty database is all the setup
there is.

Check `GET /health`, or read the routes at `/docs`.

### 3. The dashboard

In a second shell:

```bash
export VITE_API_URL=http://localhost:9000
npm install
npm run dev
```

**`VITE_API_URL` is not optional in dev either.** `src/api/client.js` falls back
to a same-origin base when it is missing, and it requests `/auth/login` — not
`/api/auth/login` — so the dev-server proxy in `vite.config.js`, which only
rewrites the `/api` prefix, never sees the call. It hits Vite's own origin and
404s. The bot's `ALLOWED_ORIGINS` defaults to `*`, so cross-origin calls to
`:9000` work without extra config.

Log in with the `SUPERADMIN_EMAIL` and `SUPERADMIN_PASSWORD` from step 2, then
add a business from **Businesses → + Add Business** — the same path production
takes, since there is no seed endpoint.

**Those env vars only seed an account that does not exist yet.** If the email
is already in the database, the password is left alone — changing
`SUPERADMIN_PASSWORD` and restarting does not change the password of an
account that already exists. To reset one, use `PATCH /admin/users/{id}`
with a super-admin token, or delete the row and restart.

### 4. Reading the database

```bash
docker exec -it qb-postgres psql -U postgres -d queuebot
docker exec -it qb-redis redis-cli
```

Any GUI client (DBeaver, TablePlus, pgAdmin) takes the same URL the app uses:
`postgresql://postgres:password@localhost:5432/queuebot`.

### Reading the production database

Railway dashboard → the Postgres service → **Variables** → `DATABASE_PUBLIC_URL`.
The `DATABASE_URL` next to it resolves only inside Railway's private network and
will not connect from a laptop.

```bash
psql "<DATABASE_PUBLIC_URL>"
```

That is live customer data: bookings, phone numbers, Evolution credentials on the
tenant rows. Reads are fine; treat writes as production changes.

> **Do not point a local bot at it.** Boot runs `create_db_and_tables()` and then
> `midnight_reset_job()` immediately — a local process on the production URL will
> alter the live schema and close out live queue entries, from a shell where you
> were only meaning to look around.

### Windows

The steps above are POSIX. On Windows (PowerShell or cmd) the same four parts,
with the differences that actually bite:

| | Windows |
|---|---|
| venv | `python -m venv .venv` then `.venv\Scripts\activate` (`.venv\Scripts\activate.ps1` in PowerShell) |
| env vars | `set DATABASE_URL=...` per shell, or `$env:DATABASE_URL="..."` in PowerShell. One shell only — they are not inherited by a second one |
| paths in `psql` | `docker exec -it qb-postgres psql -U postgres -d queuebot` works as-is |

The one thing that is not optional on any platform: **the env vars must be in
the shell that starts uvicorn.** Nothing in the app reads a `.env` file —
`app/config.py` calls `os.getenv` directly — so exporting them in one terminal
and starting the bot in another leaves it without a JWT secret, and auth
returns 503 with an empty dashboard.

### Windows on this machine

Python lives at `C:\Users\Banele Mofokeng\AppData\Local\Python\pythoncore-3.14-64\python.exe`
and is reachable as `py -3.14`. The project dependencies are already installed
into that interpreter, so there is no venv to create:

```bash
py -3.14 -m uvicorn main:app --port 9000
```

If you want an isolated venv anyway, `psycopg2-binary` ships wheels for
released Python versions only — on a brand-new interpreter pip falls back to
building from source and needs the PostgreSQL client libraries. Python 3.14
has a wheel, so a venv on it is fine here.

### Checking it works

```bash
curl http://localhost:9000/health
```

A healthy local bot answers:

```json
{"status":"ok","redis":true,"tenants":1,"webhook_auth":false,"outbox_pending":0,"outbox_failed":0}
```

`webhook_auth: false` is expected and correct locally — no `WEBHOOK_SECRET` is
set, so `/webhook` accepts a hand-written POST without a token. In production
this must read `true`.

`GET /health` also reports `outbox_failed`. A non-zero count means messages
were queued but Evolution was unreachable — the common local case, because a
tenant's `evolution_api_url` points at a real gateway that does not exist on
your laptop. See "Testing the WhatsApp flow" below.

---

## Testing the WhatsApp flow locally

The bot talks to Evolution over HTTP, and Evolution is the thing that delivers
WhatsApp messages. Without it the bot still runs and still writes to the
database, but every outbound message fails and lands in `outbox_failed`.

Two pieces make the conversation testable without a real WhatsApp account.

### 1. A fake Evolution API

`scripts/fake_evolution.py` serves the one route the bot calls
(`POST /message/sendText/{instance}`), logs every message it receives, and
writes them to `scripts/evolution-inbox.log` so you can read what the bot
would have sent:

```bash
py -3.14 scripts/fake_evolution.py     # listens on :9999
```

Then point a tenant at it — **Businesses → edit the business → Evolution API
URL** — with the URL `http://localhost:9999`, and give the business an
Evolution instance name. The bot reads both from the tenant row.

### 2. A script that sends inbound messages

`scripts/whatsapp.sh` posts inbound customer messages to the webhook in
Evolution's shape, one per argument, in order:

```bash
bash scripts/whatsapp.sh "hi"
bash scripts/whatsapp.sh "1" "1" "1"     # join queue → date → who for
```

The bot's replies land in `scripts/evolution-inbox.log`. The script's customer
number and tenant number can be overridden:

```bash
CUSTOMER_NUMBER=27722222222@s.whatsapp.net TENANT_NUMBER=27810000000 \
  bash scripts/whatsapp.sh "hi"
```

`WEBHOOK_SECRET` must be unset locally for this to work without a token — which
is also why startup warns about it. Set one and add
`?token=<secret>` to the webhook URL in the script.

The state machine advances one step per message, so a booking is six replies:
main menu → date → who for → service → agent → arrival time → confirmation.
The last one writes a `queueentry` row, which then appears on the dashboard's
live queue.

### The routes are under `/admin`

The API is not mounted at the root. `GET /tenants` is a 404; the real path is
`GET /admin/tenants`. The full set:

| Method | Path |
|---|---|
| POST | `/auth/login` |
| GET | `/auth/me` |
| GET | `/admin/tenants` |
| GET | `/admin/services/{tenant_id}` |
| GET | `/admin/agents/{tenant_id}` |
| GET | `/admin/queue/{tenant_id}` |

`/docs` lists them all, and `/health` and `/ping` are unauthenticated.

---

## Railway Setup

One Railway project, four services: **bot**, **admin frontend**, **Postgres**,
**Redis**. Both application services build from the Dockerfiles in this repo and
redeploy automatically on every push to `main`.

> **Deploys are automatic and unordered.** Merging to `main` rebuilds both
> services at once, and there is no guarantee the bot finishes first. A frontend
> that comes up ahead of a new endpoint shows 404s until the bot restart lands —
> it resolves itself, but that is the explanation when a new page looks broken
> for a minute after a merge.

### Service 1: Bot (main.py)

1. **New Service → GitHub Repo →** this repository
2. **Settings → Build → Dockerfile Path:** `Dockerfile.bot`
3. **Settings → Networking → Generate Domain.** Railway reads `EXPOSE 9000` from
   the Dockerfile and routes to that port.
   - Railway also injects a `PORT` variable. **This app ignores it** — the
     Dockerfile's `CMD` hardcodes `--port 9000`. That is consistent and works,
     but if the target port is ever set by hand it must stay `9000`, or the
     service will deploy successfully and answer nothing.
4. **Variables:**
   ```
   DATABASE_URL=${{Postgres.DATABASE_URL}}   # reference variable, not a literal
   REDIS_URL=${{Redis.REDIS_URL}}
   JWT_SECRET=<long-random-secret>           # REQUIRED — signs login tokens
   WEBHOOK_SECRET=<long-random-secret>       # REQUIRED — authenticates Evolution's /webhook calls
   SUPERADMIN_EMAIL=you@example.com          # your platform-operator login (seeded on boot)
   SUPERADMIN_PASSWORD=<strong-password>     # change after first login
   ALLOWED_ORIGINS=https://<your-admin>.up.railway.app   # CORS allow-list (defaults to *)
   TZ=Africa/Johannesburg
   ```
   - Use Railway's **reference variables** (`${{Postgres.DATABASE_URL}}`) rather
     than pasting a connection string. A rotated database password then reaches
     the bot on its own; a pasted literal goes stale silently and the bot fails
     to boot at the worst moment.
   - Railway's Postgres hands out a `postgresql://` URL. Older providers hand out
     `postgres://`, which SQLAlchemy rejects — `app/config.py` rewrites that
     prefix, so either form works.
   - `JWT_SECRET` **must be set** or auth returns `503`. Generate one e.g. `openssl rand -hex 32`.
   - `WEBHOOK_SECRET` **must be set.** If it is empty the bot still boots and takes
     bookings — so an existing deployment doesn't go silent on upgrade — but
     `/webhook` is then completely open: the tenant is resolved from the request
     body, and a business WhatsApp number is public, so anyone who finds the URL
     can forge bookings, cancel real customers' spots, and burn Evolution credits.
     Startup prints a warning and `GET /health` reports `"webhook_auth": false`
     until it's set. Use a different value from `JWT_SECRET`.
   - `SUPERADMIN_EMAIL`/`SUPERADMIN_PASSWORD` seed your super-admin account on startup
     (only if it doesn't already exist). Without them, no one can log in.
   - **Changing `SUPERADMIN_PASSWORD` after the account exists does nothing.**
     `seed_superadmin()` returns early when the email is already present, so
     the env var is a *seed*, not a reset — the old password keeps working and
     the new one is silently ignored. This is the most common reason a login
     that "should" work answers "Invalid email or password". To change the
     password of an account that already exists, either change it from the
     dashboard, or reset it with the API:
     ```bash
     curl -X PATCH "https://<your-bot>/admin/users/<user_id>" \
       -H "Authorization: Bearer <your-jwt>" \
       -H "Content-Type: application/json" \
       -d '{"password":"<new-password>"}'
     ```
     Deleting the row and restarting also works, but it discards every other
     field on that user.
   - `ALLOWED_ORIGINS` is comma-separated; set it to your admin URL in production instead of `*`.
5. Deploy. Check `GET /health` on the generated domain — `"webhook_auth"` must
   be `true`.

6. Once live, log in to the admin frontend with your `SUPERADMIN_EMAIL` and
   register your first business from **Businesses → + Add Business**. There is
   no seed endpoint — tenants carry Evolution credentials, so they are created
   through an authenticated route only.

**Replicas: keep this at one.** The scheduler runs inside the web process, so a
second replica runs a second copy of the reminder sweep, the notification
reconciler and the outbox drain. Nothing double-sends if it happens — every
notification is claimed in `notification_log` and every message carries an
outbox `dedupe_key`, both enforced by unique constraints in the database rather
than by timing — but the duplicated work is pure waste, and one instance is the
shape this is built for.

---

### Service 2: Admin Frontend (React)

1. **New Service → GitHub Repo →** this repository (the same repo, a second service)
2. **Settings → Build → Dockerfile Path:** `Dockerfile.frontend`
3. **Variables:**
   ```
   VITE_API_URL=https://<your-bot>.up.railway.app
   ```
   Railway exposes service variables to the Docker build, and `Dockerfile.frontend`
   declares a matching `ARG VITE_API_URL`.

   > **This one is baked in at build time, not read at runtime.** Vite compiles
   > it into the bundle, so changing the variable does nothing until the service
   > is rebuilt.
   >
   > `Dockerfile.frontend` has no fallback and **fails the build** if this is
   > missing, rather than shipping a bundle that loads perfectly and talks to
   > nothing. A failed build costs a deploy; the running container keeps serving
   > until a good one replaces it.

4. **Settings → Networking → Generate Domain.** Railway reads `EXPOSE 80` from
   the Dockerfile; nginx listens on 80 and serves the SPA.
5. Deploy, then open the generated domain — you should see the login screen.
6. Add that domain to the bot's `ALLOWED_ORIGINS`, then redeploy the bot.

---

### Services 3 and 4: Postgres and Redis

**New Service → Database → Add PostgreSQL**, and the same for **Redis**. No
configuration beyond that; the bot reaches them through the reference variables
above, over Railway's private network.

**Postgres is not optional.** Every booking, tenant and message lives there.

**Redis is close to optional, and fails in a way worth knowing.** It holds two
things: the conversation session store, and the booking lock. If it is gone or
unreachable:

- `booking_lock` **degrades open** — deliberately. Bookings keep working, and
  two landing in the same instant can pick the same agent. The partial unique
  index `uq_queueentry_agent_slot` is what still stops an appointment being
  double-booked, and it is enforced by the database, not by Redis.
- Conversations lose their place mid-flow. A customer halfway through picking a
  time starts again.

So a Redis outage degrades the product rather than corrupting it. See
[Booking lock](#booking-lock).

### Schema changes on deploy

There is no migration step to run. `create_db_and_tables()` runs on boot and:

1. creates any missing table,
2. applies `ADDED_COLUMNS` — additive `ALTER TABLE` statements, each guarded by
   an `information_schema` check,
3. builds the indexes in `INDEXES` and `UNIQUE_INDEXES`, **after** the column
   additions, so an index over a newly added column can actually be built,
4. runs `backfill_notification_log()`, which seeds `notification_log` from the
   two boolean columns it replaced. Without it, every customer already warned
   before that shipped would be warned again on the first tick after deploy.

Every step is additive and idempotent. Nothing is dropped, renamed or
backfilled destructively, so a redeploy of an older image finds a database it
still understands — the columns it does not know about are simply unread. That
is what makes Railway's automatic deploys safe here.

---

## Cloudflare Setup

The admin frontend is a static SPA. It can live on Cloudflare Workers instead
of the Railway frontend service; the **bot stays on Railway**, because it needs
Postgres, Redis and a long-lived process with a scheduler, none of which
Workers provides.

| Piece | Where |
|-------|-------|
| Admin dashboard | Cloudflare Workers (static assets) |
| Bot (`main.py`) | Railway |
| Postgres, Redis | Railway |

### Why there is a `wrangler.jsonc`

Wrangler's automatic configuration detects a Vite project and refuses to guess
at anything older than Vite 6:

```
[ERROR] The version of Vite used in the project ("5.4.21") cannot be
automatically configured. Please update the Vite version to at least "6.0.0"
and try again.
```

Two things fix it, and this repo does both:

1. **Vite is upgraded to 6.x** (`package.json`). The version in the error
   message is the one that was pinned in `package.json`.
2. **`wrangler.jsonc` is checked in.** Automatic configuration only runs when
   there is no Wrangler config file, so a checked-in config makes the
   deployment deterministic — Wrangler uploads the assets as they are, with no
   framework detection in the way.

The config uploads `dist/` as static assets with
`not_found_handling: "single-page-application"`. That last line is what makes
client-side routing work: without it, opening `/admin/businesses` directly
returns Cloudflare's 404 page instead of `index.html`, and React Router never
boots.

### Deploy from the dashboard

1. **Cloudflare Dashboard → Workers & Pages → Create → Import from GitHub**, and
   pick this repository.
2. **Settings → Builds → Build configuration:**
   | Field | Value |
   |-------|-------|
   | Build command | `npm run build:deploy` |
   | Deploy command | `npx wrangler deploy` |
   | Root directory | *(leave empty)* |
3. **Settings → Variables and Secrets → Add:**
   | Name | Value | Environment |
   |------|-------|-------------|
   | `VITE_API_URL` | `https://<your-bot>.up.railway.app` | Build and runtime |

   > **`VITE_API_URL` is baked into the bundle at build time.** Vite compiles
   > it in, so changing the variable does nothing until the next build.
   >
   > `npm run build:deploy` **fails the build** if it is missing, rather than
   > shipping a bundle that loads perfectly and talks to nothing. A failed
   > build costs a deploy; the live site keeps serving until a good one
   > replaces it.

4. Deploy. Open the `*.workers.dev` URL — you should see the login screen.
5. Add the `*.workers.dev` origin to the bot's `ALLOWED_ORIGINS` on Railway,
   then redeploy the bot. The dashboard and the bot are on different origins,
   so the browser will not make the requests at all until this is done.

### Deploy from the command line

```bash
npm install
npx wrangler login
export VITE_API_URL=https://<your-bot>.up.railway.app
npm run deploy
```

`npm run deploy` is `build:deploy` followed by `wrangler deploy` — the same
two steps, with the same guard in between. `npm run preview:cf` serves the
same build locally through `wrangler dev` if you want to check the routing
before it goes out.

### Cloudflare Pages

If you would rather use Pages, the same build output works:
`npx wrangler pages deploy dist`. `public/_redirects` is already in the repo
and carries the SPA fallback (`/* /index.html 200`) for Pages, which does not
read `not_found_handling`. The two are mutually exclusive configuration —
Workers uses the `assets` block, Pages uses the `_redirects` file.

---

## Adding a new business tenant

1. In Evolution API Manager → create a new instance → scan QR → set webhook to
   `https://your-bot-url/webhook?token=<WEBHOOK_SECRET>`
   (or set the webhook URL plainly and add an `X-Webhook-Token` header — see
   [Webhook authentication](#webhook-authentication) below)
2. In BookBot Admin → **Add Business** → fill in the form → Register
3. Under **What The Bot Does**, pick the mode:
   - **Queue bookings** — salon, clinic, workshop. Services, agents, ETAs.
   - **Takeaway orders** — kitchen, kota shop. Menu, cart, collection times.
     See [Ordering](#ordering-takeaway-businesses).
4. Done — that business's customers can now message it on WhatsApp.

A business runs one mode or the other, and it can be switched later on the same
form. Everything that existed before this shipped is a queue business and stays
one until someone changes it.

---

## Authentication & multi-tenant access

Email + password logins, scoped per business:

- **Super-admin (you):** seeded from `SUPERADMIN_EMAIL`/`SUPERADMIN_PASSWORD`.
  Sees every business, creates businesses, and provisions client logins.
- **Tenant user (each client):** only sees and manages their **own** business —
  queue, services and agents, or orders and menu, depending on the mode. Cannot
  see other tenants or the Businesses page.

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
isolation holds even if the UI is bypassed. `/health` stays public.

---

## Webhook authentication

`/webhook` requires the `WEBHOOK_SECRET` shared secret on every call. It is
accepted three ways, whichever your Evolution version can be configured to send:

| How | Where to set it |
|-----|-----------------|
| `?token=<secret>` on the URL | Evolution instance → webhook URL. **Always works** — the URL is always editable. |
| `X-Webhook-Token: <secret>` | Evolution instance → webhook custom headers (v2+). |
| `Authorization: Bearer <secret>` | Same, if you prefer the standard header. |

Anything else gets `401`. Check it's on with `curl https://your-bot-url/health`
→ `"webhook_auth": true`.

### Rotating the secret (no downtime)
`WEBHOOK_SECRET` is comma-separated and every value is accepted, so:

1. Set `WEBHOOK_SECRET=<old>,<new>` and redeploy.
2. Repoint every Evolution instance at `<new>`.
3. Set `WEBHOOK_SECRET=<new>` and redeploy.

Skipping step 1 means bookings 401 until every instance is repointed.

---

## Testing

```
pip install -r requirements-dev.txt
pytest                       # runs unit + sqlite-backed integration tests
```

No Postgres/Redis needed for the test suite (uses a temp SQLite DB; health-check
redis errors are tolerated).

### `tests/test_orders.py`
Drives the whole takeaway conversation through the real `/webhook` endpoint with
an in-process fake Redis, then covers pricing and price-snapshotting, the
backlog maths at one pan versus four, cancellation rules, tenant isolation, the
midnight sweep, and that a queue business still gets the queue bot.

### `tests/test_eta_characterization.py`
Pins what the four ETA functions — `calculate_estimated_start`,
`get_agent_backlog_minutes`, `recalculate_queue`,
`find_walkin_insert_joined_at` — do **today**, to the minute, against a frozen
clock. They are not a specification; they exist so that changing the scheduling
model can't alter behaviour silently.

Behaviour that is arguably wrong is pinned anyway and tagged `QUIRK:` in the
docstring — a single late booking reading as hours of backlog, an idle gap never
being filled by the customer behind it, a deleted service counting as 0 minutes
in one function and 60 in another. **A `QUIRK` test failing during a scheduling
change is expected**: read it, decide whether the new behaviour is the fix,
update the pin deliberately. Any other failure is a regression.

Per-agent working hours went in against these pins and every one held — the
no-schedule fallback reproduces the old behaviour exactly. Only the two
closing-time QUIRKs were rewritten, and only to narrow them: a quote now snaps
into a real working window, and what survives is the overflow case where the
day is oversubscribed and there is nothing to snap to.

Verified by mutation: six deliberate breakages of the engine (dropping the
now() anchor, dropping the duration floor, flipping the gap-fill boundary from
`<=` to `<`, ignoring `earliest_arrival`, re-timing `InService`, treating
`InService` as full duration) each fail between 2 and 6 of these tests.

---

## Message delivery & notifications

Outbound WhatsApp messages are **not** sent inline. `send_text` writes a row to
the `outbox_message` table and returns; a background worker drains it. So a
webhook reply never waits on Evolution, and a send that fails is retried rather
than silently lost.

- Retries use exponential backoff (5s, 10s, 20s, 40s, 80s), capped at 5 minutes,
  giving up after `OUTBOX_MAX_ATTEMPTS` (5) and marking the row `Failed`.
- Messages go out **in queue order, one at a time** — consecutive bot replies
  must arrive in the order they were written. A failing message is deferred and
  the worker moves on, so one unreachable tenant can't block the rest.
- Notifications that must not duplicate (`you're next`, the 15-minute warning)
  carry a `dedupe_key` and are claimed in `notification_log` first.

**One notification is one row, not one column.** `notification_log` holds a row
per `(entry_id, rule)`, and its unique constraint *is* the claim: two callers
racing on the same notification both INSERT, exactly one commits, and the loser
is told it lost. Adding a notification means adding a rule name — no migration
on `queueentry`, which is the hottest table in the app. `notified_two_away` and
`notified_next` are still written so a rollout with an old process still running
cannot re-send, and `backfill_notification_log()` seeds the log from them on
startup; without that seed the first tick after deploy would re-warn every entry
already warned. Both columns can go once no old process is left.

**The 15-minute warning is derived from queue state, not scheduled.** Every
`RECONCILE_SECONDS` (60) the reconciler looks at who is currently `InService`
and warns the next waiter if that service is within 15 minutes of finishing.
Nothing is persisted as a timer, so a restart or crash can't lose a warning —
worst case it goes out one tick late. `"You're up next"` still fires
immediately when staff mark someone Done/NoShow/Cancelled.

**The midnight sweep covers a range, not one date.** `midnight_reset_job` runs
at 00:01 and again on startup, closing everything left open on any day *before
today* — today itself is still being worked, and future bookings are never
touched. The 00:01 cron does not fire retroactively, so a process down across
two midnights would otherwise strand the older day forever: its entries stay
`Waiting`, keep occupying their agent in every backlog and ETA calculation, and
never reach reporting as closed. It works in batches of `SWEEP_BATCH` (500) per
transaction, so the first pass after a long outage doesn't hold one long write
lock.

### Monitoring
`GET /health` reports `outbox_pending` and `outbox_failed`. Pending should sit
near zero; a climbing number means Evolution is unreachable, and any `failed`
count is a message that never reached a customer. Both are worth alerting on.

---

## Database indexes

`ensure_indexes()` runs on every boot and creates the hot-path indexes with
`CREATE INDEX IF NOT EXISTS`, so both fresh and existing databases get them —
`create_all()` alone only indexes tables it creates, which is never the case on
an already-deployed instance.

`agent_schedule` and `agent_block` are new tables; `create_all()` adds them on
first boot after upgrade, and `ensure_indexes()` adds their two indexes. No
backfill is needed — an agent with no schedule rows falls back to the tenant's
opening hours (see [Agent working hours](#agent-working-hours)).

The one that matters most is
`ix_queueentry_tenant_date_agent_status (tenant_id, queue_date, agent_id, status)`.
Every backlog, ETA-recalculation, gap-fill and next-waiter query filters exactly
those columns, and the `(tenant_id, queue_date)` prefix also covers the
dashboard's whole-day read. The rest cover party cancellation, the
"already in the queue?" check, the 60s reconciler sweep, the outbox drain, and
tenant/agent/service lookups. Full list: `INDEXES` in `app/db.py`.

Ordering adds four: the kitchen board's `(tenant_id, order_date, status)`, the
customer's "what did I order?" lookup, order lines by their order, and menu
items by tenant.

Index creation takes a brief write lock on PostgreSQL. At this data size that's
milliseconds; if `queueentry` ever reaches millions of rows, switch to
`CREATE INDEX CONCURRENTLY` (which cannot run inside a transaction block).

`tests/test_indexes.py` asserts each index exists **and** that the planner
actually chooses it for the real query shapes — an index the planner ignores
because of column order would otherwise pass silently.

---

## Query cost

The queue engine used to fetch each entry's service with its own round trip,
and `assign_agent` opened a fresh database session per candidate agent. Answers
were correct, they were just paid for per row. Measured on 6 agents / 48
entries:

| Call | Before | After |
|------|--------|-------|
| `assign_agent` | 57 | 6 |
| `get_queue` (dashboard poll) | 97 | 4 |
| `recalculate_queue` | 51 selects | 4 selects |
| `get_agent_backlog_minutes` | 9 | 2 |
| `find_walkin_insert_joined_at` | 10 | 2 |

`get_agent_backlogs_minutes(agent_ids, …)` answers "who is free soonest?" for
every candidate in two queries; `assign_agent` uses it. `tests/test_concurrency.py`
asserts the counts do not grow with the number of rows or agents, which is what
fails if an N+1 creeps back — a plain correctness test would stay green.

---

## Reports

`GET /admin/analytics/{tenant_id}?from_date=&to_date=` (default: last 30 days,
max 366). Dashboard: **Reports** in the sidebar.

Volume, completion, busiest hours and weekdays, per-agent workload, service mix,
and measured wait/service times.

### The no-show trap

`midnight_reset_job` closes anything staff left open as `NoShow`. Counted
naively, **a shop that forgets to tap Done looks like a shop whose customers
don't turn up** — the report would measure staff habits while claiming to
measure customers.

So `queueentry.closed_by` records who closed each entry:

| Value | Set by | Counted as |
|---|---|---|
| `staff` | dashboard status change | a real no-show / cancellation |
| `customer` | WhatsApp cancellation | customer-initiated cancel |
| `system` | the midnight sweep | **`unclosed`** — a data-quality number, never a no-show |
| `""` | rows written before this shipped | `no_shows_untracked` |

Two consequences worth knowing before you read a number:

- **The no-show rate is `null` for any range containing untracked rows.** A rate
  computed over them would read low and look authoritative. The response carries
  `rates.no_show_note` explaining why, and the dashboard shows it instead of a
  figure. It starts reporting once the range contains only rows closed after
  the upgrade.
- **Rates are over closed entries only.** Including a queue that is still
  running would make completion fall through the morning and recover by closing
  time, meaning nothing. `still_open` is reported separately.

### Measured vs estimated

`started_at` and `finished_at` are written when staff move an entry to
InService and to a terminal status. Nothing recorded these before — 
`estimated_start` is a forecast that recalculation overwrites — so **real wait
and service times only exist from this upgrade onwards**. With no samples the
response says `available: false` rather than showing a zero that reads like a
measurement. `minutes_booked` in the service mix is scheduled duration from each
service's configured length, labelled as such in the UI.

`p90` is nearest-rank: "9 in 10 customers were seen within this". Nine 5-minute
waits and one 120 gives a p90 of 5, not 120 — one bad morning must not read as
a staffing problem.

### Migration
`closed_by`, `started_at` and `finished_at` are added to `queueentry` by
`QUEUEENTRY_COLUMNS` in `create_db_and_tables()` on boot. Existing rows keep
`closed_by = ''` and are reported as untracked. No backfill — inventing
provenance for old rows is exactly the error this design exists to avoid.

---

## Agent working hours

Each agent has a recurring weekly pattern (`agent_schedule`) plus one-off
unavailable windows (`agent_block`) that cut holes in it. The queue engine
places every booking inside the resulting **working windows** — ETAs, backlog
placement, gap-fill and assignment all respect them.

Set them in the dashboard: **Agents → Hours** on any agent row.

### The fallback that keeps existing shops unchanged

| Agent's schedule rows | Result |
|---|---|
| none at all | the tenant's `queue_opens`–`queue_closes`, **every day** |
| some, including today's weekday | exactly those windows |
| some, none for today's weekday | not working today |

Every agent starts in the first row, so **nothing moves on deploy**. Hours only
change for an agent once someone sets a schedule for them. Saving an empty
schedule puts them back on shop hours.

Two rows for the same weekday make a split shift — 08:00–12:00 and 13:00–17:00
means the hour between is not worked. Overlapping rows coalesce.

### Rules the engine applies
- **A service must fit in one contiguous window.** A 60-minute cut cannot start
  at 12:30 when lunch begins at 13:00. A 30-minute one can.
- **An agent not working that date is never assigned** — including when the
  customer asks for them by name. Without this an agent on leave has an empty
  queue, therefore zero backlog, therefore wins every assignment.
- **Blocks beat the schedule.** A block spanning midnight only removes that
  day's portion.
- **An oversubscribed day still gives everyone a time.** When nothing fits, the
  old back-to-back arithmetic stands and the ETA reads after hours. That is
  honest — nobody can serve them inside the schedule — and no booking is lost.
- Setting a schedule, or adding/removing a block, immediately re-runs
  `recalculate_queue` for the affected day, so customers already quoted the old
  hours get restated times rather than ones nobody will honour.


### API
| Route | Purpose |
|---|---|
| `GET /admin/agents/{id}/schedule` | weekly windows; `uses_tenant_hours` flags the fallback |
| `PUT /admin/agents/{id}/schedule` | replace all — `{"windows":[{"weekday":0,"start_minute":480,"end_minute":1020}]}` |
| `GET /admin/agents/{id}/blocks?from_date=` | upcoming time off |
| `POST /admin/agents/{id}/blocks` | `{"starts_at":ISO,"ends_at":ISO,"reason":""}` |
| `DELETE /admin/blocks/{id}` | remove one |
| `GET /admin/agents/{id}/windows?queue_date=` | **the windows the engine actually uses** — the honest answer to "why was nobody booked at 14:00?" |

`weekday` is 0 = Monday … 6 = Sunday (Python's `datetime.weekday()`). Times are
minutes from midnight, 0–1440. `PUT` is replace-all rather than per-row because
a half-applied schedule would silently take a working day off the board; an
invalid window rejects the whole request and leaves the existing schedule
intact.

---

## Fixed appointments

A queue entry's `estimated_start` is **derived**. `recalculate_queue` rewrites it
whenever anything ahead of it moves, which is correct for a queue and wrong for
a booked time. `queueentry.is_fixed` says this start is a **promise**:

- Fixed entries are never rewritten. Instead they are cut out of the agent's
  working windows, so flexible work is scheduled *around* them.
- `slot_end` is written once at booking. Retiming a service tomorrow must not
  move an appointment promised today.
- Fixed entries take no part in backlog. A 16:00 booking is not queued work at
  09:00; counting it would quote a walk-in a seven-hour wait for an empty shop,
  and would make every agent with an afternoon booking look busy to
  `assign_agent` all morning.
- The gaps *between* appointments stay bookable, which is what lets one shop run
  booked times and people off the street on the same day.

The single thing this rests on: a fixed entry must never travel through the
scheduling loop. Entries are placed in `joined_at` order, and an appointment
booked last week has the earliest `joined_at` of anyone in the shop — treated as
a queue position it would advance the agent's clock to 14:45 before this
morning's walk-in is even considered.

### Slots
`tenant.slot_granularity_minutes` (default 30) is the grid offered times land
on: 09:00, 09:30, 10:00. A service longer than one step consumes several, and a
candidate whose service would run past the end of its window is dropped rather
than allowed to spill into a break.

The grid is anchored to the **shift**, never to the fragments a day's bookings
leave behind. `list_free_slots` takes working windows plus a list of occupied
intervals — it does not take pre-carved free windows. Subtracting a 10:00–10:45
appointment first would leave a window starting at 10:45, and since the grid
restarts at each window, the rest of the day would be offered as 10:45 and 11:15
instead of 11:00 and 11:30.

A slot is only offerable if the *whole service* fits. With 10:00–10:45 booked, a
45-minute service cannot start at 09:30 even though 09:30 itself is free.

### Not booking one chair twice
Three layers, because the first is allowed to fail:

1. `booking_lock` serialises bookings per tenant per date — but it **degrades
   open** when Redis is unreachable, by design. For a queue that costs a
   mis-assignment staff can fix. For an appointment it would cost two people
   sent to one chair.
2. An overlap check inside the transaction. This is what catches a *partial*
   overlap — 10:00 for 45 minutes against a 10:30 booking.
3. `uq_queueentry_agent_slot`, a unique partial index on `(agent_id,
   estimated_start)` over live fixed rows. **This is the layer that still holds
   when Redis is down**, and the reason `reserve_appointment` is the only
   supported way to write a fixed entry.

A flexible entry sitting in the time is *not* a conflict — it reschedules around
the appointment, which is the entire meaning of fixed.

### The customer's conversation

`mode = "appointments"` runs `app/appointments_flow.py`. Every state is prefixed
`apt_`, so a tenant that switches mode mid-session can never land in a queue or
ordering state.

```
menu → 1 book → day → service → person → time → confirm
     → 2 my appointment
     → 3 cancel
```

- **Day and time lists page.** A numbered list on WhatsApp stops being readable
  around ten entries, and a clinic booking six weeks out would otherwise send a
  wall of forty dates. `more` and `back` move a page; `0` steps back a screen.
  `advance_days` governs the span, capped at 60.
- **One person who can do the service is not a choice.** That screen is skipped
  — and `0` from the time list then goes back to *services*, not to a screen
  that would immediately re-skip itself and strand the customer on the times.
- **"No preference" offers every time anybody can do**, once each. Which agent
  takes it is settled at booking against live availability, by the same
  shortest-backlog rule `assign_agent` uses.
- **One live appointment per customer.** Said before they walk the whole flow,
  not after — a second booking wastes a slot the shop could have sold.
- **A time taken mid-conversation is reported, never papered over.** If somebody
  confirms the same slot first, the second customer is told plainly and shown
  what is left. They are never quietly given a different time.

### Reminders

`reminder_offsets_minutes` is a list of minutes before the appointment, largest
first. `"1440,120"` — the default — is a day ahead so the customer can move it,
and two hours ahead so they actually leave. Blank switches reminders off.

`reminder_sweep` runs every `REMINDER_SWEEP_SECONDS` (300) and derives what is
due from live state, exactly like the queue's 15-minute warning. **No timer is
persisted**, so a restart or a crash cannot lose a reminder.

Each offset is a *rung*, and a rung is sendable across a window rather than at
an instant. Two things close that window, earlier wins:

- **The next rung takes over.** Once "in about two hours" is due, "tomorrow" has
  nothing left to say.
- **Its own lateness budget — half its lead time**, floored at 15 minutes. The
  message is worded from the nominal offset, so lateness is bounded by when that
  wording stops being true. "Your appointment is tomorrow" survives eleven hours
  of outage and is absurd twenty-three hours late, by which point the
  appointment is today.

A rung whose whole window is missed is **skipped, not sent late**. That is what
stops a business switching reminders on at lunchtime from sending every customer
a day-ahead notice for appointments starting within the hour.

Reminders only ever go to fixed entries. A queue entry has no promised time to
remind anyone about and keeps the 15-minute warning it already had — nothing
changes for a queue or ordering tenant.

**Never at 03:00.** A rung that comes due outside `queue_opens`–`queue_closes`
waits for opening, which it can do because the rung stays open. Only a rung
whose entire window sits outside business hours is lost, which takes a
deliberately odd configuration.

> **Before moving off Evolution:** these are business-initiated messages, often
> sent more than 24 hours after the customer last wrote. Evolution rides the
> WhatsApp Web protocol and does not care. The official Cloud API does — outside
> the 24-hour customer service window it accepts only approved template
> messages. Reminders are the part of this product that would need reworking
> first on a migration, not the booking flow.

### Config

| Field | Meaning |
|---|---|
| `mode` | `"appointments"` |
| `slot_granularity_minutes` | the grid, default 30. Rejected outside 5–240 — a zero makes the engine offer nothing at all, silently |
| `reminder_offsets_minutes` | `"1440,120"`. Up to 4 rungs, each at most a fortnight. Blank = off |
| `advance_days` | how far ahead customers may book |
| `queue_opens` / `queue_closes` | the day's bounds; per-agent schedules narrow it from there, and reminders are held inside them |

The sweep tolerates a malformed `reminder_offsets_minutes` — it drops what it
cannot parse rather than raising, so one tenant's typo cannot stop everybody
else's reminders. The dashboard rejects it at the point somebody types it,
because a silent switch-off would not be noticed until a customer failed to
arrive.

Services, agents, agent schedules and blocks all work exactly as they do for a
queue tenant — an appointment tenant uses the same **Services**, **Agents**,
**Calendar** and **Reports** pages, and the same walk-in endpoint.

### The calendar

`GET /admin/timeline/{tenant_id}?queue_date=YYYY-MM-DD` — the day laid out per
agent: the hours each one works, and the work already placed inside them. The
**Calendar** page draws it as a column per agent on one shared time axis.

The queue list answers *who is next?*. This answers *where are the gaps?*, which
is what a shop taking booked appointments actually asks and what a table cannot
show — a free hour is the absence of a row, and absence is not drawable in a
list.

| Field | Meaning |
|---|---|
| `day_start` / `day_end` | the shared axis, floored and ceiled to the hour. Widened to cover anything scheduled outside the working hours, because an overrun is exactly what someone opens this view to find |
| `agents[].windows` | what the engine schedules into — schedule resolved for the weekday, blocks already subtracted. The same answer `/admin/agents/{id}/windows` gives, for every agent at once |
| `agents[].entries` | placed work, in time order, each with `start`, `end`, `minutes` and `is_fixed` |
| `agents[].working_minutes` / `booked_minutes` / `free_minutes` | the day's load. Free time is clamped at zero — a block dropped on top of an existing booking leaves more booked than worked |
| `agents[].unplaced` | entries with no time. Nothing should reach this state; if something does it is named rather than dropped |
| `orphaned` | entries whose agent row no longer exists, for the same reason |

Batched like the queue read: windows for every agent in three queries and one
service lookup, so the cost does not grow per agent.

Two rules the view keeps to:

- **Cancelled and no-show entries are not drawn.** They gave their time back.
  Drawing them would show the day as fuller than it is, which is the one thing a
  calendar must never do. `Done` stays — the morning is still worth seeing in
  the afternoon.
- **A fixed entry is drawn at the length it was booked at**, from `slot_end`,
  not from today's `Service.duration_minutes`. Retiming a service must not
  silently redraw an appointment somebody was already promised.

Overlapping blocks are laid out side by side rather than stacked. Nothing should
ever overlap — the engine schedules around fixed slots — so that layout exists
to make a scheduling bug visible, not to look tidy.

Not appointment-only: a queue tenant gets the same page, where the blocks are
forecasts the next walk-in may move. Fixed appointments carry a marker and a
different colour, because "promised" and "predicted" cannot be allowed to look
alike on a wall the shop runs its day from.

### Migration

`mode` defaults to `"queue"`, so nothing changes for a deployed tenant until
someone picks the new mode on the dashboard. `slot_granularity_minutes` (30) and
`reminder_offsets_minutes` (`"1440,120"`) are added by `ADDED_COLUMNS` and are
ignored in every other mode — a queue tenant inherits a reminder ladder that
nothing will ever read, because the sweep only looks at fixed entries.

---

## Ordering (takeaway businesses)

A business with `mode = "orders"` runs a different WhatsApp conversation: browse
a menu, build a cart, place an order, chase it, cancel it. Nothing in the queue
engine is involved — the two share the tenant, the Redis session store and the
outbox, and nothing else.

Dashboard: **Menu** and **Orders** appear in the sidebar for ordering
businesses. A takeaway-only account lands on the kitchen board instead of a
queue it doesn't have.

### Why separate tables and not "a queue entry with items"

A queue entry is one service, on one agent, at one time. An order is several
items, in quantities, with money attached and nobody assigned. Bending one into
the other would have put an "is this actually an order?" branch in every backlog,
ETA and reporting query. `menu_item`, `customer_order` and `order_item` are their
own tables. (`order` is a reserved word in SQL — hence `customer_order`, the same
reason `User` is stored as `app_user`.)

### The customer's conversation

```
hi           → main menu: place an order / my order / cancel
1            → the menu, grouped by category, numbered
3            → picks item 3 → "How many Full House?"
2            → adds two, shows the cart and a running total
2            → checkout → "Any special request?"
no chilli    → confirm screen with the total and a collection time
yes          → order placed, with a #code and a ready time
```

`0` goes back a step from anywhere. Session state lives in Redis under `ord_*`
keys, prefixed so a business that switches modes mid-conversation can never land
a cart in a queue state.

### Money

Cents, everywhere, converted only at the UI edge. Order lines snapshot the
item's **name, price and prep time at the moment of placement**. Reprice a kota
tonight and this morning's takings are unchanged; delete an item and its
history still reads correctly. This is the single most important thing to
preserve if you extend the schema.

### Ready times

```
ready_at = now + (committed prep minutes ÷ kitchen_parallel_items) + this order's slowest item
```

`kitchen_parallel_items` (default 4, per business, on the tenant form) is how
many items the kitchen genuinely cooks at once. A four-pan kitchen quoting the
raw sum of everything outstanding tells every customer to come back an hour
late, and they stop believing the quote — so the backlog is divided by
throughput. Set it to 1 for a single fryer and the whole queue is quoted.

Items within one order are assumed to come off together: a burger and a Coke go
into one bag, so the Coke finishing sooner isn't something the customer
experiences.

### Statuses

| Status | Meaning | Who moves it |
|---|---|---|
| `Placed` | received, kitchen hasn't started | — |
| `Preparing` | being made | staff, on the board |
| `Ready` | on the pass — **customer is messaged automatically** | staff |
| `Collected` | handed over and paid for | staff |
| `Cancelled` | not happening | staff, or the customer while still `Placed` |

**A customer can only cancel while `Placed`.** Once the kitchen has started,
real food exists and writing it off is a counter decision, not a WhatsApp one —
the bot says so and points them at the till.

The "your order is ready" message carries `dedupe_key = order_ready:{id}`, so a
double-tap, or a bounce back through `Preparing` and forward again, never shouts
at the same customer twice.

### Payment

**Cash at the counter on collection.** There is no online payment and no
delivery. `Collected` is what books an order as money taken — the board's
`takings_cents` counts collected orders only, never open or cancelled ones.

### The midnight sweep

`midnight_reset_job` cancels `Placed`, `Preparing` and `Ready` orders from any
day before today with `closed_by = "system"` — the same provenance rule the
queue uses (see [The no-show trap](#the-no-show-trap)). It never marks them
`Collected`, which would book money as taken for food that may still be sitting
on the pass. Orders with a blank `order_date` are left alone: a blank sorts
before every real date, and a row whose day is unknown is not a row the sweep
knows to be stale.

### API

| Route | Purpose |
|---|---|
| `GET /admin/menu/{tenant_id}` | full menu **including sold-out items** — staff need to see them to switch them back on |
| `POST /admin/menu` | `{"tenant_id":1,"name":"Kota","price_cents":4500,"prep_minutes":12,"category":"Kotas"}` |
| `PATCH /admin/menu/{item_id}` | edit; `is_active:false` = sold out |
| `DELETE /admin/menu/{item_id}` | remove for good — past orders keep their own copy |
| `GET /admin/orders/{tenant_id}?order_date=` | the kitchen board, plus a summary and takings |
| `PATCH /admin/orders/{order_id}/status` | `{"status":"Ready"}` — sends the ready message |
| `POST /admin/orders` | counter order; a `customer_phone` gets the same ready message |

`sort_order` controls display order **and** which category leads: the category
containing the lowest `sort_order` comes first. Otherwise "Drinks" would sit
above "Kotas" at a kota shop purely because D precedes K.

### Migration

`menu_item`, `customer_order` and `order_item` are created by `create_all()` on
first boot after upgrade, and `ensure_indexes()` adds their four indexes.
`mode`, `currency_symbol` and `kitchen_parallel_items` are added to the existing
`tenant` table by `TENANT_COLUMNS` in `create_db_and_tables()`, defaulting to
`queue` / `R` / `4`. No backfill, and no existing business changes behaviour.

---

## Booking lock

Agent assignment is read-then-write: `assign_agent` measures every agent's
backlog, then the caller inserts a row that changes those backlogs. Two
bookings landing inside that window both see the pre-insert state and pick the
same agent. `booking_lock(tenant_id, queue_date)` — a Redis lock scoped per
business per day — serialises assignment and insert on both paths, WhatsApp
(`_do_assign`) and dashboard walk-in (`add_walkin`). Two businesses, or the
same business on two dates, never wait on each other.

The WhatsApp flow picks an agent *before* asking for arrival time, so by the
time the customer replies the pick is a round trip stale. If they never named
an agent, `_do_assign` re-resolves under the lock. That can only find an agent
whose backlog is ≤ the one already quoted, so the ETA the customer was shown
still holds. An explicitly requested agent is never overridden.

**It degrades open.** If Redis is unreachable, or the lock is still held after
`BOOKING_LOCK_WAIT` (2s), the booking proceeds unlocked — exactly the behaviour
before the lock existed. Dropping a real booking is worse than a rare double
assignment, which staff can see and fix on the dashboard. The wait is capped
short on purpose: `handle_webhook` is async and calls into this synchronously,
so a long spin would stall the event loop and the outbox drain with it. Normal
hold time is a few writes.

`BOOKING_LOCK_TTL` (15s) is the safety net for a process that dies mid-booking.
Release is compare-and-delete against the holder's own token, so a lock whose
TTL lapsed and was re-taken by someone else is never released out from under
them.

---

## Known limitations (not yet hardened)

- **Single-process assumption.** `Dockerfile.bot` runs one uvicorn worker. Adding
  `--workers` would run one scheduler and one outbox worker *per process*, which
  means duplicate reconciler ticks and concurrent outbox drains. The `dedupe_key`
  and conditional-UPDATE claim prevent duplicate *notifications*, but ordinary
  replies could be sent twice and out of order. Scale by adding a lock or moving
  the worker out of the web process first.
- **No webhook rate limit.** `WEBHOOK_SECRET` stops unauthenticated callers, but
  a holder of the secret (or a compromised Evolution instance) is not throttled.
- **No per-customer concurrency lock.** Bookings are serialised per tenant per
  day (see [Booking lock](#booking-lock)), and duplicate *retries* are dropped
  via message-id idempotency — but two genuinely simultaneous messages from the
  same customer still run their conversation steps concurrently.
- **The booking lock is advisory, not a database constraint.** It degrades open
  when Redis is down, and nothing at the schema level stops two rows taking the
  same position. Positions are re-derived by `recalculate_queue` on the next
  status change, so a duplicate is transient; the agent choice is not.
- **Legacy data:** family bookings created before the party-linkage fix may not
  fully cancel as one. New bookings are fine.
- **Ordering takes no payment.** Cash at the counter on collection. There is no
  payment gateway, so nothing reconciles what was charged against what was
  ordered, and no delivery — collection only.
- **Order placement isn't locked.** Unlike queue bookings there is no
  `booking_lock` around it, because nothing is being assigned: two orders
  landing together each get their own row and their own quote. The second may be
  quoted a ready time that doesn't count the first, so a genuine burst can quote
  slightly optimistically.
- **Order codes are the last three digits of the id.** Unique within a day well
  past any realistic volume, and the board is per-day, so wrap-around isn't
  visible — but it is not a guarantee.

