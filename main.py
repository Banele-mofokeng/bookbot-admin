import os
import json
import requests
import redis
from datetime import datetime, timedelta, time, date
from zoneinfo import ZoneInfo
from fastapi import FastAPI, Request, HTTPException
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
    position: int              = 0                   # display position in full queue
    notified_two_away: bool    = False
    notified_next: bool        = False
    joined_at: datetime        = Field(default_factory=now)


def create_db_and_tables():
    SQLModel.metadata.create_all(engine)
    # Add new columns to existing tables if they don't exist yet (SQLite migration)
    with engine.connect() as conn:
        existing = [row[1] for row in conn.execute(
            __import__("sqlalchemy").text("PRAGMA table_info(queueentry)")
        ).fetchall()]
        if "parent_entry_id" not in existing:
            conn.execute(__import__("sqlalchemy").text(
                "ALTER TABLE queueentry ADD COLUMN parent_entry_id INTEGER"
            ))
            conn.commit()


# =============================================================================
# 3. APP + SCHEDULER
# =============================================================================

app = FastAPI(title="QueueBot — Smart Queue Platform")
scheduler = AsyncIOScheduler()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def on_startup():
    print("🚀 QueueBot starting...")
    create_db_and_tables()
    scheduler.add_job(midnight_reset_job, "cron", hour=0, minute=1, id="midnight_reset")
    scheduler.start()
    print("✅ DB ready. Scheduler running.")


@app.on_event("shutdown")
async def on_shutdown():
    scheduler.shutdown()


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


# =============================================================================
# 6. QUEUE ENGINE
# =============================================================================

