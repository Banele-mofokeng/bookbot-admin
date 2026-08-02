import os
import asyncio
import json
import math
import hashlib
import hmac
import secrets
import time as _time          # aliased — "time" is taken by datetime.time below
import httpx
import redis
import jwt
from contextlib import contextmanager
from datetime import datetime, timedelta, time, date
from zoneinfo import ZoneInfo
from fastapi import FastAPI, Request, HTTPException, Header, Depends, APIRouter
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import SQLModel, Field, create_engine, Session, select, Relationship
from typing import Optional, Dict, Any, List
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Timezone — set TZ env var to your local timezone e.g. "Africa/Johannesburg"
TZ = ZoneInfo(os.getenv("TZ", "Africa/Johannesburg"))

def now() -> datetime:
    """Current local time aware of the configured timezone."""
    return datetime.now(TZ).replace(tzinfo=None)

def today_str() -> str:
    """Today's date as ISO string in local timezone."""
    return now().date().isoformat()

def yesterday_str() -> str:
    return (now().date() - timedelta(days=1)).isoformat()

def normalize_number(number: str) -> str:
    """
    Ensures a SA number has the correct country code.
    0812345678   → 27812345678
    27812345678  → 27812345678
    +27812345678 → 27812345678
    """
    n = number.strip().replace("+", "").replace(" ", "")
    if n.startswith("0"):
        n = "27" + n[1:]
    return n

# =============================================================================
# 1. CONFIGURATION
# =============================================================================

DATABASE_URL = os.getenv("DATABASE_URL", "")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
if not DATABASE_URL:
    DATABASE_URL = "postgresql://postgres:password@localhost:5432/queuebot"

REDIS_URL    = os.getenv("REDIS_URL", "redis://localhost:6379")
engine       = create_engine(DATABASE_URL)
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


# =============================================================================
# 2. DATABASE MODELS
# =============================================================================

class Tenant(SQLModel, table=True):
    """One business using the platform."""
    id: Optional[int]          = Field(default=None, primary_key=True)
    business_name: str                               # "Porsche Hair Salon"
    business_type: str         = "General"           # "Hair Salon", "Clinic", etc.
    whatsapp_number: str                             # the business WhatsApp number
    owner_number: str          = ""                  # owner's personal number for notifications
    evolution_instance: str                          # Evolution API instance name
    evolution_api_key: str                           # Evolution API key
    evolution_api_url: str                           # Evolution API base URL
    # ── Labels (makes the bot generic) ──
    agent_label: str           = "Agent"             # "Stylist", "Doctor", "Bay"
    service_label: str         = "Service"           # "Hair Service", "Procedure"
    # ── Queue config ──
    queue_opens: int           = 8                   # 8 = 08:00
    queue_closes: int          = 17                  # 17 = 17:00
    advance_days: int          = 1                   # how many days ahead allowed (0 = today only)
    is_active: bool            = True


class User(SQLModel, table=True):
    """A dashboard login. Either a super-admin (platform operator, tenant_id=None)
    or a tenant user who can only see/manage their own business."""
    __tablename__ = "app_user"  # avoid the reserved word "user" in Postgres
    id: Optional[int]          = Field(default=None, primary_key=True)
    email: str                 = Field(index=True)       # unique login email (stored lowercased)
    password_hash: str                                   # pbkdf2 "iterations$salt$hash"
    tenant_id: Optional[int]   = Field(default=None, foreign_key="tenant.id")  # None = super-admin
    is_super: bool             = False
    is_active: bool            = True


class Service(SQLModel, table=True):
    """A service offered by a tenant e.g. Box Braids, Wheel Alignment."""
    id: Optional[int]          = Field(default=None, primary_key=True)
    tenant_id: int             = Field(foreign_key="tenant.id")
    name: str                                        # "Box Braids"
    duration_minutes: int      = 60                  # how long this service takes
    is_active: bool            = True


class Agent(SQLModel, table=True):
    """A person or station that serves customers e.g. Nomsa, Bay 1, Dr Dlamini."""
    id: Optional[int]          = Field(default=None, primary_key=True)
    tenant_id: int             = Field(foreign_key="tenant.id")
    name: str                                        # "Nomsa"
    is_active: bool            = True


class AgentService(SQLModel, table=True):
    """Which services each agent can perform."""
    id: Optional[int]          = Field(default=None, primary_key=True)
    agent_id: int              = Field(foreign_key="agent.id")
    service_id: int            = Field(foreign_key="service.id")


class AgentSchedule(SQLModel, table=True):
    """
    One recurring working window for an agent on one weekday. Several rows for
    the same weekday make a split shift — 08:00–12:00 and 13:00–17:00 is two
    rows, and the hour between them is simply not worked.

    Times are minutes from midnight rather than a time column: it keeps the
    arithmetic in one unit, sidesteps per-dialect time handling, and lets a
    window run to 1440 (midnight) without wrapping.

    An agent with NO rows at all falls back to the tenant's opening hours, so
    every existing agent keeps working exactly as before until someone sets a
    schedule. An agent WITH rows but none for today is off today.
    """
    __tablename__ = "agent_schedule"
    id: Optional[int]          = Field(default=None, primary_key=True)
    tenant_id: int             = Field(foreign_key="tenant.id")
    agent_id: int              = Field(foreign_key="agent.id")
    weekday: int                                     # 0 = Monday … 6 = Sunday
    start_minute: int          = 8 * 60              # 480 = 08:00
    end_minute: int            = 17 * 60             # 1020 = 17:00, exclusive


class AgentBlock(SQLModel, table=True):
    """
    A one-off window an agent is unavailable — leave, training, a dentist
    appointment. Subtracted from whatever AgentSchedule says, so it wins.
    Stored as absolute datetimes because a block is a specific occasion, not a
    recurring pattern.
    """
    __tablename__ = "agent_block"
    id: Optional[int]          = Field(default=None, primary_key=True)
    tenant_id: int             = Field(foreign_key="tenant.id")
    agent_id: int              = Field(foreign_key="agent.id")
    starts_at: datetime
    ends_at: datetime
    reason: str                = ""


class QueueEntry(SQLModel, table=True):
    """A single customer in a queue."""
    id: Optional[int]          = Field(default=None, primary_key=True)
    tenant_id: int             = Field(foreign_key="tenant.id")
    service_id: int            = Field(foreign_key="service.id")
    agent_id: int              = Field(foreign_key="agent.id")
    preferred_agent_id: Optional[int] = Field(default=None, foreign_key="agent.id")
    customer_number: str                             # "27764519653@s.whatsapp.net"
    customer_name: str
    additional_names: str      = ""                  # kept for legacy walk-in use; children now get own entries
    parent_entry_id: Optional[int] = Field(default=None)  # set on child entries to link back to parent
    customer_phone: str        = ""                  # walk-in phone for notifications e.g. "27812345678"
    status: str                = "Waiting"           # Waiting|InService|Done|NoShow|Cancelled
    booked_via: str            = "whatsapp"          # whatsapp | walkin
    queue_date: str                                  # "2026-03-18" — ISO date string
    estimated_start: Optional[datetime] = None       # calculated ETA
    earliest_arrival: Optional[datetime] = None      # customer's declared arrival time
    position: int              = 0                   # display position in full queue
    notified_two_away: bool    = False
    notified_next: bool        = False
    joined_at: datetime        = Field(default_factory=now)
    # ── Provenance, for reporting ──
    # Who closed this entry: "staff" (someone tapped it on the dashboard),
    # "customer" (cancelled over WhatsApp), "system" (the midnight sweep closed
    # it because nobody ever did), or "" on rows written before this existed.
    # Reporting must never merge these. A system-closed NoShow means the shop
    # forgot to tap Done — not that the customer failed to turn up — and
    # counting it as a no-show measures staff habits while claiming to measure
    # customers.
    closed_by: str             = ""
    started_at: Optional[datetime]  = None   # first moved to InService
    finished_at: Optional[datetime] = None   # reached a terminal status


class OutboxMessage(SQLModel, table=True):
    """
    A WhatsApp message waiting to go out. Writing here instead of calling
    Evolution inline means a webhook reply never blocks on the network, and a
    send that fails is retried rather than silently dropped.
    """
    __tablename__ = "outbox_message"
    id: Optional[int]          = Field(default=None, primary_key=True)
    tenant_id: int             = Field(foreign_key="tenant.id")
    to_number: str
    body: str
    # Set for notifications that must never go out twice (e.g. "you're next"
    # for a given entry). Empty for ordinary conversational replies, which are
    # always sent — the same menu text legitimately repeats.
    dedupe_key: str            = Field(default="", index=True)
    status: str                = "Pending"   # Pending | Sent | Failed
    attempts: int              = 0
    # Indexed jointly with status — see INDEXES; alone it serves no query.
    next_attempt_at: datetime  = Field(default_factory=now)
    last_error: str            = ""
    created_at: datetime       = Field(default_factory=now)
    sent_at: Optional[datetime] = None


# Indexes for the queries that actually run hot. Table names come from the
# models so a rename can't leave a stale string behind.
#
# Every lookup in the queue engine — backlog, ETA recalculation, gap-fill,
# next-waiter — filters the same four columns, so one composite index serves all
# of them, and its (tenant_id, queue_date) prefix also serves the dashboard's
# whole-day read.
INDEXES = [
    # name, table, columns
    ("ix_queueentry_tenant_date_agent_status", QueueEntry.__tablename__,
     "tenant_id, queue_date, agent_id, status"),
    # "Are you already in the queue?" — runs on every WhatsApp booking attempt.
    ("ix_queueentry_tenant_customer_date", QueueEntry.__tablename__,
     "tenant_id, customer_number, queue_date"),
    # Whole-party cancellation walks children back to their parent.
    ("ix_queueentry_parent", QueueEntry.__tablename__, "parent_entry_id"),
    # Cross-tenant sweeps: the 60s reconciler and the midnight reset.
    ("ix_queueentry_status_date", QueueEntry.__tablename__, "status, queue_date"),
    # The outbox drain runs twice a second — this is the hottest query here.
    ("ix_outbox_status_due", OutboxMessage.__tablename__, "status, next_attempt_at"),
    ("ix_service_tenant", Service.__tablename__, "tenant_id"),
    ("ix_agent_tenant", Agent.__tablename__, "tenant_id"),
    ("ix_agentservice_service", AgentService.__tablename__, "service_id"),
    ("ix_agentservice_agent", AgentService.__tablename__, "agent_id"),
    # Working windows are resolved for every candidate agent on every booking.
    ("ix_agentschedule_agent_weekday", AgentSchedule.__tablename__,
     "agent_id, weekday"),
    ("ix_agentblock_agent_starts", AgentBlock.__tablename__,
     "agent_id, starts_at"),
    # Every inbound webhook resolves the tenant by its WhatsApp number.
    ("ix_tenant_whatsapp", Tenant.__tablename__, "whatsapp_number"),
]


def ensure_indexes():
    """
    Create the hot-path indexes if they're missing.

    create_all() only builds indexes for tables it creates, so an existing
    deployment never gets them from the model definitions alone. CREATE INDEX
    IF NOT EXISTS is understood by both PostgreSQL and SQLite, so this is the
    one place indexes are declared for fresh and existing databases alike.

    These are small tables; the brief write lock PostgreSQL takes while building
    is not worth the extra machinery of CONCURRENTLY. Revisit if queueentry ever
    grows to millions of rows.
    """
    from sqlalchemy import text as sql_text
    with engine.connect() as conn:
        for name, table, columns in INDEXES:
            conn.execute(sql_text(
                f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({columns})"
            ))
        conn.commit()


# Columns added to queueentry after the table first shipped. create_all() never
# alters an existing table, so an already-deployed database only gets these from
# here. Order is irrelevant — each is added only if absent.
QUEUEENTRY_COLUMNS = [
    ("parent_entry_id",  "INTEGER"),
    ("earliest_arrival", "TIMESTAMP"),
    # Analytics provenance. Existing rows keep closed_by = '' and are reported
    # as "unknown" rather than being folded into a real no-show count.
    ("closed_by",        "VARCHAR DEFAULT ''"),
    ("started_at",       "TIMESTAMP"),
    ("finished_at",      "TIMESTAMP"),
]


def create_db_and_tables():
    from sqlalchemy import text as sql_text
    SQLModel.metadata.create_all(engine)
    ensure_indexes()
    # Add new columns to existing tables if they don't exist yet (PostgreSQL migration)
    with engine.connect() as conn:
        existing = {row[0] for row in conn.execute(sql_text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'queueentry'"
        )).fetchall()}
        for column, coltype in QUEUEENTRY_COLUMNS:
            if column not in existing:
                conn.execute(sql_text(
                    f"ALTER TABLE queueentry ADD COLUMN {column} {coltype}"
                ))
                conn.commit()


# =============================================================================
# 2b. AUTH — PASSWORDS, JWT, DEPENDENCIES
# =============================================================================

def hash_password(password: str) -> str:
    """PBKDF2-SHA256 (stdlib, no native deps). Format: 'iterations$salt_hex$hash_hex'."""
    iterations = 200_000
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"{iterations}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        iter_s, salt_hex, hash_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iter_s))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


def create_access_token(user: "User") -> str:
    payload = {
        "sub": str(user.id),
        "email": user.email,
        "is_super": user.is_super,
        "tenant_id": user.tenant_id,
        "exp": datetime.utcnow() + timedelta(hours=JWT_EXP_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)


def get_current_user(authorization: str = Header(default="")) -> "User":
    """Validate the Bearer JWT and return the live User row."""
    if not JWT_SECRET:
        raise HTTPException(status_code=503, detail="JWT_SECRET not configured on server")
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    with Session(engine) as s:
        user = s.get(User, int(payload.get("sub", 0)))
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    return user


def require_super(user: "User" = Depends(get_current_user)) -> "User":
    if not user.is_super:
        raise HTTPException(status_code=403, detail="Super-admin only")
    return user


def ensure_tenant_access(user: "User", tenant_id: int):
    """Super-admins reach any tenant; tenant users only their own."""
    if user.is_super:
        return
    if user.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="Forbidden — not your business")


def verify_webhook_secret(
    request: Request,
    x_webhook_token: str = Header(default=""),
    authorization: str = Header(default=""),
):
    """
    Authenticate an inbound Evolution webhook call against WEBHOOK_SECRETS.

    The secret may arrive three ways, because Evolution deployments differ in
    what they can be configured to send:
      - 'X-Webhook-Token: <secret>'
      - 'Authorization: Bearer <secret>'
      - '?token=<secret>' on the webhook URL (always available — the URL itself
        is editable in the Evolution instance settings)

    With WEBHOOK_SECRET unset the check is skipped, so an already-deployed bot
    keeps taking bookings across the upgrade instead of going silent. Startup
    logs a warning in that case and /health reports it as off.
    """
    if not WEBHOOK_SECRETS:
        return
    if x_webhook_token.strip():
        presented = x_webhook_token.strip()
    elif authorization.lower().startswith("bearer "):
        presented = authorization.split(" ", 1)[1].strip()
    else:
        presented = (request.query_params.get("token") or "").strip()
    # Compare against every configured secret so rotation works, in constant
    # time so a wrong guess can't be tuned byte by byte.
    matched = False
    for secret in WEBHOOK_SECRETS:
        if hmac.compare_digest(presented.encode(), secret.encode()):
            matched = True
    if not presented or not matched:
        raise HTTPException(status_code=401, detail="Invalid webhook credentials")


