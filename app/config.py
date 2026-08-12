"""Environment-driven configuration. Import the module, not the names —
these values are rebound in tests.
"""
import os
import redis

# =============================================================================
# 1. CONFIGURATION
# =============================================================================

DATABASE_URL = os.getenv("DATABASE_URL", "")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
if not DATABASE_URL:
    DATABASE_URL = "postgresql://postgres:password@localhost:5432/queuebot"

REDIS_URL    = os.getenv("REDIS_URL", "redis://localhost:6379")
redis_client = redis.from_url(REDIS_URL, decode_responses=True)
SESSION_TTL  = 60 * 30  # 30 min inactivity expires session

# ── Outbound message delivery ────────────────────────────────────────────────
# Every WhatsApp message goes through the outbox table rather than a direct
# blocking HTTP call, so a webhook reply never waits on Evolution and a failed
# send is retried instead of lost.
OUTBOX_POLL_SECONDS  = 0.5   # drain tick — also the worst-case added reply latency
OUTBOX_BATCH         = 20    # messages per tick
OUTBOX_MAX_ATTEMPTS  = 5     # then give up and mark Failed
OUTBOX_SEND_TIMEOUT  = 10.0  # seconds per Evolution call
# How often to re-derive notifications that should have fired from queue state.
RECONCILE_SECONDS    = 60
# How often to look for appointment reminders that have come due. Coarser than
# the queue reconciler on purpose: a reminder a few minutes either side of the
# hour is indistinguishable to the customer, and this sweep reads every live
# appointment across every tenant.
REMINDER_SWEEP_SECONDS = 300
# How late a reminder may still go out, as a fraction of its own lead time. A
# day-ahead rung tolerates hours; a half-hour rung tolerates minutes. Expressed
# this way because lateness is only ever a lie relative to what the message
# claims — "your appointment is tomorrow" is fine eleven hours late and absurd
# twenty-three hours late, by which point the appointment is today.
REMINDER_GRACE_FRACTION    = 0.5
# ...but never less than this, or a short rung could expire between two ticks.
REMINDER_MIN_GRACE_MINUTES = 15

# ── Midnight sweep ───────────────────────────────────────────────────────────
# How many rows the sweep closes per transaction. It runs over every date
# before today rather than yesterday alone, so the first pass after a long
# outage — or after this range sweep first ships onto a database that has been
# accumulating stragglers — can face far more than one night's worth. Batching
# keeps that off a single long-held write lock.
SWEEP_BATCH          = 500

# ── Booking lock ─────────────────────────────────────────────────────────────
# Agent assignment reads the queue, then writes to it. Two bookings landing
# between the read and the write both pick the same "shortest backlog" agent.
# A Redis lock scoped per tenant per day serialises that window.
BOOKING_LOCK_TTL   = 15    # seconds — safety net if a holder dies mid-booking
BOOKING_LOCK_WAIT  = 2.0   # max seconds to wait before proceeding unlocked
BOOKING_LOCK_RETRY = 0.02  # seconds between acquire attempts

# Allowed CORS origins — comma-separated env, defaults to "*" for local/dev.
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()]

# ── Auth config ──────────────────────────────────────────────────────────────
# JWT signing secret — REQUIRED in production. Tokens are unverifiable without it.
JWT_SECRET     = os.getenv("JWT_SECRET", "")
JWT_ALG        = "HS256"
JWT_EXP_HOURS  = int(os.getenv("JWT_EXP_HOURS", "12"))
# Super-admin (platform operator) seeded on startup if absent.
SUPERADMIN_EMAIL    = os.getenv("SUPERADMIN_EMAIL", "")
SUPERADMIN_PASSWORD = os.getenv("SUPERADMIN_PASSWORD", "")

# ── Webhook config ───────────────────────────────────────────────────────────
# Shared secret Evolution must present on every /webhook call. Without it the
# endpoint is world-writable: the tenant is resolved from the request body, and
# a business WhatsApp number is public, so anyone could forge or cancel
# bookings. Comma-separated so a secret can be rotated with no downtime — set
# "old,new", repoint Evolution at "new", then drop "old".
WEBHOOK_SECRETS = [s.strip() for s in os.getenv("WEBHOOK_SECRET", "").split(",") if s.strip()]