def get_agent_backlog_minutes(agent_id: int, tenant_id: int, queue_date: str,
                               exclude_entry_id: Optional[int] = None) -> int:
    """
    Total minutes of work still ahead for a given agent on a given date.
    Counts Waiting + InService entries only (Done/NoShow/Cancelled don't block).
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

        total = 0
        for e in entries:
            if exclude_entry_id and e.id == exclude_entry_id:
                continue
            svc = s.get(Service, e.service_id)
            if svc:
                total += svc.duration_minutes
    return total


def calculate_estimated_start(tenant: Tenant, agent_id: int,
                               queue_date: str, backlog_minutes: int) -> datetime:
    """Convert backlog minutes into an actual datetime."""
    opens = datetime.strptime(
        f"{queue_date} {tenant.queue_opens:02d}:00", "%Y-%m-%d %H:%M"
    )
    return opens + timedelta(minutes=backlog_minutes)


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

        # Honor preference if that agent is capable
        if preferred_agent_id:
            preferred = next((a for a in active_agents if a.id == preferred_agent_id), None)
            if preferred:
                return preferred.id

        # Assign to agent with shortest backlog
        best_agent_id = min(
            active_agents,
            key=lambda a: get_agent_backlog_minutes(a.id, tenant.id, queue_date)
        ).id
        return best_agent_id


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

        running_minutes = 0
        for entry in entries:
            svc = s.get(Service, entry.service_id)
            duration = svc.duration_minutes if svc else 60

            if entry.status == "InService":
                # Frozen — don't recalculate, just add their duration to the running total
                running_minutes += duration
            else:
                # Recalculate
                entry.estimated_start = calculate_estimated_start(
                    tenant, agent_id, queue_date, running_minutes
                )
                s.add(entry)
                running_minutes += duration

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

def send_text(tenant: Tenant, number: str, text: str):
    url     = f"{tenant.evolution_api_url.rstrip('/')}/message/sendText/{tenant.evolution_instance}"
    headers = {"apikey": tenant.evolution_api_key, "Content-Type": "application/json"}
    try:
        r = requests.post(url, json={"number": number, "text": text}, headers=headers, timeout=10)
        print(f"📡 [{tenant.business_name}] → {number} | {r.status_code}")
        return r.json()
    except Exception as e:
        print(f"❌ [{tenant.business_name}] send error: {e}")
        return {"error": str(e)}


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


def send_agent_menu(tenant: Tenant, number: str,
                    agents: list, queue_date: str, service_id: int):
    lines = []
    for i, agent in enumerate(agents):
        agent_id = agent["id"] if isinstance(agent, dict) else agent.id
        name     = agent["name"] if isinstance(agent, dict) else agent.name
        backlog  = get_agent_backlog_minutes(agent_id, tenant.id, queue_date)
        eta      = calculate_estimated_start(tenant, agent_id, queue_date, backlog)
        lines.append(
            f"{i+1}️⃣ {name}  _(next free around {format_eta(eta)})_"
        )
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


def _notify_job_id(agent_id: int, queue_date: str) -> str:
    return f"notify_next_{agent_id}_{queue_date}"


def _schedule_15min_warning(tenant_id: int, agent_id: int, queue_date: str, duration_minutes: int):
    """
    Called when an entry goes InService.
    Schedules a one-shot job to warn the next waiter 15 minutes before this service ends.
    """
    delay = max(1, duration_minutes - 15)
    fire_at = now() + timedelta(minutes=delay)
    job_id = _notify_job_id(agent_id, queue_date)
    try:
        scheduler.remove_job(job_id)
    except Exception:
        pass
    scheduler.add_job(
        _fire_15min_warning,
        "date",
        run_date=fire_at,
        args=[tenant_id, agent_id, queue_date],
        id=job_id,
    )
    print(f"\U0001f4e5 15-min warning scheduled for agent {agent_id} at {fire_at.strftime('%H:%M')}")


async def _fire_15min_warning(tenant_id: int, agent_id: int, queue_date: str):
    """Fires ~15 min before the current InService person finishes — warns the next waiter."""
    with Session(engine) as s:
        tenant = s.get(Tenant, tenant_id)
        if not tenant:
            return
        next_entry = s.exec(
            select(QueueEntry).where(
                QueueEntry.agent_id   == agent_id,
                QueueEntry.tenant_id  == tenant_id,
                QueueEntry.queue_date == queue_date,
                QueueEntry.status     == "Waiting",
            ).order_by(QueueEntry.joined_at)
        ).first()
        if not next_entry or next_entry.notified_two_away:
            return
        notify_to = get_notify_number(next_entry)
        if not notify_to:
            return
        agent   = s.get(Agent, next_entry.agent_id)
        service = s.get(Service, next_entry.service_id)
        send_text(tenant, notify_to,
            f"\u23f3 *Almost your turn at {tenant.business_name}!*\n\n"
            f"You\'re up in about *15 minutes*.\n"
            f"\U0001f4bc {service.name if service else ''} with {agent.name if agent else tenant.agent_label}\n\n"
            f"Start making your way over \U0001f6b6"
        )
        next_entry.notified_two_away = True
        s.add(next_entry)
        s.commit()


def _fire_youre_next(tenant_id: int, agent_id: int, queue_date: str):
    """
    Called when an entry is marked Done / NoShow / Cancelled.
    Cancels any pending 15-min job and immediately tells the next waiter they're up.
    """
    # Cancel the pending 15-min warning — the InService person is already done
    try:
        scheduler.remove_job(_notify_job_id(agent_id, queue_date))
    except Exception:
        pass

    with Session(engine) as s:
        tenant = s.exec(
            select(Tenant).where(Tenant.id == tenant_id)
        ).first()
        if not tenant:
            return
        next_entry = s.exec(
            select(QueueEntry).where(
                QueueEntry.agent_id   == agent_id,
                QueueEntry.tenant_id  == tenant_id,
                QueueEntry.queue_date == queue_date,
                QueueEntry.status     == "Waiting",
            ).order_by(QueueEntry.joined_at)
        ).first()
        if not next_entry or next_entry.notified_next:
            return
        notify_to = get_notify_number(next_entry)
        if not notify_to:
            return
        agent   = s.get(Agent, next_entry.agent_id)
        service = s.get(Service, next_entry.service_id)
        send_text(tenant, notify_to,
            f"\U0001f680 *You\'re up next at {tenant.business_name}!*\n\n"
            f"Head over now \u2014 {agent.name if agent else tenant.agent_label} is ready for you.\n"
            f"\U0001f4bc {service.name if service else ''}"
        )
        next_entry.notified_next    = True
        next_entry.notified_two_away = True
        s.add(next_entry)
        s.commit()

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
            entry.status = "NoShow" if entry.status == "Waiting" else "Done"
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
               children_names: list = None):
    """
    Saves queue entries and sends confirmation.
    include_parent=False means only child entries are created (parent is just escorting).
    children_names is a list of child name strings; each gets its own QueueEntry.
    Shared by single-agent auto-assign and manual agent pick paths.
    """
    if children_names is None:
        children_names = []

    backlog  = get_agent_backlog_minutes(assigned_agent_id, tenant.id, queue_date)
    eta      = calculate_estimated_start(tenant, assigned_agent_id, queue_date, backlog)

    print(f"\U0001f4be Saving entry | tenant={tenant.id} service={service_id} agent={assigned_agent_id} date={queue_date} parent={include_parent} children={children_names}")

    with Session(engine) as s:
        total_waiting = len(s.exec(
            select(QueueEntry).where(
                QueueEntry.tenant_id  == tenant.id,
                QueueEntry.queue_date == queue_date,
                QueueEntry.status     == "Waiting"
            )
        ).all())
        next_position = total_waiting + 1

        parent_entry = None
        if include_parent:
            parent_entry = QueueEntry(
                tenant_id          = tenant.id,
                service_id         = service_id,
                agent_id           = assigned_agent_id,
                customer_number    = customer_num,
                customer_name      = customer_name,
                queue_date         = queue_date,
                estimated_start    = eta,
                position           = next_position,
                booked_via         = "whatsapp"
            )
            s.add(parent_entry)
            s.commit()
            s.refresh(parent_entry)
            next_position += 1

        child_entries = []
        for child_name in children_names:
            child_entry = QueueEntry(
                tenant_id          = tenant.id,
                service_id         = service_id,
                agent_id           = assigned_agent_id,
                customer_number    = customer_num,
                customer_name      = child_name,
                queue_date         = queue_date,
                estimated_start    = eta,
                position           = next_position,
                booked_via         = "whatsapp",
                parent_entry_id    = parent_entry.id if parent_entry else None,
            )
            s.add(child_entry)
            child_entries.append((child_name, next_position))
            next_position += 1
        s.commit()

        agent   = s.get(Agent, assigned_agent_id)
        service = s.get(Service, service_id)

    clear_session(tenant.id, customer_num)

    date_display = datetime.strptime(queue_date, "%Y-%m-%d").strftime("%a %d %b")

    # Build position summary lines
    position_lines = ""
    if include_parent:
        position_lines += f"\U0001f4cd *Your position:* #{parent_entry.position}\n"
    for child_name, child_pos in child_entries:
        position_lines += f"\U0001f476 *{child_name}:* #{child_pos}\n"

    # Notification promise is based on the first queued position
    first_position = parent_entry.position if include_parent else (child_entries[0][1] if child_entries else 1)
    if first_position == 1:
        notify_line = "You\'re first in line \U0001f3c6 — head over when you\'re ready."
    elif first_position == 2:
        notify_line = "We\'ll notify you when you\'re up next."
    else:
        notify_line = f"We\'ll notify you when you\'re 2 away and when you\'re next."

    send_text(tenant, customer_num,
        f"\u2705 *You\'re in the queue!*\n\n"
        f"{position_lines}"
        f"\U0001f464 {tenant.agent_label}: {agent.name if agent else 'TBD'}\n"
        f"\U0001f4bc {tenant.service_label}: {service.name if service else 'TBD'}\n"
        f"\U0001f4c5 Date: {date_display}\n"
        f"\u23f0 Estimated time: {format_eta(eta)}\n\n"
        f"{notify_line}\n"
        f"Reply *status* anytime to check your position."
    )

    # Notify owner (report the first booked person)
    report_name = customer_name if include_parent else (children_names[0] if children_names else customer_name)
    report_position = parent_entry.position if include_parent else (child_entries[0][1] if child_entries else 1)
    if tenant.owner_number:
        send_text(tenant, normalize_number(tenant.owner_number),
            f"\U0001f514 *New Queue Entry*\n\n"
            f"\U0001f464 {report_name}\n"
            f"\U0001f4bc {service.name if service else ''}\n"
            f"\U0001f477 {agent.name if agent else ''}\n"
            f"\U0001f4cd Position #{report_position} | \u23f0 {format_eta(eta)}"
        )


@app.post("/webhook")
async def handle_webhook(request: Request):
    data = await request.json()

    if data.get("event") != "messages.upsert":
        return {"status": "ignored"}

    msg_data = data.get("data", {})
    if msg_data.get("key", {}).get("fromMe"):
        return {"status": "ignored"}

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
    if any(w in text for w in ["menu","start","hi","hello","hey"]) and state not in ["awaiting_booking_for", "awaiting_children", "awaiting_children_names"]:
        set_session(tenant.id, customer_num, {"state": "main_menu"})
        send_main_menu(tenant, customer_num)
        return {"status": "success"}

    # ── BACK HANDLER ──────────────────────────────────────────────────────
    if text == "0":
        if state == "awaiting_date":
            set_session(tenant.id, customer_num, {"state": "main_menu"})
            send_main_menu(tenant, customer_num)
        elif state == "awaiting_service":
            prev_date_options = sess.get("date_options")
            if prev_date_options:
                # Came from date picker — go back to it
                set_session(tenant.id, customer_num, {
                    "state": "awaiting_date",
                    "date_options": prev_date_options,
                })
                lines_out = []
                today_d = now().date()
                for i, d_str in enumerate(prev_date_options):
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
        elif state == "awaiting_agent":
            # Go back to service menu
            queue_date   = sess.get("queue_date", today_str())
            service_ids  = sess.get("service_ids", [])
            if service_ids:
                set_session(tenant.id, customer_num, {
                    "state": "awaiting_service",
                    "queue_date": queue_date,
                    "service_ids": service_ids,
                    "date_options": sess.get("date_options"),
                })
                with Session(engine) as s:
                    services = s.exec(
                        select(Service).where(Service.id.in_(service_ids))
                    ).all()
                send_service_menu(tenant, customer_num, services)
            else:
                set_session(tenant.id, customer_num, {"state": "main_menu"})
                send_main_menu(tenant, customer_num)
        elif state in ("awaiting_booking_for", "awaiting_children", "awaiting_children_names"):
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
                            QueueEntry.joined_at  < existing.joined_at
                        )
                    ).all())
                set_session(tenant.id, customer_num, {
                    "state": "awaiting_rebook",
                    "existing_entry_id": existing.id,
                })
                send_text(tenant, customer_num,
                    f"You\'re already in the queue today!\n\n"
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

            # Start booking flow
            if tenant.advance_days == 0:
                with Session(engine) as s:
                    services = s.exec(
                        select(Service).where(Service.tenant_id == tenant.id, Service.is_active == True)
                    ).all()
                    service_list = [{"id": sv.id, "name": sv.name, "duration_minutes": sv.duration_minutes} for sv in services]
                if not service_list:
                    send_text(tenant, customer_num, f"No services configured. Contact {tenant.business_name} directly.")
                    clear_session(tenant.id, customer_num)
                else:
                    set_session(tenant.id, customer_num, {
                        "state": "awaiting_service",
                        "queue_date": today_str(),
                        "service_ids": [sv["id"] for sv in service_list],
                    })
                    with Session(engine) as s:
                        svc_objs = s.exec(select(Service).where(Service.id.in_([sv["id"] for sv in service_list]))).all()
                    send_service_menu(tenant, customer_num, svc_objs)
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
                    agent_id = entry.agent_id
                    entry.status = "Cancelled"
                    s.add(entry)
                    s.commit()
                    recalculate_queue(tenant.id, agent_id, today)
                    send_text(tenant, customer_num,
                        f"\u2705 You\'ve been removed from the queue at *{tenant.business_name}*.\n\n"
                        f"Reply *menu* anytime to rejoin."
                    )
                    if tenant.owner_number:
                        svc  = s.get(Service, entry.service_id)
                        date_display = datetime.strptime(entry.queue_date, "%Y-%m-%d").strftime("%a %d %b")
                        send_text(tenant, normalize_number(tenant.owner_number),
                            f"\u274c *Queue Cancellation*\n\n"
                            f"\U0001f464 {entry.customer_name}\n"
                            f"\U0001f4bc {svc.name if svc else ''}\n"
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
            # Cancel and rebook
            if existing_entry_id:
                with Session(engine) as s:
                    entry = s.get(QueueEntry, existing_entry_id)
                    if entry:
                        agent_id = entry.agent_id
                        entry.status = "Cancelled"
                        s.add(entry)
                        s.commit()
                        recalculate_queue(tenant.id, agent_id, entry.queue_date)
            # Start fresh booking flow
            if tenant.advance_days == 0:
                with Session(engine) as s:
                    services = s.exec(select(Service).where(Service.tenant_id == tenant.id, Service.is_active == True)).all()
                    svc_list = [{"id": sv.id, "name": sv.name} for sv in services]
                set_session(tenant.id, customer_num, {
                    "state": "awaiting_service",
                    "queue_date": today_str(),
                    "service_ids": [sv["id"] for sv in svc_list],
                })
                with Session(engine) as s:
                    svc_objs = s.exec(select(Service).where(Service.id.in_([sv["id"] for sv in svc_list]))).all()
                send_service_menu(tenant, customer_num, svc_objs)
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
            with Session(engine) as s:
                services = s.exec(
                    select(Service).where(Service.tenant_id == tenant.id, Service.is_active == True)
                ).all()
                svc_list = [{"id": sv.id, "name": sv.name} for sv in services]
            if not svc_list:
                send_text(tenant, customer_num, f"No services configured. Contact {tenant.business_name} directly.")
                clear_session(tenant.id, customer_num)
            else:
                set_session(tenant.id, customer_num, {
                    "state": "awaiting_service",
                    "queue_date": chosen_date,
                    "service_ids": [sv["id"] for sv in svc_list],
                    "date_options": date_options,
                })
                with Session(engine) as s:
                    svc_objs = s.exec(select(Service).where(Service.id.in_([sv["id"] for sv in svc_list]))).all()
                send_service_menu(tenant, customer_num, svc_objs)
        else:
            send_text(tenant, customer_num,
                f"Please reply with a number between 1 and {len(date_options)}, or *0* to go back.")
        return {"status": "success"}

    # ── SERVICE SELECTION ─────────────────────────────────────────────────
    if state == "awaiting_service":
        service_ids = sess.get("service_ids", [])
        queue_date  = sess.get("queue_date", today_str())

        if text == "0":
            date_options = sess.get("date_options")
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
                    "*Which day?* \U0001f4c5\n\n" + "\n".join(lines_out)
                    + "\n\nReply with a number, or *0* to go back."
                )
            else:
                set_session(tenant.id, customer_num, {"state": "main_menu"})
                send_main_menu(tenant, customer_num)
            return {"status": "success"}

        if text.isdigit() and 1 <= int(text) <= len(service_ids):
            chosen_service_id = service_ids[int(text) - 1]
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
                # Only one agent — skip agent menu
                set_session(tenant.id, customer_num, {
                    "state": "awaiting_agent",
                    "queue_date": queue_date,
                    "service_id": chosen_service_id,
                    "agent_ids": [agents[0]["id"]],
                    "service_ids": service_ids,
                    "date_options": sess.get("date_options"),
                    "auto_assigned": True,
                    "auto_agent_id": agents[0]["id"],
                })
                # Ask who we're booking for before saving the entry
                set_session(tenant.id, customer_num, {
                    "state": "awaiting_booking_for",
                    "pending_agent_id": agents[0]["id"],
                    "pending_service_id": chosen_service_id,
                    "pending_queue_date": queue_date,
                })
                send_text(tenant, customer_num,
                    "Who are you booking for?\n"
                    "1\ufe0f\u20e3 Just me\n"
                    "2\ufe0f\u20e3 Me and my children\n"
                    "3\ufe0f\u20e3 My children only\n\n"
                    "Reply *1*, *2*, or *3*"
                )
            else:
                set_session(tenant.id, customer_num, {
                    "state": "awaiting_agent",
                    "queue_date": queue_date,
                    "service_id": chosen_service_id,
                    "agent_ids": [a["id"] for a in agents],
                    "service_ids": service_ids,
                    "date_options": sess.get("date_options"),
                })
                send_agent_menu(tenant, customer_num, agents, queue_date, chosen_service_id)
        else:
            send_text(tenant, customer_num,
                f"Please reply with a number between 1 and {len(service_ids)}, or *0* to go back.")
        return {"status": "success"}

    # ── AGENT SELECTION ───────────────────────────────────────────────────
    if state == "awaiting_agent":
        agent_ids   = sess.get("agent_ids", [])
        service_id  = sess.get("service_id")
        queue_date  = sess.get("queue_date", today_str())
        no_pref_idx = len(agent_ids) + 1

        if text == "0":
            service_ids  = sess.get("service_ids", [])
            date_options = sess.get("date_options")
            set_session(tenant.id, customer_num, {
                "state": "awaiting_service",
                "queue_date": queue_date,
                "service_ids": service_ids,
                "date_options": date_options,
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
                send_text(tenant, customer_num,
                    f"Sorry, no {tenant.agent_label.lower()}s available. Reply *0* to go back.")
                return {"status": "success"}

            set_session(tenant.id, customer_num, {
                "state": "awaiting_booking_for",
                "pending_agent_id": assigned_agent_id,
                "pending_service_id": service_id,
                "pending_queue_date": queue_date,
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
                f"Please reply with a number, or *0* to go back.")
        return {"status": "success"}

    # ── WHO ARE WE BOOKING FOR? ────────────────────────────────────────────
    if state == "awaiting_booking_for":
        pending_agent_id   = sess.get("pending_agent_id")
        pending_service_id = sess.get("pending_service_id")
        pending_queue_date = sess.get("pending_queue_date", today_str())

        if text == "1":
            # Just the parent — book immediately
            _do_assign(tenant, customer_num, customer_name,
                       pending_agent_id, pending_service_id, pending_queue_date, sess,
                       include_parent=True, children_names=[])
        elif text in ("2", "3"):
            include_parent = (text == "2")
            set_session(tenant.id, customer_num, {
                "state": "awaiting_children",
                "pending_agent_id": pending_agent_id,
                "pending_service_id": pending_service_id,
                "pending_queue_date": pending_queue_date,
                "include_parent": include_parent,
            })
            send_text(tenant, customer_num,
                "How many children are you booking for?\n"
                "1\ufe0f\u20e3 1 child\n"
                "2\ufe0f\u20e3 2 children\n\n"
                "Reply *1* or *2*"
            )
        else:
            send_text(tenant, customer_num,
                "Please reply *1* (just me), *2* (me + children), or *3* (children only)."
            )
        return {"status": "success"}

    # ── HOW MANY CHILDREN? ────────────────────────────────────────────────
    if state == "awaiting_children":
        pending_agent_id   = sess.get("pending_agent_id")
        pending_service_id = sess.get("pending_service_id")
        pending_queue_date = sess.get("pending_queue_date", today_str())
        include_parent     = sess.get("include_parent", True)

        if text.isdigit() and 1 <= int(text) <= 2:
            count = int(text)
            set_session(tenant.id, customer_num, {
                "state": "awaiting_children_names",
                "pending_agent_id": pending_agent_id,
                "pending_service_id": pending_service_id,
                "pending_queue_date": pending_queue_date,
                "include_parent": include_parent,
                "children_count": count,
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
        pending_agent_id   = sess.get("pending_agent_id")
        pending_service_id = sess.get("pending_service_id")
        pending_queue_date = sess.get("pending_queue_date", today_str())
        include_parent     = sess.get("include_parent", True)
        count              = sess.get("children_count", 1)
        collected          = sess.get("children_collected", [])
        collected.append(raw_text.strip())

        if len(collected) < count:
            set_session(tenant.id, customer_num, {
                "state": "awaiting_children_names",
                "pending_agent_id": pending_agent_id,
                "pending_service_id": pending_service_id,
                "pending_queue_date": pending_queue_date,
                "include_parent": include_parent,
                "children_count": count,
                "children_collected": collected,
            })
            send_text(tenant, customer_num,
                f"Name of child {len(collected) + 1} of {count}:"
            )
        else:
            # All names collected — save all entries now
            _do_assign(tenant, customer_num, customer_name,
                       pending_agent_id, pending_service_id, pending_queue_date, sess,
                       include_parent=include_parent, children_names=collected)
        return {"status": "success"}

    # ── FALLBACK ──────────────────────────────────────────────────────────
    clear_session(tenant.id, customer_num)
    send_main_menu(tenant, customer_num)
    return {"status": "success"}


import os
import json
import requests
import redis
from datetime import datetime, timedelta, time, date
from zoneinfo import ZoneInfo
from fastapi import FastAPI, Request, HTTPException
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
# 10. ADMIN — QUEUE MANAGEMENT
# =============================================================================

@app.get("/admin/queue/{tenant_id}")
def get_queue(tenant_id: int, queue_date: Optional[str] = None):
    """Get full queue for a tenant on a given date (defaults to today)."""
    target_date = queue_date or today_str()
    with Session(engine) as s:
        entries = s.exec(
            select(QueueEntry).where(
                QueueEntry.tenant_id  == tenant_id,
                QueueEntry.queue_date == target_date
            ).order_by(QueueEntry.position, QueueEntry.joined_at)
        ).all()

        result = []
        for e in entries:
            agent   = s.get(Agent, e.agent_id)
            service = s.get(Service, e.service_id)
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


@app.patch("/admin/queue/{entry_id}/status")
def update_entry_status(entry_id: int, body: Dict[str, Any]):
    """Update a queue entry status and recalculate ETAs."""
    new_status = body.get("status")
    if new_status not in ["InService", "Done", "NoShow", "Cancelled"]:
        raise HTTPException(status_code=400, detail="Invalid status")

    with Session(engine) as s:
        entry = s.get(QueueEntry, entry_id)
        if not entry:
            raise HTTPException(status_code=404, detail="Entry not found")
        agent_id   = entry.agent_id
        tenant_id  = entry.tenant_id
        queue_date = entry.queue_date
        service_id = entry.service_id
        entry.status = new_status
        s.add(entry)
        s.commit()

    recalculate_queue(tenant_id, agent_id, queue_date)

    if new_status == "InService":
        with Session(engine) as s:
            svc = s.get(Service, service_id)
            duration = svc.duration_minutes if svc else 60
        _schedule_15min_warning(tenant_id, agent_id, queue_date, duration)
    elif new_status in ("Done", "NoShow", "Cancelled"):
        _fire_youre_next(tenant_id, agent_id, queue_date)

    return {"status": "updated", "entry_id": entry_id, "new_status": new_status}


@app.post("/admin/queue/walkin")
def add_walkin(body: Dict[str, Any]):
    """Add a walk-in customer from the admin dashboard."""
    tenant_id        = body.get("tenant_id")
    service_id       = body.get("service_id")
    agent_id         = body.get("agent_id")
    name             = body.get("customer_name", "Walk-in")
    phone            = body.get("customer_phone", "")
    additional_names = body.get("additional_names", "")
    queue_date       = body.get("queue_date", today_str())

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
            raise HTTPException(status_code=400, detail="No available agents for this service")

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
            customer_number  = "walkin",
            customer_name    = name,
            customer_phone   = clean_phone,
            additional_names = additional_names,
            queue_date       = queue_date,
            estimated_start  = eta,
            position         = total_waiting + 1,
            booked_via       = "walkin"
        )
        s.add(entry)
        s.commit()
        s.refresh(entry)

        agent   = s.get(Agent, assigned_agent_id)
        service = s.get(Service, service_id)

        # Send WhatsApp confirmation if phone captured
        if clean_phone:
            add_line = f"\n👥 Also for: {additional_names}" if additional_names else ""
            send_text(tenant, clean_phone,
                f"✅ *You're in the queue at {tenant.business_name}!*\n\n"
                f"📍 Position: #{total_waiting + 1}\n"
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

@app.get("/admin/tenants")
def list_tenants():
    with Session(engine) as s:
        return s.exec(select(Tenant)).all()

@app.post("/admin/tenants")
def create_tenant(data: TenantCreate):
    tenant = Tenant(**data.dict())
    with Session(engine) as s:
        s.add(tenant)
        s.commit()
        s.refresh(tenant)
    return tenant


@app.patch("/admin/tenants/{tenant_id}")
def update_tenant(tenant_id: int, updates: Dict[str, Any]):
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

@app.get("/admin/services/{tenant_id}")
def list_services(tenant_id: int):
    with Session(engine) as s:
        return s.exec(
            select(Service).where(Service.tenant_id == tenant_id)
        ).all()


class ServiceCreate(SQLModel):
    tenant_id:         int
    name:              str
    duration_minutes:  int  = 60
    is_active:         bool = True


@app.post("/admin/services")
def create_service(data: ServiceCreate):
    service = Service(**data.dict())
    with Session(engine) as s:
        s.add(service)
        s.commit()
        s.refresh(service)
    return service


@app.patch("/admin/services/{service_id}")
def update_service(service_id: int, updates: Dict[str, Any]):
    with Session(engine) as s:
        svc = s.get(Service, service_id)
        if not svc:
            raise HTTPException(status_code=404, detail="Service not found")
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

@app.get("/admin/agents/{tenant_id}")
def list_agents(tenant_id: int):
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


@app.post("/admin/agents")
def create_agent(body: Dict[str, Any]):
    """Create an agent and assign their services in one call."""
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


@app.patch("/admin/agents/{agent_id}")
def update_agent(agent_id: int, updates: Dict[str, Any]):
    service_ids = updates.pop("service_ids", None)
    with Session(engine) as s:
        agent = s.get(Agent, agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")
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
    return {"status": "ok", "redis": redis_ok, "tenants": tenants}


@app.post("/admin/migrate-reset")
def migrate_reset():
    """Drop and recreate all tables using CASCADE to handle FK dependencies."""
    from sqlalchemy import text
    with engine.connect() as conn:
        conn.execute(text(
            "DROP TABLE IF EXISTS agentservice, queueentry, agent, service, tenant CASCADE"
        ))
        conn.commit()
    SQLModel.metadata.create_all(engine)
    return {"status": "done"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=9000)