def seed_superadmin():
    """Create the platform operator login from env if it doesn't exist yet."""
    if not (SUPERADMIN_EMAIL and SUPERADMIN_PASSWORD):
        print("⚠️  SUPERADMIN_EMAIL/PASSWORD not set — no super-admin seeded.")
        return
    email = SUPERADMIN_EMAIL.strip().lower()
    with Session(engine) as s:
        existing = s.exec(select(User).where(User.email == email)).first()
        if existing:
            return
        s.add(User(email=email, password_hash=hash_password(SUPERADMIN_PASSWORD),
                   tenant_id=None, is_super=True, is_active=True))
        s.commit()
    print(f"✅ Seeded super-admin {email}")


# =============================================================================
# 3. APP + SCHEDULER
# =============================================================================

app = FastAPI(title="QueueBot — Smart Queue Platform")
# Every admin route hangs off this router; the dependency authenticates the
# caller. Per-route handlers additionally scope data to the caller's tenant.
admin_router = APIRouter(dependencies=[Depends(get_current_user)])
scheduler = AsyncIOScheduler()

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def on_startup():
    print("🚀 QueueBot starting...")
    create_db_and_tables()
    seed_superadmin()
    if not WEBHOOK_SECRETS:
        print("⚠️  WEBHOOK_SECRET not set — /webhook is UNAUTHENTICATED. "
              "Anyone who knows this URL can forge or cancel bookings. Set it now.")
    else:
        print(f"✅ /webhook authenticated ({len(WEBHOOK_SECRETS)} secret(s) accepted)")
    scheduler.add_job(midnight_reset_job, "cron", hour=0, minute=1, id="midnight_reset")
    scheduler.add_job(reconcile_notifications, "interval",
                      seconds=RECONCILE_SECONDS, id="reconcile_notifications")
    scheduler.start()
    # Catch up on anything missed while the process was down: the 00:01 cron
    # doesn't fire retroactively, so a restart spanning midnight would otherwise
    # leave yesterday's queue open forever.
    await midnight_reset_job()
    app.state.outbox_task = asyncio.create_task(outbox_worker())
    print("✅ DB ready. Scheduler running.")


@app.on_event("shutdown")
async def on_shutdown():
    scheduler.shutdown()
    task = getattr(app.state, "outbox_task", None)
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# =============================================================================
# 4. TENANT HELPERS
# =============================================================================

def get_tenant_by_number(raw: str) -> Optional[Tenant]:
    clean = normalize_number(raw.replace("@s.whatsapp.net", "").replace("@lid", ""))
    with Session(engine) as s:
        return s.exec(
            select(Tenant).where(Tenant.whatsapp_number == clean, Tenant.is_active == True)
        ).first()


# =============================================================================
# 5. REDIS SESSION HELPERS
# =============================================================================

def get_session(tenant_id: int, customer_num: str) -> dict:
    raw = redis_client.get(f"s:{tenant_id}:{customer_num}")
    return json.loads(raw) if raw else {"state": "idle"}


def set_session(tenant_id: int, customer_num: str, data: dict):
    redis_client.setex(f"s:{tenant_id}:{customer_num}", SESSION_TTL, json.dumps(data))


def clear_session(tenant_id: int, customer_num: str):
    redis_client.delete(f"s:{tenant_id}:{customer_num}")


_RELEASE_LOCK_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""


@contextmanager
def booking_lock(tenant_id: int, queue_date: str):
    """
    Serialise agent assignment + queue insert for one tenant on one day.

    Assignment is read-then-write: assign_agent measures every agent's backlog,
    then the caller inserts a row that changes those backlogs. Two bookings
    landing inside that window both see the pre-insert state, both pick the
    same shortest-backlog agent, and both compute the same position. Positions
    self-heal on the next recalculate_queue; the agent choice does not.

    Scoped per tenant per date, so two businesses — or the same business on two
    different dates — never wait on each other.

    Degrades open. If Redis is unreachable, or the lock is still held after
    BOOKING_LOCK_WAIT, the booking goes ahead unlocked. That is exactly the
    behaviour before this lock existed, and dropping a real booking is worse
    than a rare double-assignment that staff can see and fix on the dashboard.

    The wait is capped short because handle_webhook is async and calls into
    this synchronously — a long spin here stalls the event loop, and with it
    the outbox drain. Normal hold time is a few writes, tens of milliseconds.

    Yields True if the lock was actually held, False if it degraded open.
    """
    key   = f"lock:booking:{tenant_id}:{queue_date}"
    token = secrets.token_hex(16)
    held  = False
    try:
        deadline = _time.monotonic() + BOOKING_LOCK_WAIT
        while True:
            if redis_client.set(key, token, nx=True, ex=BOOKING_LOCK_TTL):
                held = True
                break
            if _time.monotonic() >= deadline:
                print(f"⚠️  booking lock busy | {key} — proceeding unlocked")
                break
            _time.sleep(BOOKING_LOCK_RETRY)
    except Exception as exc:
        print(f"⚠️  booking lock unavailable ({exc}) — proceeding unlocked")

    try:
        yield held
    finally:
        if held:
            try:
                # Compare-and-delete. If our TTL already expired and another
                # booking took the lock, a blind DEL would release theirs.
                redis_client.eval(_RELEASE_LOCK_LUA, 1, key, token)
            except Exception as exc:
                print(f"⚠️  booking lock release failed ({exc}) — "
                      f"expires on its own within {BOOKING_LOCK_TTL}s")


# =============================================================================
# 6. QUEUE ENGINE
# =============================================================================

# ── Working windows ──────────────────────────────────────────────────────────
# A "window" is a (start, end) pair of naive datetimes on one calendar day
# during which one agent is actually available. Everything downstream — ETAs,
# backlog placement, gap-fill — schedules inside windows instead of assuming
# the agent is free from opening to closing.

Window = "tuple"   # (datetime, datetime); aliased for readability only


def _merge_windows(windows: List) -> List:
    """Sort and coalesce touching or overlapping windows."""
    out: List = []
    for start, end in sorted(windows):
        if end <= start:
            continue
        if out and start <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


def _subtract_windows(windows: List, blocks: List) -> List:
    """
    Remove every blocked interval from every window. A block in the middle of a
    shift splits it in two, which is what makes a lunch break work.
    """
    out = list(windows)
    for b_start, b_end in blocks:
        if b_end <= b_start:
            continue
        nxt: List = []
        for w_start, w_end in out:
            if b_end <= w_start or b_start >= w_end:
                nxt.append((w_start, w_end))       # no overlap
                continue
            if b_start > w_start:
                nxt.append((w_start, b_start))     # keep the head
            if b_end < w_end:
                nxt.append((b_end, w_end))         # keep the tail
        out = nxt
    return [(s, e) for s, e in sorted(out) if e > s]


def get_working_windows_for(agent_ids: List[int], tenant: Tenant,
                            queue_date: str) -> Dict[int, List]:
    """
    Working windows for several agents on one date — three queries total, so
    assign_agent can rank every candidate without a per-agent round trip.

    Fallback rules, in order:
      * agent has schedule rows for this weekday  → those windows
      * agent has no schedule rows at all         → the tenant's opening hours
      * agent has rows, but none for this weekday → [] (off today)

    The middle case is what keeps existing deployments unchanged: nobody has a
    schedule yet, so nobody's hours move until someone sets one.
    """
    if not agent_ids:
        return {}

    day = datetime.strptime(queue_date, "%Y-%m-%d")
    weekday = day.weekday()
    day_end = day + timedelta(days=1)

    with Session(engine) as s:
        rows = s.exec(
            select(AgentSchedule).where(AgentSchedule.agent_id.in_(agent_ids))
        ).all()
        blocks = s.exec(
            select(AgentBlock).where(
                AgentBlock.agent_id.in_(agent_ids),
                AgentBlock.starts_at < day_end,
                AgentBlock.ends_at   > day,
            )
        ).all()

    has_schedule   = {r.agent_id for r in rows}
    todays_windows: Dict[int, List] = {}
    for r in rows:
        if r.weekday != weekday:
            continue
        todays_windows.setdefault(r.agent_id, []).append((
            day + timedelta(minutes=r.start_minute),
            day + timedelta(minutes=r.end_minute),
        ))

    blocks_by_agent: Dict[int, List] = {}
    for b in blocks:
        # Clip to the day — a block running overnight only removes today's part.
        blocks_by_agent.setdefault(b.agent_id, []).append(
            (max(b.starts_at, day), min(b.ends_at, day_end))
        )

    tenant_hours = [(
        day + timedelta(hours=tenant.queue_opens),
        day + timedelta(hours=tenant.queue_closes),
    )]

    result: Dict[int, List] = {}
    for aid in agent_ids:
        if aid in has_schedule:
            base = todays_windows.get(aid, [])
        else:
            base = tenant_hours
        result[aid] = _subtract_windows(_merge_windows(base),
                                        blocks_by_agent.get(aid, []))
    return result


def get_working_windows(tenant: Tenant, agent_id: int, queue_date: str) -> List:
    """Single-agent convenience wrapper. See get_working_windows_for."""
    return get_working_windows_for([agent_id], tenant, queue_date).get(agent_id, [])


def place_in_windows(windows: List, floor: datetime,
                     duration_minutes: int) -> Optional[datetime]:
    """
    Earliest start at or after `floor` where `duration_minutes` fits inside one
    contiguous window. Returns None when the day has no room left.

    A service must fit in a single window: a 60-minute cut cannot start at
    12:30 if lunch begins at 13:00, because nobody actually works through it.

    `start < w_end` only bites for a zero duration, where it stops a bare
    "when is this agent next free?" snap from answering 13:00 on the dot — the
    exact minute the agent leaves for lunch.
    """
    need = timedelta(minutes=duration_minutes)
    for w_start, w_end in windows:
        start = max(w_start, floor)
        if start < w_end and start + need <= w_end:
            return start
    return None


def _service_map(s: Session, entries) -> Dict[int, Service]:
    """
    Every service referenced by these entries, in one query.

    The queue engine walks entries and needs each one's duration. Doing that
    with s.get(Service, ...) per entry costs a round trip per row, on the
    hottest paths in the app (backlog, ETA recalc, gap-fill, dashboard read).
    A day's queue only ever touches a handful of distinct services.
    """
    ids = {e.service_id for e in entries if e.service_id is not None}
    if not ids:
        return {}
    return {
        svc.id: svc
        for svc in s.exec(select(Service).where(Service.id.in_(ids))).all()
    }


def _backlog_from_entries(entries, services: Dict[int, Service],
                          current_time: datetime,
                          exclude_entry_id: Optional[int] = None) -> int:
    """
    Backlog in minutes for one already-fetched list of entries. Pure — no
    queries — so callers that need several agents' backlogs can fetch once
    and call this per agent.
    """
    total = 0
    for e in entries:
        if exclude_entry_id and e.id == exclude_entry_id:
            continue
        svc = services.get(e.service_id)
        if not svc:
            continue
        if e.estimated_start:
            # Use actual scheduled finish time so gaps (e.g. earliest_arrival)
            # are reflected — the agent isn't free until this entry is done.
            finish_time = e.estimated_start + timedelta(minutes=svc.duration_minutes)
            remaining = (finish_time - current_time).total_seconds() / 60
            if e.status == "InService":
                total += max(0, remaining)
            else:
                # Waiting: never less than the full service duration
                total += max(svc.duration_minutes, max(0, remaining))
        else:
            total += svc.duration_minutes
    return int(total)


def get_agent_backlogs_minutes(agent_ids: List[int], tenant_id: int,
                               queue_date: str) -> Dict[int, int]:
    """
    Backlog for several agents at once — two queries total, regardless of how
    many agents or entries are involved.

    assign_agent used to call get_agent_backlog_minutes once per candidate
    agent, each opening its own Session and re-querying services per entry.
    For a salon with 5 stylists and 20 people booked that was over 100 queries
    to answer one "who's free soonest?".
    """
    if not agent_ids:
        return {}
    with Session(engine) as s:
        entries = s.exec(
            select(QueueEntry).where(
                QueueEntry.agent_id.in_(agent_ids),
                QueueEntry.tenant_id  == tenant_id,
                QueueEntry.queue_date == queue_date,
                QueueEntry.status.in_(["Waiting", "InService"])
            ).order_by(QueueEntry.joined_at)
        ).all()
        services = _service_map(s, entries)

        current_time = now()
        by_agent: Dict[int, list] = {aid: [] for aid in agent_ids}
        for e in entries:
            by_agent.setdefault(e.agent_id, []).append(e)
        return {
            aid: _backlog_from_entries(by_agent.get(aid, []), services, current_time)
            for aid in agent_ids
        }


def get_agent_backlog_minutes(agent_id: int, tenant_id: int, queue_date: str,
                               exclude_entry_id: Optional[int] = None) -> int:
    """
    Minutes from NOW until the agent finishes all current work.
    - InService: time remaining until their service ends.
    - Waiting with estimated_start: time until their scheduled finish
      (estimated_start + duration), because the agent won't start them
      before that slot — so we can't claim the agent is free any earlier.
    - Waiting without estimated_start: full duration (fallback).
    """
    with Session(engine) as s:
        entries = s.exec(
            select(QueueEntry).where(
                QueueEntry.agent_id   == agent_id,
                QueueEntry.tenant_id  == tenant_id,
                QueueEntry.queue_date == queue_date,
                QueueEntry.status.in_(["Waiting", "InService"])
            ).order_by(QueueEntry.joined_at)
        ).all()
        services = _service_map(s, entries)
        return _backlog_from_entries(entries, services, now(), exclude_entry_id)


def calculate_estimated_start(tenant: Tenant, agent_id: int,
                               queue_date: str, backlog_minutes: int,
                               earliest_arrival: Optional[datetime] = None) -> datetime:
    """Convert backlog minutes into an actual datetime.
    Uses max(queue_opens, now) as the base so backlog is always added to
    the correct anchor — preventing stale opens-time calculations mid-day.

    The result is then snapped forward into the agent's next working window, so
    a quote never lands in a lunch break or on a day off. If it lands past the
    agent's last window the raw time is returned unchanged: the day is over-
    subscribed, and an honest after-hours ETA beats pretending otherwise.
    """
    opens = datetime.strptime(
        f"{queue_date} {tenant.queue_opens:02d}:00", "%Y-%m-%d %H:%M"
    )
    base   = max(opens, now())
    result = base + timedelta(minutes=backlog_minutes)
    # Respect customer's declared arrival time
    if earliest_arrival:
        result = max(result, earliest_arrival)

    windows = get_working_windows(tenant, agent_id, queue_date)
    snapped = place_in_windows(windows, result, 0)
    return snapped or result


def parse_arrival_time(text: str, queue_date: str) -> Optional[datetime]:
    """Parse 'now', 'HH:MM', or 'HHMM' into a datetime on queue_date."""
    t = text.strip().lower()
    if t == "now":
        return now()
    for fmt in ("%H:%M", "%H%M"):
        try:
            parsed = datetime.strptime(t, fmt)
            base   = datetime.strptime(queue_date, "%Y-%m-%d")
            return base.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)
        except ValueError:
            continue
    return None


def assign_agent(tenant: Tenant, service_id: int,
                 preferred_agent_id: Optional[int], queue_date: str) -> Optional[int]:
    with Session(engine) as s:
        # Get tenant agents first, then find which can do this service
        tenant_agent_ids = [
            a.id for a in s.exec(
                select(Agent).where(Agent.tenant_id == tenant.id, Agent.is_active == True)
            ).all()
        ]
        print(f"🔍 assign_agent | tenant={tenant.id} service={service_id} tenant_agents={tenant_agent_ids}")

        if not tenant_agent_ids:
            print(f"⚠️  No active agents for tenant {tenant.id}")
            return None

        capable_agent_ids = [
            row.agent_id for row in s.exec(
                select(AgentService).where(
                    AgentService.service_id == service_id,
                    AgentService.agent_id.in_(tenant_agent_ids)
                )
            ).all()
        ]
        print(f"🔍 assign_agent | capable_agents={capable_agent_ids}")

        if not capable_agent_ids:
            print(f"⚠️  No agents can do service {service_id} for tenant {tenant.id}")
            return None

        active_agents = s.exec(
            select(Agent).where(
                Agent.id.in_(capable_agent_ids),
                Agent.is_active == True
            )
        ).all()

        if not active_agents:
            print(f"⚠️  No active agents in capable list {capable_agent_ids}")
            return None

        candidate_ids = [a.id for a in active_agents]

    # Drop anyone not working on this date — off day, or blocked out for the
    # whole of it. Without this an agent on leave has an empty queue, so zero
    # backlog, so they would win every single assignment.
    windows = get_working_windows_for(candidate_ids, tenant, queue_date)
    candidate_ids = [aid for aid in candidate_ids if windows.get(aid)]
    if not candidate_ids:
        print(f"⚠️  No agents working on {queue_date} for tenant {tenant.id}")
        return None

    # Honor preference if that agent is capable and working.
    if preferred_agent_id and preferred_agent_id in candidate_ids:
        return preferred_agent_id

    # Assign to agent with shortest backlog. One batched read for all
    # candidates rather than a fresh Session and a fresh query per agent.
    # Ties break on the earlier candidate_ids position, matching min()'s old
    # first-wins behaviour over active_agents.
    backlogs = get_agent_backlogs_minutes(candidate_ids, tenant.id, queue_date)
    return min(candidate_ids, key=lambda aid: backlogs.get(aid, 0))


def recalculate_queue(tenant_id: int, agent_id: int, queue_date: str):
    """
    After any status change, recalculate estimated_start for all
    Waiting entries on this agent and update their positions.
    """
    with Session(engine) as s:
        tenant = s.get(Tenant, tenant_id)
        if not tenant:
            return

        # Get all entries for this agent sorted by joined_at
        entries = s.exec(
            select(QueueEntry).where(
                QueueEntry.agent_id   == agent_id,
                QueueEntry.tenant_id  == tenant_id,
                QueueEntry.queue_date == queue_date,
                QueueEntry.status.in_(["Waiting", "InService"])
            ).order_by(QueueEntry.joined_at)
        ).all()

        services = _service_map(s, entries)
        windows = get_working_windows(tenant, agent_id, queue_date)
        current_time = now()
        opens = datetime.strptime(
            f"{queue_date} {tenant.queue_opens:02d}:00", "%Y-%m-%d %H:%M"
        )
        # next_free tracks the absolute datetime when the agent becomes free
        next_free = max(opens, current_time)
        for entry in entries:
            svc = services.get(entry.service_id)
            duration = svc.duration_minutes if svc else 60

            if entry.status == "InService":
                # Frozen — advance next_free to when this service actually finishes
                if entry.estimated_start:
                    finish_time = entry.estimated_start + timedelta(minutes=duration)
                    next_free = max(next_free, finish_time)
            else:
                # Start no earlier than next_free, and no earlier than earliest_arrival
                start_time = next_free
                if entry.earliest_arrival:
                    start_time = max(start_time, entry.earliest_arrival)
                # Then push forward to somewhere the agent is actually working.
                # If nothing fits — day oversubscribed, or the agent is off —
                # keep the raw back-to-back time rather than dropping the
                # customer. Their ETA reads after hours, which is the truth.
                start_time = place_in_windows(windows, start_time, duration) or start_time
                entry.estimated_start = start_time
                s.add(entry)
                next_free = start_time + timedelta(minutes=duration)

        # Recalculate display positions across the full tenant queue for this date
        all_waiting = s.exec(
            select(QueueEntry).where(
                QueueEntry.tenant_id  == tenant_id,
                QueueEntry.queue_date == queue_date,
                QueueEntry.status     == "Waiting"
            ).order_by(QueueEntry.estimated_start, QueueEntry.joined_at)
        ).all()

        for i, e in enumerate(all_waiting):
            e.position = i + 1
            s.add(e)

        s.commit()


def cancel_party(tenant_id: int, entry_id: int) -> List[int]:
    """
    Cancel a booking and everyone linked to it (parent + all children),
    so a family booking never leaves stranded child entries holding agent slots.

    Given any entry in the party, resolves the party root (the entry itself if
    it's a parent, else its parent_entry_id) and cancels every active
    (Waiting/InService) entry in that party.

    Returns the list of distinct agent_ids touched, for ETA recalculation.
    """
    touched_agents: set = set()
    with Session(engine) as s:
        entry = s.get(QueueEntry, entry_id)
        if not entry or entry.tenant_id != tenant_id:
            return []
        root_id = entry.parent_entry_id or entry.id
        party = s.exec(
            select(QueueEntry).where(
                QueueEntry.tenant_id == tenant_id,
                (QueueEntry.id == root_id) | (QueueEntry.parent_entry_id == root_id),
            )
        ).all()
        for e in party:
            if e.status in ("Waiting", "InService"):
                e.status      = "Cancelled"
                e.closed_by   = "customer"   # they cancelled over WhatsApp
                e.finished_at = now()
                s.add(e)
                touched_agents.add(e.agent_id)
        s.commit()
    return list(touched_agents)


def find_walkin_insert_joined_at(
    assigned_agent_id: int, tenant_id: int, tenant: "Tenant",
    queue_date: str, walk_in_service_id: int,
    new_arrival: Optional[datetime] = None,
    exclude_entry_id: Optional[int] = None,
) -> Optional[datetime]:
    """
    Check whether a new entry can be slotted in before a future appointment.

    Walks the agent's current queue in order.  At each Waiting entry that has
    an earliest_arrival (i.e. a customer who said they'll arrive at time T),
    it tests whether the new entry's own start (respecting its declared
    arrival new_arrival) plus its duration finishes before T:

        max(max(now, queue_opens) + accumulated_backlog, new_arrival)
            + duration  <=  T

    If yes, the new entry finishes before that customer is even due, so we
    return a joined_at 1 second before that entry's joined_at.
    recalculate_queue then places the new entry ahead of them.

    Used by both walk-ins (new_arrival=None → can start immediately) and
    WhatsApp joiners (new_arrival = their declared arrival time).

    Returns None if the new entry should go at the end of the queue as usual.
    """
    with Session(engine) as s:
        entries = s.exec(
            select(QueueEntry).where(
                QueueEntry.agent_id   == assigned_agent_id,
                QueueEntry.tenant_id  == tenant_id,
                QueueEntry.queue_date == queue_date,
                QueueEntry.status.in_(["Waiting", "InService"])
            ).order_by(QueueEntry.joined_at)
        ).all()

        services = _service_map(s, entries)
        walk_in_svc = services.get(walk_in_service_id) or s.get(Service, walk_in_service_id)
        if not walk_in_svc:
            return None
        walk_in_duration = walk_in_svc.duration_minutes
        windows = get_working_windows(tenant, assigned_agent_id, queue_date)

        opens = datetime.strptime(
            f"{queue_date} {tenant.queue_opens:02d}:00", "%Y-%m-%d %H:%M"
        )
        base         = max(opens, now())
        current_time = now()
        running_minutes = 0

        for entry in entries:
            if exclude_entry_id and entry.id == exclude_entry_id:
                continue
            svc = services.get(entry.service_id)
            duration = svc.duration_minutes if svc else 60

            if entry.status == "InService":
                # Can't insert before someone already being served
                if entry.estimated_start:
                    finish = entry.estimated_start + timedelta(minutes=duration)
                    remaining = (finish - current_time).total_seconds() / 60
                    running_minutes += max(0, remaining)
                else:
                    running_minutes += duration
                continue

            # Waiting entry with a declared arrival time — can we slip in before them?
            if entry.earliest_arrival:
                new_start = base + timedelta(minutes=running_minutes)
                if new_arrival:
                    # New entry can't start before its own declared arrival
                    new_start = max(new_start, new_arrival)
                # The gap only exists if the agent is working through it.
                placed = place_in_windows(windows, new_start, walk_in_duration)
                if placed is None:
                    # running_minutes only grows, so no later gap can fit either.
                    return None
                walk_in_finish = placed + timedelta(minutes=walk_in_duration)
                if walk_in_finish <= entry.earliest_arrival:
                    return entry.joined_at - timedelta(seconds=1)

            running_minutes += duration

    return None


def format_duration(minutes: int) -> str:
    """Converts minutes to a human-friendly string."""
    if minutes < 60:
        return f"~{minutes} min"
    hours   = minutes // 60
    remainder = minutes % 60
    if remainder == 0:
        return f"~{hours}hr"
    return f"~{hours}hr {remainder}min"


def format_eta(dt: Optional[datetime]) -> str:
    if not dt:
        return "TBD"
    return dt.strftime("%H:%M")


# =============================================================================
# 7. MESSAGING HELPERS
# =============================================================================

def send_text(tenant: Tenant, number: str, text: str, dedupe_key: str = ""):
    """
    Queue a WhatsApp message and return immediately — the outbox worker does the
    actual HTTP call. Callers never block on Evolution and never lose a message
    to a transient failure.

    dedupe_key, when given, makes the enqueue idempotent: if a message with that
    key was already queued or sent, this is a no-op. Use it for notifications
    that would be worse to duplicate than to skip.
    """
    if not number:
        return
    with Session(engine) as s:
        if dedupe_key:
            already = s.exec(
                select(OutboxMessage).where(OutboxMessage.dedupe_key == dedupe_key)
            ).first()
            if already:
                print(f"⏭️  [{tenant.business_name}] duplicate suppressed | {dedupe_key}")
                return
        s.add(OutboxMessage(
            tenant_id       = tenant.id,
            to_number       = number,
            body            = text,
            dedupe_key      = dedupe_key,
            # Set explicitly rather than leaning on the field default, which
            # binds now() at class-definition time.
            created_at      = now(),
            next_attempt_at = now(),
        ))
        s.commit()


async def _send_one(client: httpx.AsyncClient, tenant: Tenant, msg: OutboxMessage):
    """POST a single queued message to Evolution. Raises on failure."""
    url     = f"{tenant.evolution_api_url.rstrip('/')}/message/sendText/{tenant.evolution_instance}"
    headers = {"apikey": tenant.evolution_api_key, "Content-Type": "application/json"}
    r = await client.post(
        url,
        json={"number": msg.to_number, "text": msg.body},
        headers=headers,
        timeout=OUTBOX_SEND_TIMEOUT,
    )
    r.raise_for_status()
    print(f"📡 [{tenant.business_name}] → {msg.to_number} | {r.status_code}")


async def drain_outbox_once(client: httpx.AsyncClient) -> int:
    """
    Send one batch of due messages, oldest first. Returns how many went out.

    Messages are sent strictly in id order and one at a time: consecutive bot
    replies ("Welcome" then "Which service?") must arrive in the order they were
    queued, which concurrent sends would not guarantee. A failing message is
    marked for retry and the loop moves on, so one unreachable tenant delays
    others by at most a single timeout per pass rather than blocking forever.
    """
    sent = 0
    with Session(engine) as s:
        due = s.exec(
            select(OutboxMessage).where(
                OutboxMessage.status == "Pending",
                OutboxMessage.next_attempt_at <= now(),
            ).order_by(OutboxMessage.id).limit(OUTBOX_BATCH)
        ).all()

        for msg in due:
            tenant = s.get(Tenant, msg.tenant_id)
            msg.attempts += 1
            try:
                if not tenant:
                    raise RuntimeError(f"tenant {msg.tenant_id} no longer exists")
                await _send_one(client, tenant, msg)
                msg.status  = "Sent"
                msg.sent_at = now()
                sent += 1
            except Exception as exc:
                msg.last_error = str(exc)[:500]
                if msg.attempts >= OUTBOX_MAX_ATTEMPTS:
                    msg.status = "Failed"
                    print(f"❌ outbox giving up on msg {msg.id} after "
                          f"{msg.attempts} attempts: {exc}")
                else:
                    # Exponential backoff, capped at 5 min.
                    delay = min(300, 5 * (2 ** (msg.attempts - 1)))
                    msg.next_attempt_at = now() + timedelta(seconds=delay)
                    print(f"⚠️  outbox retry {msg.attempts}/{OUTBOX_MAX_ATTEMPTS} "
                          f"for msg {msg.id} in {delay}s: {exc}")
            s.add(msg)
        s.commit()
    return sent


async def outbox_worker():
    """Background loop draining the outbox for the life of the process."""
    print("📤 Outbox worker started")
    async with httpx.AsyncClient() as client:
        while True:
            try:
                await drain_outbox_once(client)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Never let a bad pass kill the worker — that would silently
                # stop every outbound message.
                print(f"❌ outbox worker error: {exc}")
            await asyncio.sleep(OUTBOX_POLL_SECONDS)


def send_main_menu(tenant: Tenant, number: str):
    send_text(tenant, number,
        f"*Welcome to {tenant.business_name}* 👋\n\n"
        f"What would you like to do?\n\n"
        f"1️⃣ Join the queue\n"
        f"2️⃣ My queue status\n"
        f"3️⃣ Leave the queue\n\n"
        f"Reply with *1*, *2*, or *3*"
    )


def send_service_menu(tenant: Tenant, number: str, services: list):
    lines = [
        f"{i+1}️⃣ {svc.name} ({format_duration(svc.duration_minutes)})"
        for i, svc in enumerate(services)
    ]
    send_text(tenant, number,
        f"*Which {tenant.service_label.lower()} do you need?* 💼\n\n"
        + "\n".join(lines)
        + f"\n\nReply with the *number* of your {tenant.service_label.lower()}."
    )


def get_agent_status(agent_id: int, tenant_id: int, queue_date: str):
    """
    Returns a display string for the agent's current availability:
    - 'free'             — no bookings at all
    - 'free till HH:MM'  — free now, but has an upcoming Waiting booking
    - 'busy till HH:MM'  — currently serving someone (InService)
    """
    with Session(engine) as s:
        in_service = s.exec(
            select(QueueEntry).where(
                QueueEntry.agent_id   == agent_id,
                QueueEntry.tenant_id  == tenant_id,
                QueueEntry.queue_date == queue_date,
                QueueEntry.status     == "InService"
            )
        ).first()

        if in_service:
            svc         = s.get(Service, in_service.service_id)
            duration    = svc.duration_minutes if svc else 60
            finish_time = (in_service.estimated_start or now()) + timedelta(minutes=duration)
            return f"_(busy till {format_eta(finish_time)})_"

        next_booking = s.exec(
            select(QueueEntry).where(
                QueueEntry.agent_id   == agent_id,
                QueueEntry.tenant_id  == tenant_id,
                QueueEntry.queue_date == queue_date,
                QueueEntry.status     == "Waiting"
            ).order_by(QueueEntry.estimated_start)
        ).first()

        if next_booking and next_booking.estimated_start:
            return f"_(free till {format_eta(next_booking.estimated_start)})_"

        return "_(free)_"


def send_agent_menu(tenant: Tenant, number: str,
                    agents: list, queue_date: str, service_id: int):
    lines = []
    for i, agent in enumerate(agents):
        agent_id = agent["id"] if isinstance(agent, dict) else agent.id
        name     = agent["name"] if isinstance(agent, dict) else agent.name
        status   = get_agent_status(agent_id, tenant.id, queue_date)
        lines.append(f"{i+1}️⃣ {name}  {status}")
    lines.append(f"{len(agents)+1}️⃣ No preference _(assign me to earliest)_")

    send_text(tenant, number,
        f"*Do you have a preferred {tenant.agent_label.lower()}?* 👤\n\n"
        + "\n".join(lines)
        + f"\n\nReply with a number."
    )


def queue_is_open_today(tenant: Tenant) -> bool:
    """Returns True if the queue is still accepting entries right now."""
    current_hour = now().hour
    return tenant.queue_opens <= current_hour < tenant.queue_closes


def send_date_menu(tenant: Tenant, number: str):
    today      = now().date()
    today_open = queue_is_open_today(tenant)
    options    = []

    for delta in range(14):
        d = today + timedelta(days=delta)
        # Skip today if the queue has already closed
        if delta == 0 and not today_open:
            continue
        options.append(d)
        if len(options) == tenant.advance_days + 1:
            break

    if not options:
        send_text(tenant, number,
            f"Sorry, the queue at *{tenant.business_name}* is currently closed.\n\n"
            f"We open at {tenant.queue_opens:02d}:00. Please try again tomorrow! 🙏"
        )
        return

    lines = []
    for i, d in enumerate(options):
        label = d.strftime("%a %d %b")
        if d == today:
            label += " _(today)_"
        elif d == today + timedelta(days=1):
            label += " _(tomorrow)_"
        lines.append(f"{i+1}\u0031\ufe0f\u20e3 {label}")

    set_session(tenant.id, number, {
        "state": "awaiting_date",
        "date_options": [d.isoformat() for d in options]
    })

    send_text(tenant, number,
        f"*Which day would you like to queue for?* \U0001f4c5\n\n"
        + "\n".join(lines)
        + "\n\nReply with the *number* of the day."
    )

# =============================================================================
# 8. BACKGROUND JOBS
# =============================================================================

def get_notify_number(entry) -> str:
    """Returns the number to notify. Handles walk-ins with captured phone."""
    if entry.booked_via == "walkin":
        return normalize_number(entry.customer_phone) if entry.customer_phone else ""
    return entry.customer_number


def _claim_notification(entry_id: int, flag: str) -> bool:
    """
    Atomically claim the right to send one notification for one entry.

    Flips notified_two_away / notified_next from False to True in a single
    conditional UPDATE and reports whether this caller was the one that flipped
    it. A read-then-write would let the reconciler and a live status change both
    decide to send the same message. Returns False if already claimed.
    """
    from sqlalchemy import text as sql_text
    if flag not in ("notified_two_away", "notified_next"):
        raise ValueError(f"unknown notification flag {flag!r}")
    with Session(engine) as s:
        result = s.execute(
            sql_text(
                f"UPDATE queueentry SET {flag} = :yes "
                f"WHERE id = :eid AND {flag} = :no"
            ),
            {"eid": entry_id, "yes": True, "no": False},
        )
        s.commit()
        return result.rowcount == 1


def _next_waiting(s: Session, tenant_id: int, agent_id: int, queue_date: str):
    """The entry an agent will serve next, by ETA then arrival order."""
    return s.exec(
        select(QueueEntry).where(
            QueueEntry.agent_id   == agent_id,
            QueueEntry.tenant_id  == tenant_id,
            QueueEntry.queue_date == queue_date,
            QueueEntry.status     == "Waiting",
        ).order_by(QueueEntry.estimated_start, QueueEntry.joined_at)
    ).first()


def _fire_15min_warning(tenant_id: int, agent_id: int, queue_date: str):
    """Warns the next waiter that they're roughly 15 minutes out."""
    with Session(engine) as s:
        tenant = s.get(Tenant, tenant_id)
        if not tenant:
            return
        next_entry = _next_waiting(s, tenant_id, agent_id, queue_date)
        if not next_entry or next_entry.notified_two_away:
            return
        notify_to = get_notify_number(next_entry)
        if not notify_to:
            return
        entry_id = next_entry.id
        agent   = s.get(Agent, next_entry.agent_id)
        service = s.get(Service, next_entry.service_id)
        body = (
            f"⏳ *Almost your turn at {tenant.business_name}!*\n\n"
            f"You're up in about *15 minutes*.\n"
            f"\U0001f4bc {service.name if service else ''} with {agent.name if agent else tenant.agent_label}\n\n"
            f"Start making your way over \U0001f6b6"
        )
    # Claim before queueing, so a concurrent caller cannot queue it too.
    if not _claim_notification(entry_id, "notified_two_away"):
        return
    send_text(tenant, notify_to, body, dedupe_key=f"two_away:{entry_id}")


def reconcile_notifications() -> int:
    """
    Re-derive which notifications are due, from live queue state.

    Runs every RECONCILE_SECONDS. For every agent currently serving someone, if
    that service is within 15 minutes of finishing, the next waiter gets their
    warning. Because this reads state rather than replaying a schedule, a
    restart, a crash or a missed tick cannot lose a notification — worst case it
    goes out one tick late. That is what makes the warning durable: there is
    deliberately no persisted timer to lose.

    Returns how many warnings it fired, for logging and tests.
    """
    due = []
    with Session(engine) as s:
        in_service = s.exec(
            select(QueueEntry).where(
                QueueEntry.status     == "InService",
                QueueEntry.queue_date >= yesterday_str(),
            )
        ).all()
        for entry in in_service:
            svc      = s.get(Service, entry.service_id)
            duration = svc.duration_minutes if svc else 60
            start    = entry.estimated_start or entry.joined_at
            if now() >= start + timedelta(minutes=duration - 15):
                due.append((entry.tenant_id, entry.agent_id, entry.queue_date))

    fired = 0
    for tenant_id, agent_id, queue_date in due:
        _fire_15min_warning(tenant_id, agent_id, queue_date)
        fired += 1
    return fired


def _fire_youre_next(tenant_id: int, agent_id: int, queue_date: str):
    """
    Called when an entry is marked Done / NoShow / Cancelled — tells the next
    waiter they're up, immediately. There is no scheduled 15-minute job to
    cancel any more: reconcile_notifications derives that warning from live
    state, and once this entry is claimed the warning is suppressed by the same
    notified_two_away flag.
    """
    with Session(engine) as s:
        tenant = s.get(Tenant, tenant_id)
        if not tenant:
            return
        next_entry = _next_waiting(s, tenant_id, agent_id, queue_date)
        if not next_entry or next_entry.notified_next:
            return
        notify_to = get_notify_number(next_entry)
        if not notify_to:
            return
        entry_id = next_entry.id
        agent   = s.get(Agent, next_entry.agent_id)
        service = s.get(Service, next_entry.service_id)
        body = (
            f"\U0001f680 *You're up next at {tenant.business_name}!*\n\n"
            f"Head over now — {agent.name if agent else tenant.agent_label} is ready for you.\n"
            f"\U0001f4bc {service.name if service else ''}"
        )
    if not _claim_notification(entry_id, "notified_next"):
        return
    # Being told "you're up next" makes the 15-minute warning redundant.
    _claim_notification(entry_id, "notified_two_away")
    send_text(tenant, notify_to, body, dedupe_key=f"youre_next:{entry_id}")

async def midnight_reset_job():
    """Runs at 00:01 every night. Closes out yesterday's queue."""
    print("🌙 Running midnight reset...")
    yesterday = yesterday_str()

    with Session(engine) as s:
        leftover = s.exec(
            select(QueueEntry).where(
                QueueEntry.queue_date == yesterday,
                QueueEntry.status.in_(["Waiting", "InService"])
            )
        ).all()

        for entry in leftover:
            # Never auto-mark as Done — staff never closed it, so it doesn't
            # count as completed work. Abandoned entries close as NoShow.
            entry.status      = "NoShow"
            # Tagged as ours, not the customer's. Reporting keeps these out of
            # the no-show rate: nobody knows whether this customer turned up,
            # only that the shop never closed the entry.
            entry.closed_by   = "system"
            entry.finished_at = now()
            s.add(entry)

        s.commit()
    print(f"🌙 Reset complete. {len(leftover)} entries closed.")


# =============================================================================
# 9. WEBHOOK HANDLER
# =============================================================================

def _do_assign(tenant, customer_num: str, customer_name: str,
               assigned_agent_id: int, service_id: int,
               queue_date: str, sess: dict,
               include_parent: bool = True,
               children_names: list = None,
               earliest_arrival: Optional[datetime] = None,
               customer_chose_agent: bool = True):
    """
    Saves queue entries and sends confirmation.
    include_parent=False means only child entries are created (parent is just escorting).
    children_names is a list of child name strings; each gets its own QueueEntry.
    Shared by single-agent auto-assign and manual agent pick paths.

    customer_chose_agent=False means assigned_agent_id was auto-picked rather
    than requested by name, so it may be re-resolved under the booking lock.
    Defaults True — never silently override a choice the customer made.
    """
    if children_names is None:
        children_names = []

    # Serialise assignment + insert. Without the lock two customers confirming
    # at the same instant both read the pre-insert backlogs and land on the
    # same agent.
    with booking_lock(tenant.id, queue_date):
        # Re-resolve the agent inside the lock unless the customer explicitly
        # picked one. The auto-pick made before the arrival-time prompt is a
        # whole round trip stale, and two customers prompted at the same moment
        # were handed the same agent. Re-resolving can only land on an agent
        # whose backlog is <= the one already quoted, so the ETA the customer
        # was shown still holds.
        if not customer_chose_agent:
            assigned_agent_id = assign_agent(
                tenant, service_id, None, queue_date
            ) or assigned_agent_id

        backlog  = get_agent_backlog_minutes(assigned_agent_id, tenant.id, queue_date)
        eta      = calculate_estimated_start(tenant, assigned_agent_id, queue_date, backlog, earliest_arrival)

        print(f"\U0001f4be Saving entry | tenant={tenant.id} service={service_id} agent={assigned_agent_id} date={queue_date} parent={include_parent} children={children_names}")

        child_entries = []
        parent_entry  = None
        agent         = None
        saved_ids     = []  # track all saved entry IDs for rollback
        try:
            with Session(engine) as s:
                total_waiting = len(s.exec(
                    select(QueueEntry).where(
                        QueueEntry.tenant_id  == tenant.id,
                        QueueEntry.queue_date == queue_date,
                        QueueEntry.status     == "Waiting"
                    )
                ).all())
                next_position = total_waiting + 1

                if include_parent:
                    parent_entry = QueueEntry(
                        tenant_id          = tenant.id,
                        service_id         = service_id,
                        agent_id           = assigned_agent_id,
                        customer_number    = customer_num,
                        customer_name      = customer_name,
                        queue_date         = queue_date,
                        estimated_start    = eta,
                        earliest_arrival   = earliest_arrival,
                        position           = next_position,
                        booked_via         = "whatsapp"
                    )
                    # If this customer finishes before a later appointment is even
                    # due, slot them into that idle gap instead of the queue tail.
                    insert_at = find_walkin_insert_joined_at(
                        assigned_agent_id, tenant.id, tenant, queue_date,
                        service_id, new_arrival=earliest_arrival
                    )
                    if insert_at:
                        parent_entry.joined_at = insert_at
                    s.add(parent_entry)
                    s.commit()
                    s.refresh(parent_entry)
                    saved_ids.append(parent_entry.id)
                    next_position += 1

                # Party root for linkage. With a parent it's the parent; for a
                # children-only booking the first child becomes the root and the
                # rest link to it, so cancel_party can find the whole party.
                party_root_id = parent_entry.id if parent_entry else None
                for child_name in children_names:
                    # Assign each child independently so free agents are used and
                    # backlogs are accurate after each commit
                    child_agent_id = assign_agent(tenant, service_id, None, queue_date) or assigned_agent_id
                    child_backlog  = get_agent_backlog_minutes(child_agent_id, tenant.id, queue_date)
                    child_eta      = calculate_estimated_start(tenant, child_agent_id, queue_date, child_backlog, earliest_arrival)
                    child_entry = QueueEntry(
                        tenant_id          = tenant.id,
                        service_id         = service_id,
                        agent_id           = child_agent_id,
                        customer_number    = customer_num,
                        customer_name      = child_name,
                        queue_date         = queue_date,
                        estimated_start    = child_eta,
                        position           = next_position,
                        booked_via         = "whatsapp",
                        parent_entry_id    = party_root_id,
                        earliest_arrival   = earliest_arrival,
                    )
                    child_insert_at = find_walkin_insert_joined_at(
                        child_agent_id, tenant.id, tenant, queue_date,
                        service_id, new_arrival=earliest_arrival
                    )
                    if child_insert_at:
                        child_entry.joined_at = child_insert_at
                    s.add(child_entry)
                    s.commit()  # commit before next child so backlog recalculates correctly
                    s.refresh(child_entry)
                    saved_ids.append(child_entry.id)
                    # First child in a parentless party becomes the root for siblings
                    if party_root_id is None:
                        party_root_id = child_entry.id
                    child_entries.append((child_name, next_position, child_agent_id, child_eta))
                    next_position += 1

                agent = s.get(Agent, assigned_agent_id)

        except Exception as exc:
            print(f"❌ _do_assign failed — rolling back {len(saved_ids)} entries: {exc}")
            if saved_ids:
                with Session(engine) as s:
                    for eid in saved_ids:
                        row = s.get(QueueEntry, eid)
                        if row:
                            s.delete(row)
                    s.commit()
            send_text(tenant, customer_num,
                "⚠️ Something went wrong while saving your booking. Please try again or contact the shop directly."
            )
            clear_session(tenant.id, customer_num)
            return

        # Recalculate ETAs for every agent touched by this booking
        agents_to_recalc = {assigned_agent_id}
        for _, _, child_agent_id, _ in child_entries:
            agents_to_recalc.add(child_agent_id)
        for aid in agents_to_recalc:
            recalculate_queue(tenant.id, aid, queue_date)

    with Session(engine) as s:
        service = s.get(Service, service_id)

    clear_session(tenant.id, customer_num)

    date_display = datetime.strptime(queue_date, "%Y-%m-%d").strftime("%a %d %b")

    # Build position summary lines
    position_lines = ""
    if include_parent:
        position_lines += (
            f"\U0001f4cd *Your position:* #{parent_entry.position} "
            f"| {tenant.agent_label}: {agent.name if agent else 'TBD'} "
            f"| \u23f0 {format_eta(eta)}\n"
        )
    for child_name, child_pos, child_agent_id, child_eta in child_entries:
        with Session(engine) as cs:
            child_agent = cs.get(Agent, child_agent_id)
        position_lines += (
            f"\U0001f476 *{child_name}:* #{child_pos} "
            f"| {tenant.agent_label}: {child_agent.name if child_agent else 'TBD'} "
            f"| \u23f0 {format_eta(child_eta)}\n"
        )

    # Notification promise is based on the first queued position
    first_position = parent_entry.position if include_parent else (child_entries[0][1] if child_entries else 1)
    if first_position == 1 and queue_date == today_str():
        notify_line = "You\'re first in line \U0001f3c6 — head over when you\'re ready."
    elif first_position == 1:
        notify_line = f"You\'re first in line for {date_display} \U0001f3c6 — we\'ll notify you on the day."
    elif first_position == 2:
        notify_line = "We\'ll notify you when you\'re up next."
    else:
        notify_line = f"We\'ll notify you when you\'re 2 away and when you\'re next."

    send_text(tenant, customer_num,
        f"\u2705 *You\'re in the queue!*\n\n"
        f"{position_lines}"
        f"\U0001f4bc {tenant.service_label}: {service.name if service else 'TBD'}\n"
        f"\U0001f4c5 Date: {date_display}\n\n"
        f"{notify_line}\n"
        f"Reply *status* anytime to check your position."
    )

    # Notify owner (report the first booked person)
    report_name = customer_name if include_parent else (children_names[0] if children_names else customer_name)
    report_position = parent_entry.position if include_parent else (child_entries[0][1] if child_entries else 1)
    report_eta = eta if include_parent else (child_entries[0][3] if child_entries else eta)
    # Report the agent actually assigned to the reported person (children may
    # land on a different agent than the parent's assigned_agent_id).
    if include_parent:
        report_agent_name = agent.name if agent else ""
    elif child_entries:
        with Session(engine) as ras:
            ra = ras.get(Agent, child_entries[0][2])
        report_agent_name = ra.name if ra else ""
    else:
        report_agent_name = agent.name if agent else ""
    if tenant.owner_number:
        send_text(tenant, normalize_number(tenant.owner_number),
            f"\U0001f514 *New Queue Entry*\n\n"
            f"\U0001f464 {report_name}\n"
            f"\U0001f4bc {service.name if service else ''}\n"
            f"\U0001f477 {report_agent_name}\n"
            f"\U0001f4cd Position #{report_position} | \u23f0 {format_eta(report_eta)}"
        )


@app.post("/webhook", dependencies=[Depends(verify_webhook_secret)])
async def handle_webhook(request: Request):
    data = await request.json()

    if data.get("event") != "messages.upsert":
        return {"status": "ignored"}

    msg_data = data.get("data", {})
    if msg_data.get("key", {}).get("fromMe"):
        return {"status": "ignored"}

    # Idempotency — Evolution retries deliver the same message id more than once.
    # Without this, a retried "1"/"now" reply creates duplicate queue entries.
    msg_id = msg_data.get("key", {}).get("id")
    if msg_id:
        if not redis_client.set(f"seen:{msg_id}", "1", nx=True, ex=600):
            return {"status": "duplicate"}

    tenant = get_tenant_by_number(data.get("sender", ""))
    if not tenant:
        return {"status": "unknown_tenant"}

    customer_num  = msg_data["key"]["remoteJid"]
    customer_name = msg_data.get("pushName", "Customer")
    message_obj   = msg_data.get("message", {})
    raw_text = (
        message_obj.get("conversation")
        or message_obj.get("extendedTextMessage", {}).get("text")
        or ""
    ).strip()
    text = raw_text.lower()

    sess  = get_session(tenant.id, customer_num)
    state = sess.get("state", "idle")

    print(f"\U0001f4e9 [{tenant.business_name}] {customer_num} | {state} | \'{text}\'")

    # ── GLOBAL TRIGGERS (work from any state) ─────────────────────────────
    if any(w in text for w in ["menu","start","hi","hello","hey"]) and state not in ["awaiting_booking_for", "awaiting_children", "awaiting_children_names", "awaiting_arrival_time"]:
        set_session(tenant.id, customer_num, {"state": "main_menu"})
        send_main_menu(tenant, customer_num)
        return {"status": "success"}

    # ── BACK HANDLER ──────────────────────────────────────────────────────
    if text == "0":
        if state == "awaiting_date":
            set_session(tenant.id, customer_num, {"state": "main_menu"})
            send_main_menu(tenant, customer_num)
        elif state == "awaiting_booking_for":
            # Back from "who for" → back to date picker or main menu
            date_options = sess.get("date_options")
            queue_date   = sess.get("pending_queue_date", today_str())
            if date_options:
                set_session(tenant.id, customer_num, {"state": "awaiting_date", "date_options": date_options})
                today_d = now().date()
                lines_out = []
                for i, d_str in enumerate(date_options):
                    d = datetime.strptime(d_str, "%Y-%m-%d").date()
                    label = d.strftime("%a %d %b")
                    if d == today_d:       label += " _(today)_"
                    elif d == today_d + timedelta(days=1): label += " _(tomorrow)_"
                    lines_out.append(f"{i+1}\u0031\ufe0f\u20e3 {label}")
                send_text(tenant, customer_num,
                    "*Which day would you like to queue for?* \U0001f4c5\n\n"
                    + "\n".join(lines_out)
                    + "\n\nReply with the *number* of the day, or *0* to go back."
                )
            else:
                set_session(tenant.id, customer_num, {"state": "main_menu"})
                send_main_menu(tenant, customer_num)
        elif state == "awaiting_service":
            # Back from service → back to "who for"
            queue_date   = sess.get("pending_queue_date", today_str())
            date_options = sess.get("date_options")
            set_session(tenant.id, customer_num, {
                "state":              "awaiting_booking_for",
                "pending_queue_date": queue_date,
                "date_options":       date_options,
            })
            send_text(tenant, customer_num,
                "Who are you booking for?\n"
                "1\ufe0f\u20e3 Just me\n"
                "2\ufe0f\u20e3 Me and my children\n"
                "3\ufe0f\u20e3 My children only\n\n"
                "Reply *1*, *2*, or *3*"
            )
        elif state == "awaiting_agent":
            # Back from agent → back to service menu
            queue_date   = sess.get("queue_date", today_str())
            service_ids  = sess.get("service_ids", [])
            if service_ids:
                set_session(tenant.id, customer_num, {
                    "state":              "awaiting_service",
                    "pending_queue_date": queue_date,
                    "service_ids":        service_ids,
                    "date_options":       sess.get("date_options"),
                    "include_parent":     sess.get("include_parent", True),
                    "children_collected": sess.get("children_collected", []),
                })
                with Session(engine) as s:
                    services = s.exec(select(Service).where(Service.id.in_(service_ids))).all()
                send_service_menu(tenant, customer_num, services)
            else:
                set_session(tenant.id, customer_num, {"state": "main_menu"})
                send_main_menu(tenant, customer_num)
        elif state in ("awaiting_children", "awaiting_children_names", "awaiting_arrival_time"):
            # Nothing saved yet — just cancel and return to main menu
            clear_session(tenant.id, customer_num)
            send_text(tenant, customer_num,
                "\u274c Booking cancelled.\n\nReply *menu* to start again."
            )
        elif state == "awaiting_rebook":
            set_session(tenant.id, customer_num, {"state": "main_menu"})
            send_main_menu(tenant, customer_num)
        else:
            set_session(tenant.id, customer_num, {"state": "main_menu"})
            send_main_menu(tenant, customer_num)
        return {"status": "success"}

    # ── MAIN MENU ─────────────────────────────────────────────────────────
    if state in ["idle", "main_menu"]:
        if state == "idle":
            set_session(tenant.id, customer_num, {"state": "main_menu"})
            send_main_menu(tenant, customer_num)
            return {"status": "success"}

        if text == "1":
            # Check if already in queue
            with Session(engine) as s:
                existing = s.exec(
                    select(QueueEntry).where(
                        QueueEntry.tenant_id      == tenant.id,
                        QueueEntry.customer_number == customer_num,
                        QueueEntry.queue_date      >= today_str(),
                        QueueEntry.status.in_(["Waiting", "InService"])
                    ).order_by(QueueEntry.queue_date, QueueEntry.joined_at)
                ).first()

            if existing:
                with Session(engine) as s:
                    agent   = s.get(Agent, existing.agent_id)
                    service = s.get(Service, existing.service_id)
                ahead_count = 0
                with Session(engine) as s:
                    ahead_count = len(s.exec(
                        select(QueueEntry).where(
                            QueueEntry.agent_id   == existing.agent_id,
                            QueueEntry.tenant_id  == tenant.id,
                            QueueEntry.queue_date == existing.queue_date,
                            QueueEntry.status     == "Waiting",
                            QueueEntry.position   < existing.position
                        )
                    ).all())
                set_session(tenant.id, customer_num, {
                    "state": "awaiting_rebook",
                    "existing_entry_id": existing.id,
                })
                existing_day = (
                    "today" if existing.queue_date == today_str()
                    else datetime.strptime(existing.queue_date, "%Y-%m-%d").strftime("%a %d %b")
                )
                send_text(tenant, customer_num,
                    f"You\'re already in the queue for {existing_day}!\n\n"
                    f"\U0001f4cd Position: #{existing.position}\n"
                    f"\U0001f464 {tenant.agent_label}: {agent.name if agent else 'TBD'}\n"
                    f"\U0001f4bc {tenant.service_label}: {service.name if service else 'TBD'}\n"
                    f"\u23f0 ETA: {format_eta(existing.estimated_start)}\n"
                    f"\U0001f465 People ahead: {ahead_count}\n\n"
                    f"Would you like to:\n"
                    f"1\ufe0f\u20e3 Keep my spot\n"
                    f"2\ufe0f\u20e3 Cancel and rebook for something else\n\n"
                    f"Reply *1* or *2*"
                )
                return {"status": "success"}

            # Queue closed check
            if tenant.advance_days == 0 and not queue_is_open_today(tenant):
                send_text(tenant, customer_num,
                    f"Sorry, the queue at *{tenant.business_name}* is currently closed.\n\n"
                    f"We open at {tenant.queue_opens:02d}:00. Please try again tomorrow! \U0001f64f"
                )
                clear_session(tenant.id, customer_num)
                return {"status": "success"}

            # Start booking flow — ask who first, then service
            if tenant.advance_days == 0:
                set_session(tenant.id, customer_num, {
                    "state": "awaiting_booking_for",
                    "pending_queue_date": today_str(),
                })
                send_text(tenant, customer_num,
                    "Who are you booking for?\n"
                    "1\ufe0f\u20e3 Just me\n"
                    "2\ufe0f\u20e3 Me and my children\n"
                    "3\ufe0f\u20e3 My children only\n\n"
                    "Reply *1*, *2*, or *3*"
                )
            else:
                send_date_menu(tenant, customer_num)

        elif text == "2":
            today = today_str()
            with Session(engine) as s:
                entries = s.exec(
                    select(QueueEntry).where(
                        QueueEntry.tenant_id       == tenant.id,
                        QueueEntry.customer_number == customer_num,
                        QueueEntry.queue_date      >= today,
                        QueueEntry.status.in_(["Waiting", "InService"])
                    ).order_by(QueueEntry.queue_date, QueueEntry.position)
                ).all()

                if not entries:
                    send_text(tenant, customer_num, "You\'re not currently in the queue.\n\nReply *menu* to join.")
                else:
                    agent   = s.get(Agent, entries[0].agent_id)
                    service = s.get(Service, entries[0].service_id)
                    ahead   = s.exec(
                        select(QueueEntry).where(
                            QueueEntry.agent_id   == entries[0].agent_id,
                            QueueEntry.tenant_id  == tenant.id,
                            QueueEntry.queue_date == entries[0].queue_date,
                            QueueEntry.status     == "Waiting",
                            QueueEntry.position   < entries[0].position
                        )
                    ).all()
                    ahead_count = len(ahead)

                    # Build position lines for all people in this booking
                    position_lines = ""
                    for e in entries:
                        position_lines += f"\U0001f522 *{e.customer_name}:* #{e.position}\n"

                    send_text(tenant, customer_num,
                        f"*Your Queue Status* \U0001f4cd\n\n"
                        f"{position_lines}"
                        f"\U0001f464 {tenant.agent_label}: {agent.name if agent else 'TBD'}\n"
                        f"\U0001f4bc {tenant.service_label}: {service.name if service else 'TBD'}\n"
                        f"\u23f0 Estimated time: {format_eta(entries[0].estimated_start)}\n"
                        f"\U0001f465 People ahead: {ahead_count}\n\n"
                        f"Reply *menu* for more options."
                    )
            clear_session(tenant.id, customer_num)

        elif text == "3":
            today = today_str()
            with Session(engine) as s:
                entry = s.exec(
                    select(QueueEntry).where(
                        QueueEntry.tenant_id      == tenant.id,
                        QueueEntry.customer_number == customer_num,
                        QueueEntry.queue_date      >= today,
                        QueueEntry.status          == "Waiting"
                    ).order_by(QueueEntry.queue_date, QueueEntry.joined_at)
                ).first()

                if not entry:
                    send_text(tenant, customer_num, "You\'re not currently in the queue.\n\nReply *menu* to go back.")
                else:
                    entry_id     = entry.id
                    party_date   = entry.queue_date
                    cust_name    = entry.customer_name
                    svc          = s.get(Service, entry.service_id)
                    svc_name     = svc.name if svc else ""
                    date_display = datetime.strptime(party_date, "%Y-%m-%d").strftime("%a %d %b")
            # Cancel the whole party (parent + children), then recalc each agent touched
            if entry:
                touched = cancel_party(tenant.id, entry_id)
                for aid in touched:
                    recalculate_queue(tenant.id, aid, party_date)
                send_text(tenant, customer_num,
                    f"\u2705 You\'ve been removed from the queue at *{tenant.business_name}*.\n\n"
                    f"Reply *menu* anytime to rejoin."
                )
                if tenant.owner_number:
                    send_text(tenant, normalize_number(tenant.owner_number),
                        f"\u274c *Queue Cancellation*\n\n"
                        f"\U0001f464 {cust_name}\n"
                        f"\U0001f4bc {svc_name}\n"
                        f"\U0001f4c5 {date_display}"
                    )
            clear_session(tenant.id, customer_num)

        else:
            send_text(tenant, customer_num, "Please reply with *1*, *2*, or *3*.")

        return {"status": "success"}

    # ── REBOOK CONFIRMATION ───────────────────────────────────────────────
    if state == "awaiting_rebook":
        existing_entry_id = sess.get("existing_entry_id")
        if text == "1":
            # Keep spot
            clear_session(tenant.id, customer_num)
            send_text(tenant, customer_num, "\U0001f44d Got it — your spot is safe! Reply *status* to check your position.")
        elif text == "2":
            # Cancel and rebook — cancel the whole party (parent + children)
            if existing_entry_id:
                with Session(engine) as s:
                    entry = s.get(QueueEntry, existing_entry_id)
                    party_date = entry.queue_date if entry else None
                if party_date:
                    touched = cancel_party(tenant.id, existing_entry_id)
                    for aid in touched:
                        recalculate_queue(tenant.id, aid, party_date)
            # Start fresh booking flow — ask who first, then service
            if tenant.advance_days == 0:
                set_session(tenant.id, customer_num, {
                    "state": "awaiting_booking_for",
                    "pending_queue_date": today_str(),
                })
                send_text(tenant, customer_num,
                    "Who are you booking for?\n"
                    "1\ufe0f\u20e3 Just me\n"
                    "2\ufe0f\u20e3 Me and my children\n"
                    "3\ufe0f\u20e3 My children only\n\n"
                    "Reply *1*, *2*, or *3*"
                )
            else:
                send_date_menu(tenant, customer_num)
        else:
            send_text(tenant, customer_num, "Please reply *1* to keep your spot or *2* to cancel and rebook.")
        return {"status": "success"}

    # ── DATE SELECTION ────────────────────────────────────────────────────
    if state == "awaiting_date":
        date_options = sess.get("date_options", [])
        if text == "0":
            set_session(tenant.id, customer_num, {"state": "main_menu"})
            send_main_menu(tenant, customer_num)
            return {"status": "success"}
        if text.isdigit() and 1 <= int(text) <= len(date_options):
            chosen_date = date_options[int(text) - 1]
            set_session(tenant.id, customer_num, {
                "state": "awaiting_booking_for",
                "pending_queue_date": chosen_date,
                "date_options": date_options,
            })
            send_text(tenant, customer_num,
                "Who are you booking for?\n"
                "1\ufe0f\u20e3 Just me\n"
                "2\ufe0f\u20e3 Me and my children\n"
                "3\ufe0f\u20e3 My children only\n\n"
                "Reply *1*, *2*, or *3*"
            )
        else:
            send_text(tenant, customer_num,
                f"Please reply with a number between 1 and {len(date_options)}, or *0* to go back.")
        return {"status": "success"}

    # ── SERVICE SELECTION ─────────────────────────────────────────────────
    if state == "awaiting_service":
        service_ids        = sess.get("service_ids", [])
        queue_date         = sess.get("pending_queue_date", today_str())
        include_parent     = sess.get("include_parent", True)
        children_collected = sess.get("children_collected", [])
        date_options       = sess.get("date_options")

        if text == "0":
            # Back → go to "who for" question (not date, since date is already known)
            set_session(tenant.id, customer_num, {
                "state":              "awaiting_booking_for",
                "pending_queue_date": queue_date,
                "date_options":       date_options,
            })
            send_text(tenant, customer_num,
                "Who are you booking for?\n"
                "1\ufe0f\u20e3 Just me\n"
                "2\ufe0f\u20e3 Me and my children\n"
                "3\ufe0f\u20e3 My children only\n\n"
                "Reply *1*, *2*, or *3*"
            )
            return {"status": "success"}

        if text.isdigit() and 1 <= int(text) <= len(service_ids):
            chosen_service_id  = service_ids[int(text) - 1]
            include_parent     = sess.get("include_parent", True)
            children_collected = sess.get("children_collected", [])
            with Session(engine) as s:
                tenant_agent_ids = [
                    a.id for a in s.exec(
                        select(Agent).where(Agent.tenant_id == tenant.id, Agent.is_active == True)
                    ).all()
                ]
                capable_ids = [
                    row.agent_id for row in s.exec(
                        select(AgentService).where(
                            AgentService.service_id == chosen_service_id,
                            AgentService.agent_id.in_(tenant_agent_ids)
                        )
                    ).all()
                ]
                agent_rows = s.exec(
                    select(Agent).where(Agent.id.in_(capable_ids), Agent.is_active == True)
                ).all()
                agents = [{"id": a.id, "name": a.name} for a in agent_rows]

            if not agents:
                send_text(tenant, customer_num,
                    f"Sorry, no {tenant.agent_label.lower()}s available for that service.\n\nReply *0* to go back.")
            elif len(agents) == 1:
                # Only one agent — skip agent menu, go straight to arrival time
                assigned_agent_id = agents[0]["id"]
                _backlog = get_agent_backlog_minutes(assigned_agent_id, tenant.id, queue_date)
                _eta     = calculate_estimated_start(tenant, assigned_agent_id, queue_date, _backlog)
                set_session(tenant.id, customer_num, {
                    "state":              "awaiting_arrival_time",
                    "pending_agent_id":   assigned_agent_id,
                    "pending_service_id": chosen_service_id,
                    "pending_queue_date": queue_date,
                    "include_parent":     include_parent,
                    "children_collected": children_collected,
                })
                send_text(tenant, customer_num,
                    f"\u23f0 Your {tenant.agent_label.lower()} is available around *{format_eta(_eta)}*.\n\n"
                    f"What time do you think you'll arrive?\n"
                    f"Reply with a time like *{format_eta(_eta)}* or *now* if you're already on your way."
                )
            else:
                set_session(tenant.id, customer_num, {
                    "state":              "awaiting_agent",
                    "queue_date":         queue_date,
                    "service_id":         chosen_service_id,
                    "agent_ids":          [a["id"] for a in agents],
                    "service_ids":        service_ids,
                    "date_options":       sess.get("date_options"),
                    "include_parent":     include_parent,
                    "children_collected": children_collected,
                })
                send_agent_menu(tenant, customer_num, agents, queue_date, chosen_service_id)
        else:
            send_text(tenant, customer_num,
                f"Please reply with a number between 1 and {len(service_ids)}, or *0* to go back.")
        return {"status": "success"}

    # ── AGENT SELECTION ───────────────────────────────────────────────────
    if state == "awaiting_agent":
        agent_ids          = sess.get("agent_ids", [])
        service_id         = sess.get("service_id")
        queue_date         = sess.get("queue_date", today_str())
        include_parent     = sess.get("include_parent", True)
        children_collected = sess.get("children_collected", [])
        no_pref_idx        = len(agent_ids) + 1

        if text == "0":
            service_ids = sess.get("service_ids", [])
            set_session(tenant.id, customer_num, {
                "state":              "awaiting_service",
                "pending_queue_date": queue_date,
                "service_ids":        service_ids,
                "date_options":       date_options,
                "include_parent":     include_parent,
                "children_collected": children_collected,
            })
            with Session(engine) as s:
                svc_objs = s.exec(select(Service).where(Service.id.in_(service_ids))).all()
            send_service_menu(tenant, customer_num, svc_objs)
            return {"status": "success"}

        if text.isdigit():
            choice = int(text)
            preferred_agent_id = None
            if 1 <= choice <= len(agent_ids):
                preferred_agent_id = agent_ids[choice - 1]
            elif choice != no_pref_idx:
                send_text(tenant, customer_num,
                    f"Please reply with a number between 1 and {no_pref_idx}, or *0* to go back.")
                return {"status": "success"}

            assigned_agent_id = assign_agent(tenant, service_id, preferred_agent_id, queue_date)
            if not assigned_agent_id:
                # Also reachable when everyone is simply off that day, so name
                # the date — another one may well work.
                day_name = datetime.strptime(queue_date, "%Y-%m-%d").strftime("%a %d %b")
                send_text(tenant, customer_num,
                    f"Sorry, no {tenant.agent_label.lower()}s are available on "
                    f"{day_name}.\n\nReply *0* to go back and try another day.")
                return {"status": "success"}

            _backlog = get_agent_backlog_minutes(assigned_agent_id, tenant.id, queue_date)
            _eta     = calculate_estimated_start(tenant, assigned_agent_id, queue_date, _backlog)
            set_session(tenant.id, customer_num, {
                "state":              "awaiting_arrival_time",
                "pending_agent_id":   assigned_agent_id,
                # Remember whether this was their pick or ours. The arrival-time
                # reply comes back a round trip later, by which point an
                # auto-pick may be stale — _do_assign redoes it under the lock.
                "pending_agent_explicit": preferred_agent_id is not None,
                "pending_service_id": service_id,
                "pending_queue_date": queue_date,
                "include_parent":     include_parent,
                "children_collected": children_collected,
            })
            send_text(tenant, customer_num,
                f"\u23f0 Your {tenant.agent_label.lower()} is available around *{format_eta(_eta)}*.\n\n"
                f"What time do you think you'll arrive?\n"
                f"Reply with a time like *{format_eta(_eta)}* or *now* if you're already on your way."
            )
        else:
            send_text(tenant, customer_num,
                f"Please reply with a number, or *0* to go back.")
        return {"status": "success"}

    # ── WHO ARE WE BOOKING FOR? ────────────────────────────────────────────
    if state == "awaiting_booking_for":
        pending_queue_date = sess.get("pending_queue_date", today_str())
        date_options       = sess.get("date_options")

        if text == "1":
            include_parent = True
            children_collected = []
        elif text in ("2", "3"):
            include_parent = (text == "2")
            set_session(tenant.id, customer_num, {
                "state":              "awaiting_children",
                "pending_queue_date": pending_queue_date,
                "date_options":       date_options,
                "include_parent":     include_parent,
            })
            send_text(tenant, customer_num,
                "How many children are you booking for?\n"
                "1\ufe0f\u20e3 1 child\n"
                "2\ufe0f\u20e3 2 children\n\n"
                "Reply *1* or *2*"
            )
            return {"status": "success"}
        else:
            send_text(tenant, customer_num,
                "Please reply *1* (just me), *2* (me + children), or *3* (children only)."
            )
            return {"status": "success"}

        # text == "1" path: go straight to service selection
        with Session(engine) as s:
            svc_objs = s.exec(
                select(Service).where(Service.tenant_id == tenant.id, Service.is_active == True)
            ).all()
            svc_list = [{"id": sv.id, "name": sv.name} for sv in svc_objs]
        if not svc_list:
            send_text(tenant, customer_num, f"No services configured. Contact {tenant.business_name} directly.")
            clear_session(tenant.id, customer_num)
        else:
            set_session(tenant.id, customer_num, {
                "state":              "awaiting_service",
                "pending_queue_date": pending_queue_date,
                "date_options":       date_options,
                "service_ids":        [sv["id"] for sv in svc_list],
                "include_parent":     include_parent,
                "children_collected": children_collected,
            })
            with Session(engine) as s:
                svc_objs = s.exec(select(Service).where(Service.id.in_([sv["id"] for sv in svc_list]))).all()
            send_service_menu(tenant, customer_num, svc_objs)
        return {"status": "success"}

    # ── HOW MANY CHILDREN? ────────────────────────────────────────────────
    if state == "awaiting_children":
        pending_queue_date = sess.get("pending_queue_date", today_str())
        date_options       = sess.get("date_options")
        include_parent     = sess.get("include_parent", True)

        if text.isdigit() and 1 <= int(text) <= 2:
            count = int(text)
            set_session(tenant.id, customer_num, {
                "state":              "awaiting_children_names",
                "pending_queue_date": pending_queue_date,
                "date_options":       date_options,
                "include_parent":     include_parent,
                "children_count":     count,
                "children_collected": [],
            })
            send_text(tenant, customer_num,
                f"Please send the name of child 1 of {count}:"
            )
        else:
            send_text(tenant, customer_num,
                "Reply *1* for 1 child or *2* for 2 children."
            )
        return {"status": "success"}

    # ── COLLECTING CHILDREN NAMES ─────────────────────────────────────────
    if state == "awaiting_children_names":
        pending_queue_date = sess.get("pending_queue_date", today_str())
        date_options       = sess.get("date_options")
        include_parent     = sess.get("include_parent", True)
        count              = sess.get("children_count", 1)
        collected          = sess.get("children_collected", [])
        collected.append(raw_text.strip())

        if len(collected) < count:
            set_session(tenant.id, customer_num, {
                "state":              "awaiting_children_names",
                "pending_queue_date": pending_queue_date,
                "date_options":       date_options,
                "include_parent":     include_parent,
                "children_count":     count,
                "children_collected": collected,
            })
            send_text(tenant, customer_num,
                f"Name of child {len(collected) + 1} of {count}:"
            )
        else:
            # All names collected — now go to service selection
            with Session(engine) as s:
                svc_objs = s.exec(
                    select(Service).where(Service.tenant_id == tenant.id, Service.is_active == True)
                ).all()
                svc_list = [{"id": sv.id, "name": sv.name} for sv in svc_objs]
            if not svc_list:
                send_text(tenant, customer_num, f"No services configured. Contact {tenant.business_name} directly.")
                clear_session(tenant.id, customer_num)
            else:
                set_session(tenant.id, customer_num, {
                    "state":              "awaiting_service",
                    "pending_queue_date": pending_queue_date,
                    "date_options":       date_options,
                    "service_ids":        [sv["id"] for sv in svc_list],
                    "include_parent":     include_parent,
                    "children_collected": collected,
                })
                with Session(engine) as s:
                    svc_objs = s.exec(select(Service).where(Service.id.in_([sv["id"] for sv in svc_list]))).all()
                send_service_menu(tenant, customer_num, svc_objs)
        return {"status": "success"}

    # ── ARRIVAL TIME ──────────────────────────────────────────────────────
    if state == "awaiting_arrival_time":
        pending_agent_id   = sess.get("pending_agent_id")
        pending_service_id = sess.get("pending_service_id")
        pending_queue_date = sess.get("pending_queue_date", today_str())
        include_parent     = sess.get("include_parent", True)
        collected          = sess.get("children_collected", [])
        # Sessions written before this field existed default to True — treat an
        # unknown pick as the customer's and leave it alone.
        agent_explicit     = sess.get("pending_agent_explicit", True)

        arrival = parse_arrival_time(text, pending_queue_date)
        if arrival is None:
            send_text(tenant, customer_num,
                "Sorry, I didn't understand that time. "
                "Please reply with a time like *12:30* or *now*."
            )
            return {"status": "success"}

        _do_assign(tenant, customer_num, customer_name,
                   pending_agent_id, pending_service_id, pending_queue_date, sess,
                   include_parent=include_parent, children_names=collected,
                   earliest_arrival=arrival,
                   customer_chose_agent=agent_explicit)
        return {"status": "success"}

    # ── FALLBACK ──────────────────────────────────────────────────────────
    clear_session(tenant.id, customer_num)
    send_main_menu(tenant, customer_num)
    return {"status": "success"}


class TenantCreate(SQLModel):
    """Separate create schema so id is never accepted from the client."""
    business_name:      str
    business_type:      str  = "General"
    whatsapp_number:    str
    owner_number:       str  = ""
    evolution_instance: str
    evolution_api_key:  str
    evolution_api_url:  str
    agent_label:        str  = "Agent"
    service_label:      str  = "Service"
    queue_opens:        int  = 8
    queue_closes:       int  = 17
    advance_days:       int  = 1
    is_active:          bool = True


# =============================================================================
# 9b. AUTH ROUTES
# =============================================================================

class LoginBody(SQLModel):
    email: str
    password: str


def _user_public(user: User, tenant: Optional[Tenant]) -> dict:
    return {
        "id":        user.id,
        "email":     user.email,
        "is_super":  user.is_super,
        "tenant_id": user.tenant_id,
        "tenant":    tenant.dict() if tenant else None,
    }


@app.post("/auth/login")
def login(body: LoginBody):
    email = (body.email or "").strip().lower()
    with Session(engine) as s:
        user = s.exec(select(User).where(User.email == email)).first()
        if not user or not user.is_active or not verify_password(body.password, user.password_hash):
            raise HTTPException(status_code=401, detail="Invalid email or password")
        tenant = s.get(Tenant, user.tenant_id) if user.tenant_id else None
        token = create_access_token(user)
        return {"access_token": token, "token_type": "bearer", "user": _user_public(user, tenant)}


@app.get("/auth/me")
def whoami(user: User = Depends(get_current_user)):
    with Session(engine) as s:
        tenant = s.get(Tenant, user.tenant_id) if user.tenant_id else None
    return _user_public(user, tenant)


class UserCreate(SQLModel):
    email:     str
    password:  str
    tenant_id: Optional[int] = None
    is_super:  bool = False


@admin_router.get("/admin/users")
def list_users(user: User = Depends(require_super)):
    with Session(engine) as s:
        rows = s.exec(select(User)).all()
        return [{"id": u.id, "email": u.email, "is_super": u.is_super,
                 "tenant_id": u.tenant_id, "is_active": u.is_active} for u in rows]


@admin_router.post("/admin/users")
def create_user(data: UserCreate, _: User = Depends(require_super)):
    """Provision a login. Super-admin only (no public signup)."""
    email = data.email.strip().lower()
    if not data.password or len(data.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    with Session(engine) as s:
        if s.exec(select(User).where(User.email == email)).first():
            raise HTTPException(status_code=409, detail="Email already registered")
        if not data.is_super:
            if not data.tenant_id or not s.get(Tenant, data.tenant_id):
                raise HTTPException(status_code=400, detail="Valid tenant_id required for a tenant user")
        u = User(email=email, password_hash=hash_password(data.password),
                 tenant_id=None if data.is_super else data.tenant_id,
                 is_super=data.is_super, is_active=True)
        s.add(u); s.commit(); s.refresh(u)
        return {"id": u.id, "email": u.email, "is_super": u.is_super, "tenant_id": u.tenant_id}


@admin_router.patch("/admin/users/{user_id}")
def update_user(user_id: int, updates: Dict[str, Any], _: User = Depends(require_super)):
    """Reset password / deactivate. Super-admin only."""
    with Session(engine) as s:
        u = s.get(User, user_id)
        if not u:
            raise HTTPException(status_code=404, detail="User not found")
        if "password" in updates:
            pw = updates["password"]
            if not pw or len(pw) < 8:
                raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
            u.password_hash = hash_password(pw)
        if "is_active" in updates:
            u.is_active = bool(updates["is_active"])
        s.add(u); s.commit()
        return {"id": u.id, "email": u.email, "is_active": u.is_active}


# =============================================================================
# 10. ADMIN — QUEUE MANAGEMENT
# =============================================================================

@admin_router.get("/admin/queue/{tenant_id}")
def get_queue(tenant_id: int, queue_date: Optional[str] = None,
              user: User = Depends(get_current_user)):
    """Get full queue for a tenant on a given date (defaults to today)."""
    ensure_tenant_access(user, tenant_id)
    target_date = queue_date or today_str()
    # Status sort order: active entries first, terminal entries at the bottom
    STATUS_ORDER = {"Waiting": 0, "InService": 1, "Done": 2, "NoShow": 3, "Cancelled": 4}
    with Session(engine) as s:
        entries = sorted(
            s.exec(
                select(QueueEntry).where(
                    QueueEntry.tenant_id  == tenant_id,
                    QueueEntry.queue_date == target_date
                )
            ).all(),
            key=lambda e: (STATUS_ORDER.get(e.status, 9), e.position, e.joined_at)
        )

        # Two batched lookups instead of two queries per row. The dashboard
        # polls this every 30s per open tab, so a 40-person day was ~80
        # round trips per tab per poll.
        services   = _service_map(s, entries)
        agent_ids  = {e.agent_id for e in entries if e.agent_id is not None}
        agents     = {
            a.id: a
            for a in s.exec(select(Agent).where(Agent.id.in_(agent_ids))).all()
        } if agent_ids else {}

        result = []
        for e in entries:
            agent   = agents.get(e.agent_id)
            service = services.get(e.service_id)
            result.append({
                "id":               e.id,
                "customer_name":    e.customer_name,
                "customer_number":  e.customer_number.replace("@s.whatsapp.net", ""),
                "additional_names": e.additional_names or "",
                "customer_phone":   e.customer_phone or "",
                "service":          service.name if service else "—",
                "agent":            agent.name if agent else "—",
                "status":           e.status,
                "position":         e.position,
                "estimated_start":  format_eta(e.estimated_start),
                "booked_via":       e.booked_via,
                "joined_at":        e.joined_at.isoformat(),
            })
        return result


@admin_router.patch("/admin/queue/{entry_id}/status")
def update_entry_status(entry_id: int, body: Dict[str, Any],
                        user: User = Depends(get_current_user)):
    """Update a queue entry status and recalculate ETAs."""
    new_status = body.get("status")
    if new_status not in ["InService", "Done", "NoShow", "Cancelled"]:
        raise HTTPException(status_code=400, detail="Invalid status")

    with Session(engine) as s:
        entry = s.get(QueueEntry, entry_id)
        if not entry:
            raise HTTPException(status_code=404, detail="Entry not found")
        ensure_tenant_access(user, entry.tenant_id)
        agent_id   = entry.agent_id
        tenant_id  = entry.tenant_id
        queue_date = entry.queue_date
        entry.status = new_status
        # Timestamps for reporting. Nothing else records when service actually
        # began or ended — estimated_start is a forecast that recalculation
        # overwrites, so real wait and service times are only derivable from
        # here onwards.
        if new_status == "InService" and not entry.started_at:
            entry.started_at = now()
        if new_status in ("Done", "NoShow", "Cancelled"):
            entry.finished_at = now()
            entry.closed_by   = "staff"
        s.add(entry)
        s.commit()

    recalculate_queue(tenant_id, agent_id, queue_date)

    # Going InService needs no scheduling — reconcile_notifications picks up the
    # 15-minute warning from queue state on its next tick, which survives a
    # restart in a way a scheduled job would not.
    if new_status in ("Done", "NoShow", "Cancelled"):
        _fire_youre_next(tenant_id, agent_id, queue_date)

    return {"status": "updated", "entry_id": entry_id, "new_status": new_status}


@admin_router.post("/admin/queue/walkin")
def add_walkin(body: Dict[str, Any], user: User = Depends(get_current_user)):
    """Add a walk-in customer from the admin dashboard."""
    tenant_id        = body.get("tenant_id")
    ensure_tenant_access(user, tenant_id)
    service_id       = body.get("service_id")
    agent_id         = body.get("agent_id")
    name             = body.get("customer_name", "Walk-in")
    phone            = body.get("customer_phone", "")
    additional_names = body.get("additional_names", "")
    queue_date       = body.get("queue_date", today_str())

    # Same lock the WhatsApp path takes, so a walk-in landing mid-confirmation
    # can't be handed the agent that booking is about to fill.
    with booking_lock(tenant_id, queue_date):
        with Session(engine) as s:
            tenant = s.get(Tenant, tenant_id)
            if not tenant:
                raise HTTPException(status_code=404, detail="Tenant not found")

            # Block walk-ins after closing hours (today only)
            if queue_date == today_str() and not queue_is_open_today(tenant):
                raise HTTPException(
                    status_code=400,
                    detail=f"Queue is closed. Opens at {tenant.queue_opens:02d}:00."
                )

            assigned_agent_id = assign_agent(tenant, service_id, agent_id, queue_date)
            if not assigned_agent_id:
                raise HTTPException(
                    status_code=400,
                    detail=f"No {tenant.agent_label.lower()}s can do this service "
                           f"on {queue_date} — check their schedules and blocks.")

            # Try to slot walk-in before a future appointment if there's a gap
            insert_joined_at = find_walkin_insert_joined_at(
                assigned_agent_id, tenant_id, tenant, queue_date, service_id
            )

            backlog  = get_agent_backlog_minutes(assigned_agent_id, tenant_id, queue_date)
            eta      = calculate_estimated_start(tenant, assigned_agent_id, queue_date, backlog)

            total_waiting = len(s.exec(
                select(QueueEntry).where(
                    QueueEntry.tenant_id  == tenant_id,
                    QueueEntry.queue_date == queue_date,
                    QueueEntry.status     == "Waiting"
                )
            ).all())

            clean_phone = normalize_number(phone) if phone else ""

            entry = QueueEntry(
                tenant_id        = tenant_id,
                service_id       = service_id,
                agent_id         = assigned_agent_id,
                # Store captured phone so walk-ins are findable later; fall back to
                # literal "walkin" when no number was given.
                customer_number  = clean_phone or "walkin",
                customer_name    = name,
                customer_phone   = clean_phone,
                additional_names = additional_names,
                queue_date       = queue_date,
                estimated_start  = eta,
                position         = total_waiting + 1,
                booked_via       = "walkin"
            )
            if insert_joined_at:
                entry.joined_at = insert_joined_at

            s.add(entry)
            s.commit()
            s.refresh(entry)

        # Recalculate ETAs for the whole agent queue now that the walk-in is inserted
        recalculate_queue(tenant_id, assigned_agent_id, queue_date)

    with Session(engine) as s:
        entry     = s.get(QueueEntry, entry.id)
        agent     = s.get(Agent, assigned_agent_id)
        service   = s.get(Service, service_id)
        tenant    = s.get(Tenant, tenant_id)
        eta       = entry.estimated_start

        # Send WhatsApp confirmation if phone captured
        if clean_phone:
            add_line = f"\n👥 Also for: {additional_names}" if additional_names else ""
            send_text(tenant, clean_phone,
                f"✅ *You're in the queue at {tenant.business_name}!*\n\n"
                f"📍 Position: #{entry.position}\n"
                f"👤 {tenant.agent_label}: {agent.name if agent else 'TBD'}\n"
                f"💼 {tenant.service_label}: {service.name if service else 'TBD'}\n"
                f"⏰ Estimated time: {format_eta(eta)}"
                f"{add_line}\n\n"
                f"We'll notify you when you're close to being served."
            )

    return {
        "id":               entry.id,
        "customer_name":    entry.customer_name,
        "additional_names": entry.additional_names,
        "service":          service.name if service else "—",
        "agent":            agent.name if agent else "—",
        "position":         entry.position,
        "estimated_start":  format_eta(entry.estimated_start),
    }


# =============================================================================
# 11. ADMIN — TENANTS
# =============================================================================

@admin_router.get("/admin/tenants")
def list_tenants(user: User = Depends(get_current_user)):
    with Session(engine) as s:
        if user.is_super:
            return s.exec(select(Tenant)).all()
        # Tenant users only ever see their own business
        t = s.get(Tenant, user.tenant_id) if user.tenant_id else None
        return [t] if t else []

@admin_router.post("/admin/tenants")
def create_tenant(data: TenantCreate, _: User = Depends(require_super)):
    tenant = Tenant(**data.dict())
    with Session(engine) as s:
        s.add(tenant)
        s.commit()
        s.refresh(tenant)
    return tenant


@admin_router.patch("/admin/tenants/{tenant_id}")
def update_tenant(tenant_id: int, updates: Dict[str, Any],
                  user: User = Depends(get_current_user)):
    ensure_tenant_access(user, tenant_id)
    with Session(engine) as s:
        tenant = s.get(Tenant, tenant_id)
        if not tenant:
            raise HTTPException(status_code=404, detail="Tenant not found")
        for k, v in updates.items():
            if hasattr(tenant, k):
                setattr(tenant, k, v)
        s.add(tenant)
        s.commit()
        s.refresh(tenant)
    return tenant


# =============================================================================
# 12. ADMIN — SERVICES
# =============================================================================

@admin_router.get("/admin/services/{tenant_id}")
def list_services(tenant_id: int, user: User = Depends(get_current_user)):
    ensure_tenant_access(user, tenant_id)
    with Session(engine) as s:
        return s.exec(
            select(Service).where(Service.tenant_id == tenant_id)
        ).all()


class ServiceCreate(SQLModel):
    tenant_id:         int
    name:              str
    duration_minutes:  int  = 60
    is_active:         bool = True


@admin_router.post("/admin/services")
def create_service(data: ServiceCreate, user: User = Depends(get_current_user)):
    ensure_tenant_access(user, data.tenant_id)
    service = Service(**data.dict())
    with Session(engine) as s:
        s.add(service)
        s.commit()
        s.refresh(service)
    return service


@admin_router.patch("/admin/services/{service_id}")
def update_service(service_id: int, updates: Dict[str, Any],
                   user: User = Depends(get_current_user)):
    with Session(engine) as s:
        svc = s.get(Service, service_id)
        if not svc:
            raise HTTPException(status_code=404, detail="Service not found")
        ensure_tenant_access(user, svc.tenant_id)
        for k, v in updates.items():
            if hasattr(svc, k):
                setattr(svc, k, v)
        s.add(svc)
        s.commit()
        s.refresh(svc)
    return svc


# =============================================================================
# 13. ADMIN — AGENTS
# =============================================================================

@admin_router.get("/admin/agents/{tenant_id}")
def list_agents(tenant_id: int, user: User = Depends(get_current_user)):
    ensure_tenant_access(user, tenant_id)
    with Session(engine) as s:
        agents = s.exec(
            select(Agent).where(Agent.tenant_id == tenant_id)
        ).all()
        result = []
        for agent in agents:
            service_ids = [
                row.service_id for row in s.exec(
                    select(AgentService).where(AgentService.agent_id == agent.id)
                ).all()
            ]
            result.append({**agent.dict(), "service_ids": service_ids})
        return result


@admin_router.post("/admin/agents")
def create_agent(body: Dict[str, Any], user: User = Depends(get_current_user)):
    """Create an agent and assign their services in one call."""
    ensure_tenant_access(user, body.get("tenant_id"))
    service_ids = body.pop("service_ids", [])
    body.pop("id", None)  # never accept id from client
    with Session(engine) as s:
        agent = Agent(**{k: v for k, v in body.items() if hasattr(Agent, k) and k != "id"})
        s.add(agent)
        s.commit()
        s.refresh(agent)
        for sid in service_ids:
            s.add(AgentService(agent_id=agent.id, service_id=sid))
        s.commit()
    return {**agent.dict(), "service_ids": service_ids}


@admin_router.patch("/admin/agents/{agent_id}")
def update_agent(agent_id: int, updates: Dict[str, Any],
                 user: User = Depends(get_current_user)):
    service_ids = updates.pop("service_ids", None)
    with Session(engine) as s:
        agent = s.get(Agent, agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")
        ensure_tenant_access(user, agent.tenant_id)
        for k, v in updates.items():
            if hasattr(agent, k):
                setattr(agent, k, v)
        s.add(agent)
        if service_ids is not None:
            # Replace all service assignments
            existing = s.exec(
                select(AgentService).where(AgentService.agent_id == agent_id)
            ).all()
            for e in existing:
                s.delete(e)
            for sid in service_ids:
                s.add(AgentService(agent_id=agent_id, service_id=sid))
        s.commit()
        s.refresh(agent)
        new_service_ids = [
            row.service_id for row in s.exec(
                select(AgentService).where(AgentService.agent_id == agent_id)
            ).all()
        ]
    return {**agent.dict(), "service_ids": new_service_ids}


# =============================================================================
# 13b. ADMIN — AGENT SCHEDULES & BLOCKS
# =============================================================================

WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday",
                 "Friday", "Saturday", "Sunday"]


def _load_agent(s: Session, agent_id: int, user: User) -> Agent:
    agent = s.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    ensure_tenant_access(user, agent.tenant_id)
    return agent


def _hhmm(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


def _parse_dt(value: Any, field: str) -> datetime:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", ""))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400,
                            detail=f"{field} must be an ISO datetime")


@admin_router.get("/admin/agents/{agent_id}/schedule")
def get_agent_schedule(agent_id: int, user: User = Depends(get_current_user)):
    """
    The agent's recurring weekly windows. An empty list means no schedule is
    set, and the agent falls back to the tenant's opening hours every day.
    """
    with Session(engine) as s:
        _load_agent(s, agent_id, user)
        rows = s.exec(
            select(AgentSchedule)
            .where(AgentSchedule.agent_id == agent_id)
            .order_by(AgentSchedule.weekday, AgentSchedule.start_minute)
        ).all()
        return {
            "agent_id": agent_id,
            "uses_tenant_hours": not rows,
            "windows": [
                {"id": r.id, "weekday": r.weekday,
                 "weekday_name": WEEKDAY_NAMES[r.weekday],
                 "start_minute": r.start_minute, "end_minute": r.end_minute,
                 "start": _hhmm(r.start_minute), "end": _hhmm(r.end_minute)}
                for r in rows
            ],
        }


@admin_router.put("/admin/agents/{agent_id}/schedule")
def set_agent_schedule(agent_id: int, body: Dict[str, Any],
                       user: User = Depends(get_current_user)):
    """
    Replace the agent's whole weekly schedule.

    Body: {"windows": [{"weekday": 0, "start_minute": 480, "end_minute": 1020}, …]}

    Replace-all rather than per-row edits, because a schedule is read as a
    whole and a half-applied one would silently take a working day off the
    board. Sending an empty list clears the schedule and returns the agent to
    the tenant's opening hours.

    Two windows on the same weekday make a split shift; the gap between them is
    simply not worked. Overlapping windows are accepted and coalesce on read.
    """
    windows = body.get("windows")
    if not isinstance(windows, list):
        raise HTTPException(status_code=400, detail="windows must be a list")

    cleaned = []
    for w in windows:
        if not isinstance(w, dict):
            raise HTTPException(status_code=400, detail="each window must be an object")
        try:
            weekday = int(w["weekday"])
            start   = int(w["start_minute"])
            end     = int(w["end_minute"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(
                status_code=400,
                detail="each window needs integer weekday, start_minute, end_minute")
        if not 0 <= weekday <= 6:
            raise HTTPException(status_code=400,
                                detail="weekday must be 0 (Monday) to 6 (Sunday)")
        if not 0 <= start < end <= 24 * 60:
            raise HTTPException(
                status_code=400,
                detail="need 0 <= start_minute < end_minute <= 1440")
        cleaned.append((weekday, start, end))

    with Session(engine) as s:
        agent = _load_agent(s, agent_id, user)
        tenant_id = agent.tenant_id
        for row in s.exec(
            select(AgentSchedule).where(AgentSchedule.agent_id == agent_id)
        ).all():
            s.delete(row)
        for weekday, start, end in cleaned:
            s.add(AgentSchedule(tenant_id=tenant_id, agent_id=agent_id,
                                weekday=weekday, start_minute=start,
                                end_minute=end))
        s.commit()

    # Today's ETAs were computed against the old hours — restate them now
    # rather than leaving customers holding times nobody will honour.
    recalculate_queue(tenant_id, agent_id, today_str())
    return get_agent_schedule(agent_id, user)


@admin_router.get("/admin/agents/{agent_id}/blocks")
def list_agent_blocks(agent_id: int, from_date: Optional[str] = None,
                      to_date: Optional[str] = None,
                      user: User = Depends(get_current_user)):
    """One-off unavailable windows. Defaults to today onward."""
    start = _parse_dt(f"{from_date}T00:00:00", "from_date") if from_date \
        else datetime.strptime(today_str(), "%Y-%m-%d")
    with Session(engine) as s:
        _load_agent(s, agent_id, user)
        conditions = [AgentBlock.agent_id == agent_id, AgentBlock.ends_at > start]
        if to_date:
            conditions.append(
                AgentBlock.starts_at < _parse_dt(f"{to_date}T00:00:00", "to_date")
                + timedelta(days=1))
        rows = s.exec(
            select(AgentBlock).where(*conditions).order_by(AgentBlock.starts_at)
        ).all()
        return [
            {"id": r.id, "agent_id": r.agent_id, "reason": r.reason,
             "starts_at": r.starts_at.isoformat(), "ends_at": r.ends_at.isoformat()}
            for r in rows
        ]


@admin_router.post("/admin/agents/{agent_id}/blocks")
def create_agent_block(agent_id: int, body: Dict[str, Any],
                       user: User = Depends(get_current_user)):
    """Body: {"starts_at": ISO, "ends_at": ISO, "reason": "Lunch"}"""
    starts_at = _parse_dt(body.get("starts_at"), "starts_at")
    ends_at   = _parse_dt(body.get("ends_at"), "ends_at")
    if ends_at <= starts_at:
        raise HTTPException(status_code=400, detail="ends_at must be after starts_at")

    with Session(engine) as s:
        agent = _load_agent(s, agent_id, user)
        block = AgentBlock(tenant_id=agent.tenant_id, agent_id=agent_id,
                           starts_at=starts_at, ends_at=ends_at,
                           reason=str(body.get("reason", ""))[:200])
        s.add(block)
        s.commit()
        s.refresh(block)
        result = {"id": block.id, "agent_id": agent_id, "reason": block.reason,
                  "starts_at": block.starts_at.isoformat(),
                  "ends_at": block.ends_at.isoformat()}
        tenant_id = agent.tenant_id

    # Anyone already booked into the blocked window needs a new time.
    recalculate_queue(tenant_id, agent_id, starts_at.date().isoformat())
    return result


@admin_router.delete("/admin/blocks/{block_id}")
def delete_agent_block(block_id: int, user: User = Depends(get_current_user)):
    with Session(engine) as s:
        block = s.get(AgentBlock, block_id)
        if not block:
            raise HTTPException(status_code=404, detail="Block not found")
        ensure_tenant_access(user, block.tenant_id)
        tenant_id, agent_id = block.tenant_id, block.agent_id
        queue_date = block.starts_at.date().isoformat()
        s.delete(block)
        s.commit()

    recalculate_queue(tenant_id, agent_id, queue_date)
    return {"status": "deleted", "block_id": block_id}


@admin_router.get("/admin/agents/{agent_id}/windows")
def get_agent_windows(agent_id: int, queue_date: Optional[str] = None,
                      user: User = Depends(get_current_user)):
    """
    The windows actually used for scheduling on a date — schedule resolved for
    that weekday, blocks already subtracted. This is what the queue engine
    sees, so it's the honest answer to "why was nobody booked at 14:00?".
    """
    target = queue_date or today_str()
    with Session(engine) as s:
        agent  = _load_agent(s, agent_id, user)
        tenant = s.get(Tenant, agent.tenant_id)

    windows = get_working_windows(tenant, agent_id, target)
    return {
        "agent_id": agent_id,
        "queue_date": target,
        "working": bool(windows),
        "windows": [
            {"start": w[0].isoformat(), "end": w[1].isoformat(),
             "minutes": int((w[1] - w[0]).total_seconds() // 60)}
            for w in windows
        ],
    }


# =============================================================================
# 13c. ADMIN — ANALYTICS
# =============================================================================

ANALYTICS_MAX_DAYS = 366


def _median(values: List[float]) -> Optional[int]:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return int(ordered[mid])
    return int((ordered[mid - 1] + ordered[mid]) / 2)


def _percentile(values: List[float], pct: float) -> Optional[int]:
    """
    Nearest-rank percentile: the smallest value at or below which `pct` of the
    sample falls. Samples here are small, so no interpolation.

    p90 of nine 5s and one 120 is 5, not 120 — the point is "90% of customers
    waited no longer than this", which the slowest single outlier does not set.
    """
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, min(len(ordered), math.ceil(pct / 100 * len(ordered))))
    return int(ordered[rank - 1])


def _duration_stats(values: List[float]) -> Dict[str, Any]:
    """
    Report a duration only when there is something to report. An empty result
    says so rather than showing a zero that reads like a real measurement.
    """
    return {
        "available": bool(values),
        "samples":   len(values),
        "median_minutes": _median(values),
        "p90_minutes":    _percentile(values, 90),
    }


@admin_router.get("/admin/analytics/{tenant_id}")
def get_analytics(tenant_id: int, from_date: Optional[str] = None,
                  to_date: Optional[str] = None,
                  user: User = Depends(get_current_user)):
    """
    Aggregates for one business over a date range (default: the last 30 days,
    inclusive of today).

    The care taken here is mostly about *not* reporting things:

    * A no-show closed by the midnight sweep is not a no-show. It means nobody
      tapped Done. Those are counted separately as `unclosed`, and the no-show
      rate is withheld entirely (`null`) for any period containing rows from
      before provenance was tracked, rather than quietly reading low.
    * Rates are computed over closed entries only. Including a queue that is
      still running would drag every rate down as the day progresses.
    * Wait and service times come from `started_at` / `finished_at`, which only
      exist from the upgrade onwards. When there are no samples the block says
      `available: false` instead of showing zero.
    * `minutes_booked` is scheduled duration, not measured — labelled as such.
    """
    ensure_tenant_access(user, tenant_id)

    end   = to_date or today_str()
    start = from_date or (
        datetime.strptime(end, "%Y-%m-%d") - timedelta(days=29)
    ).date().isoformat()
    try:
        span = (datetime.strptime(end, "%Y-%m-%d")
                - datetime.strptime(start, "%Y-%m-%d")).days + 1
    except ValueError:
        raise HTTPException(status_code=400, detail="Dates must be YYYY-MM-DD")
    if span < 1:
        raise HTTPException(status_code=400, detail="to_date must not precede from_date")
    if span > ANALYTICS_MAX_DAYS:
        raise HTTPException(
            status_code=400,
            detail=f"Range is limited to {ANALYTICS_MAX_DAYS} days")

    with Session(engine) as s:
        entries = s.exec(
            select(QueueEntry).where(
                QueueEntry.tenant_id  == tenant_id,
                QueueEntry.queue_date >= start,
                QueueEntry.queue_date <= end,
            )
        ).all()
        # Batched, like everywhere else in the engine — never one query per row.
        agents = {a.id: a for a in s.exec(
            select(Agent).where(Agent.tenant_id == tenant_id)).all()}
        services = {sv.id: sv for sv in s.exec(
            select(Service).where(Service.tenant_id == tenant_id)).all()}

    done = no_show_staff = no_show_system = no_show_unknown = 0
    cancelled_customer = cancelled_other = still_open = 0
    by_day: Dict[str, Dict[str, int]] = {}
    by_weekday = [0] * 7
    by_hour    = [0] * 24
    channel    = {"whatsapp": 0, "walkin": 0}
    per_agent: Dict[int, Dict[str, int]] = {}
    per_service: Dict[int, Dict[str, int]] = {}
    wait_minutes: List[float]    = []
    service_minutes: List[float] = []

    for e in entries:
        day = by_day.setdefault(e.queue_date, {"bookings": 0, "done": 0})
        day["bookings"] += 1
        try:
            by_weekday[datetime.strptime(e.queue_date, "%Y-%m-%d").weekday()] += 1
        except ValueError:
            pass
        if e.joined_at:
            by_hour[e.joined_at.hour] += 1
        channel[e.booked_via if e.booked_via in channel else "walkin"] += 1

        a = per_agent.setdefault(e.agent_id, {"bookings": 0, "done": 0,
                                              "no_shows": 0, "unclosed": 0})
        a["bookings"] += 1
        sv = per_service.setdefault(e.service_id, {"bookings": 0, "minutes_booked": 0})
        sv["bookings"] += 1
        svc = services.get(e.service_id)
        sv["minutes_booked"] += svc.duration_minutes if svc else 0

        if e.status == "Done":
            done += 1
            day["done"] += 1
            a["done"] += 1
        elif e.status == "NoShow":
            if e.closed_by == "system":
                no_show_system += 1
                a["unclosed"] += 1
            elif e.closed_by:
                no_show_staff += 1
                a["no_shows"] += 1
            else:
                no_show_unknown += 1
        elif e.status == "Cancelled":
            if e.closed_by == "customer":
                cancelled_customer += 1
            else:
                cancelled_other += 1
        else:
            still_open += 1

        if e.started_at and e.joined_at:
            wait = (e.started_at - e.joined_at).total_seconds() / 60
            if wait >= 0:
                wait_minutes.append(wait)
        if e.status == "Done" and e.started_at and e.finished_at:
            served = (e.finished_at - e.started_at).total_seconds() / 60
            if served >= 0:
                service_minutes.append(served)

    cancelled = cancelled_customer + cancelled_other
    no_shows  = no_show_staff + no_show_unknown
    closed    = done + no_shows + no_show_system + cancelled

    def rate(count):
        return round(count / closed, 4) if closed else None

    # Withheld rather than understated: with untagged rows in the range there
    # is no way to tell a real no-show from an entry staff forgot to close.
    no_show_rate = rate(no_show_staff) if no_show_unknown == 0 else None

    return {
        "tenant_id": tenant_id,
        "from_date": start,
        "to_date":   end,
        "days":      span,
        "totals": {
            "bookings":           len(entries),
            "done":               done,
            "no_shows":           no_show_staff,
            "no_shows_untracked": no_show_unknown,
            "unclosed":           no_show_system,
            "cancelled":          cancelled,
            "cancelled_by_customer": cancelled_customer,
            "still_open":         still_open,
            "closed":             closed,
        },
        "rates": {
            "completion":   rate(done),
            "no_show":      no_show_rate,
            "no_show_note": None if no_show_unknown == 0 else (
                f"{no_show_unknown} no-shows in this range predate close tracking, "
                f"so a real no-show can't be told apart from an entry nobody "
                f"closed. Rate withheld."),
            "cancellation": rate(cancelled),
            "unclosed":     rate(no_show_system),
        },
        "channel": channel,
        "by_day": [
            {"date": d, "weekday": WEEKDAY_NAMES[
                datetime.strptime(d, "%Y-%m-%d").weekday()][:3],
             **by_day[d]}
            for d in sorted(by_day)
        ],
        "by_weekday": [
            {"weekday": i, "name": WEEKDAY_NAMES[i], "bookings": n}
            for i, n in enumerate(by_weekday)
        ],
        "by_hour": [{"hour": h, "bookings": n} for h, n in enumerate(by_hour)],
        "by_agent": sorted((
            {"agent_id": aid,
             "name": agents[aid].name if aid in agents else "(removed)",
             **vals}
            for aid, vals in per_agent.items()
        ), key=lambda r: -r["bookings"]),
        "by_service": sorted((
            {"service_id": sid,
             "name": services[sid].name if sid in services else "(removed)",
             **vals}
            for sid, vals in per_service.items()
        ), key=lambda r: -r["bookings"]),
        # Measured, not estimated — and only from the upgrade onwards.
        "wait_time":    _duration_stats(wait_minutes),
        "service_time": _duration_stats(service_minutes),
        "data_quality": {
            "unclosed": no_show_system,
            "untracked_closes": no_show_unknown,
            "note": ("Entries the midnight sweep closed because staff never "
                     "marked them Done. High numbers mean the dashboard isn't "
                     "being kept up to date, not that customers didn't arrive."),
        },
    }


# =============================================================================
# 14. HEALTH + UTILS
# =============================================================================

@app.get("/health")
def health():
    try:
        redis_client.ping()
        redis_ok = True
    except Exception as e:
        redis_ok = str(e)
    with Session(engine) as s:
        tenants = len(s.exec(select(Tenant)).all())
        # A climbing pending count or any failures means messages aren't
        # reaching customers — worth alerting on.
        outbox_pending = len(s.exec(
            select(OutboxMessage).where(OutboxMessage.status == "Pending")
        ).all())
        outbox_failed = len(s.exec(
            select(OutboxMessage).where(OutboxMessage.status == "Failed")
        ).all())
    return {"status": "ok", "redis": redis_ok, "tenants": tenants,
            "webhook_auth": bool(WEBHOOK_SECRETS),
            "outbox_pending": outbox_pending, "outbox_failed": outbox_failed}


@admin_router.post("/admin/migrate-reset")
def migrate_reset(_: User = Depends(require_super)):
    """Drop and recreate all tables using CASCADE. Destructive — super-admin only."""
    from sqlalchemy import text
    with engine.connect() as conn:
        conn.execute(text(
            "DROP TABLE IF EXISTS agentservice, queueentry, agent, service, tenant CASCADE"
        ))
        conn.commit()
    SQLModel.metadata.create_all(engine)
    return {"status": "done"}


# Register all guarded admin routes (must run after every @admin_router route above)
app.include_router(admin_router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=9000)
