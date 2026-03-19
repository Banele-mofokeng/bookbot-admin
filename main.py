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
    scheduler.add_job(notification_job,  "interval", minutes=2,  id="notifications")
    scheduler.add_job(midnight_reset_job, "cron",    hour=0, minute=1, id="midnight_reset")
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
    with Session(engine) as s:
        for i, agent in enumerate(agents):
            backlog = get_agent_backlog_minutes(agent.id, tenant.id, queue_date)
            eta     = calculate_estimated_start(tenant, agent.id, queue_date, backlog)
            lines.append(
                f"{i+1}️⃣ {agent.name}  _(next free around {format_eta(eta)})_"
            )
    lines.append(f"{len(agents)+1}️⃣ No preference _(assign me to earliest)_")

    send_text(tenant, number,
        f"*Do you have a preferred {tenant.agent_label.lower()}?* 👤\n\n"
        + "\n".join(lines)
        + f"\n\nReply with a number."
    )


def send_date_menu(tenant: Tenant, number: str):
    today   = now().date()
    options = []
    for delta in range(14):
        d = today + timedelta(days=delta)
        if len(options) == tenant.advance_days + 1:
            break
        options.append(d)

    lines = []
    for i, d in enumerate(options):
        label = d.strftime("%a %d %b")
        if d == today:
            label += " _(today)_"
        elif d == today + timedelta(days=1):
            label += " _(tomorrow)_"
        lines.append(f"{i+1}️⃣ {label}")

    set_session(tenant.id, number, {
        "state": "awaiting_date",
        "date_options": [d.isoformat() for d in options]
    })

    send_text(tenant, number,
        f"*Which day would you like to queue for?* 📅\n\n"
        + "\n".join(lines)
        + "\n\nReply with the *number* of the day."
    )


# =============================================================================
# 8. BACKGROUND JOBS
# =============================================================================

async def notification_job():
    """Runs every 2 minutes. Checks queue positions and sends 'you're 2 away' and 'you're next'."""
    print("🔔 Running notification check...")
    today = today_str()

    with Session(engine) as s:
        waiting = s.exec(
            select(QueueEntry).where(
                QueueEntry.queue_date == today,
                QueueEntry.status     == "Waiting"
            )
        ).all()

        for entry in waiting:
            tenant = s.get(Tenant, entry.tenant_id)
            if not tenant:
                continue

            # Count how many Waiting entries are ahead on same agent
            ahead = s.exec(
                select(QueueEntry).where(
                    QueueEntry.agent_id   == entry.agent_id,
                    QueueEntry.tenant_id  == entry.tenant_id,
                    QueueEntry.queue_date == today,
                    QueueEntry.status     == "Waiting",
                    QueueEntry.joined_at  < entry.joined_at
                )
            ).all()

            ahead_count = len(ahead)

            # "You're next" notification
            if ahead_count == 0 and not entry.notified_next:
                agent = s.get(Agent, entry.agent_id)
                send_text(tenant, entry.customer_number,
                    f"🚀 *You're up next at {tenant.business_name}!*\n\n"
                    f"Head over now — {agent.name if agent else tenant.agent_label} is ready for you.\n\n"
                    f"💼 {s.get(Service, entry.service_id).name if s.get(Service, entry.service_id) else ''}"
                )
                entry.notified_next = True
                s.add(entry)

            # "2 away" notification
            elif ahead_count == 2 and not entry.notified_two_away:
                eta = format_eta(entry.estimated_start)
                send_text(tenant, entry.customer_number,
                    f"⏳ *Heads up from {tenant.business_name}!*\n\n"
                    f"There are *2 people* ahead of you.\n"
                    f"Estimated time: *{eta}*\n\n"
                    f"Start making your way over 🚶"
                )
                entry.notified_two_away = True
                s.add(entry)

        s.commit()
    print("🔔 Notification check done.")


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

@app.post("/webhook")
async def handle_webhook(request: Request):
    data = await request.json()

    if data.get("event") != "messages.upsert":
        return {"status": "ignored"}

    msg_data = data.get("data", {})
    if msg_data.get("key", {}).get("fromMe"):
        return {"status": "ignored"}

    # ── Resolve tenant ────────────────────────────────────────────────────
    tenant = get_tenant_by_number(data.get("sender", ""))
    if not tenant:
        return {"status": "unknown_tenant"}

    # ── Extract message ───────────────────────────────────────────────────
    customer_num  = msg_data["key"]["remoteJid"]
    customer_name = msg_data.get("pushName", "Customer")
    message_obj   = msg_data.get("message", {})
    text = (
        message_obj.get("conversation")
        or message_obj.get("extendedTextMessage", {}).get("text")
        or ""
    ).strip().lower()

    sess  = get_session(tenant.id, customer_num)
    state = sess.get("state", "idle")

    print(f"📩 [{tenant.business_name}] {customer_num} | {state} | '{text}'")

    # ── IDLE / MENU TRIGGERS ──────────────────────────────────────────────
    if state == "idle" or any(w in text for w in ["hi","hello","menu","start","hey","status"]):
        set_session(tenant.id, customer_num, {"state": "main_menu"})
        send_main_menu(tenant, customer_num)
        return {"status": "success"}

    # ── MAIN MENU ─────────────────────────────────────────────────────────
    if state == "main_menu":
        if text == "1":
            # Start queue flow — pick a date first
            if tenant.advance_days == 0:
                # Today only — skip date picker
                set_session(tenant.id, customer_num, {
                    "state": "awaiting_service",
                    "queue_date": today_str()
                })
                with Session(engine) as s:
                    services = s.exec(
                        select(Service).where(
                            Service.tenant_id == tenant.id,
                            Service.is_active == True
                        )
                    ).all()
                if not services:
                    send_text(tenant, customer_num,
                        f"No services are configured yet. Please contact {tenant.business_name} directly.")
                    clear_session(tenant.id, customer_num)
                else:
                    set_session(tenant.id, customer_num, {
                        "state": "awaiting_service",
                        "queue_date": today_str(),
                        "service_ids": [svc.id for svc in services]
                    })
                    send_service_menu(tenant, customer_num, services)

            else:
                send_date_menu(tenant, customer_num)

        elif text == "2":
            # Check queue status — search upcoming dates not just today
            today = today_str()
            with Session(engine) as s:
                entry = s.exec(
                    select(QueueEntry).where(
                        QueueEntry.tenant_id      == tenant.id,
                        QueueEntry.customer_number == customer_num,
                        QueueEntry.queue_date      >= today,
                        QueueEntry.status.in_(["Waiting", "InService"])
                    ).order_by(QueueEntry.queue_date, QueueEntry.joined_at)
                ).first()

                if not entry:
                    send_text(tenant, customer_num,
                        f"You're not currently in the queue.\n\nReply *menu* to join.")
                else:
                    agent   = s.get(Agent, entry.agent_id)
                    service = s.get(Service, entry.service_id)

                    # Count people ahead
                    ahead = s.exec(
                        select(QueueEntry).where(
                            QueueEntry.agent_id   == entry.agent_id,
                            QueueEntry.tenant_id  == tenant.id,
                            QueueEntry.queue_date == today,
                            QueueEntry.status     == "Waiting",
                            QueueEntry.joined_at  < entry.joined_at
                        )
                    ).all()

                    send_text(tenant, customer_num,
                        f"*Your Queue Status* 📍\n\n"
                        f"🔢 Position: #{entry.position}\n"
                        f"👤 {tenant.agent_label}: {agent.name if agent else 'TBD'}\n"
                        f"💼 {tenant.service_label}: {service.name if service else 'TBD'}\n"
                        f"⏰ Estimated time: {format_eta(entry.estimated_start)}\n"
                        f"👥 People ahead: {len(ahead)}\n\n"
                        f"Reply *menu* for more options."
                    )
            clear_session(tenant.id, customer_num)

        elif text == "3":
            # Leave the queue — search upcoming dates
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
                    send_text(tenant, customer_num,
                        "You're not currently in the queue.\n\nReply *menu* to go back.")
                else:
                    agent_id = entry.agent_id
                    entry.status = "Cancelled"
                    s.add(entry)
                    s.commit()
                    recalculate_queue(tenant.id, agent_id, today)
                    send_text(tenant, customer_num,
                        f"✅ You've been removed from the queue at *{tenant.business_name}*.\n\n"
                        f"Reply *menu* anytime to rejoin."
                    )
                    if tenant.owner_number:
                        agent   = s.get(Agent, entry.agent_id)
                        service = s.get(Service, entry.service_id)
                        date_display = datetime.strptime(entry.queue_date, "%Y-%m-%d").strftime("%a %d %b")
                        send_text(tenant, normalize_number(tenant.owner_number),
                            f"\u274c *Queue Cancellation*\n\n"
                            f"\U0001f464 {entry.customer_name}\n"
                            f"\U0001f4bc {service.name if service else ''}\n"
                            f"\U0001f4c5 {date_display}"
                        )
            clear_session(tenant.id, customer_num)

        else:
            send_text(tenant, customer_num, "Please reply with *1*, *2*, or *3*.")

        return {"status": "success"}

    # ── DATE SELECTION ────────────────────────────────────────────────────
    if state == "awaiting_date":
        date_options = sess.get("date_options", [])
        if text.isdigit() and 1 <= int(text) <= len(date_options):
            chosen_date = date_options[int(text) - 1]
            with Session(engine) as s:
                services = s.exec(
                    select(Service).where(
                        Service.tenant_id == tenant.id,
                        Service.is_active == True
                    )
                ).all()
            if not services:
                send_text(tenant, customer_num,
                    f"No services configured. Contact {tenant.business_name} directly.")
                clear_session(tenant.id, customer_num)
            else:
                set_session(tenant.id, customer_num, {
                    "state": "awaiting_service",
                    "queue_date": chosen_date,
                    "service_ids": [svc.id for svc in services]
                })
                send_service_menu(tenant, customer_num, services)
        else:
            send_text(tenant, customer_num,
                f"Please reply with a number between 1 and {len(date_options)}.")
        return {"status": "success"}

    # ── SERVICE SELECTION ─────────────────────────────────────────────────
    if state == "awaiting_service":
        service_ids = sess.get("service_ids", [])
        queue_date  = sess.get("queue_date", today_str())

        if text.isdigit() and 1 <= int(text) <= len(service_ids):
            chosen_service_id = service_ids[int(text) - 1]

            # Find capable agents
            with Session(engine) as s:
                capable_ids = [
                    row.agent_id for row in s.exec(
                        select(AgentService).where(
                            AgentService.service_id == chosen_service_id
                        )
                    ).all()
                ]
                agents = s.exec(
                    select(Agent).where(
                        Agent.id.in_(capable_ids),
                        Agent.tenant_id == tenant.id,
                        Agent.is_active == True
                    )
                ).all()

            if not agents:
                send_text(tenant, customer_num,
                    f"Sorry, no {tenant.agent_label.lower()}s are available for that service right now.")
                clear_session(tenant.id, customer_num)
            else:
                set_session(tenant.id, customer_num, {
                    "state": "awaiting_agent",
                    "queue_date": queue_date,
                    "service_id": chosen_service_id,
                    "agent_ids": [a.id for a in agents]
                })
                send_agent_menu(tenant, customer_num, agents, queue_date, chosen_service_id)
        else:
            send_text(tenant, customer_num,
                f"Please reply with a number between 1 and {len(service_ids)}.")
        return {"status": "success"}

    # ── AGENT SELECTION ───────────────────────────────────────────────────
    if state == "awaiting_agent":
        agent_ids   = sess.get("agent_ids", [])
        service_id  = sess.get("service_id")
        queue_date  = sess.get("queue_date", today_str())
        no_pref_idx = len(agent_ids) + 1  # last option is "no preference"

        if text.isdigit():
            choice = int(text)
            preferred_agent_id = None

            if 1 <= choice <= len(agent_ids):
                preferred_agent_id = agent_ids[choice - 1]
            elif choice != no_pref_idx:
                send_text(tenant, customer_num,
                    f"Please reply with a number between 1 and {no_pref_idx}.")
                return {"status": "success"}

            # Assign agent
            assigned_agent_id = assign_agent(tenant, service_id, preferred_agent_id, queue_date)

            if not assigned_agent_id:
                send_text(tenant, customer_num,
                    f"Sorry, no {tenant.agent_label.lower()}s are available. Please try again later.")
                clear_session(tenant.id, customer_num)
                return {"status": "success"}

            # Calculate position + ETA
            backlog = get_agent_backlog_minutes(assigned_agent_id, tenant.id, queue_date)
            eta     = calculate_estimated_start(tenant, assigned_agent_id, queue_date, backlog)

            # Count total waiting across whole tenant queue for display position
            print(f"💾 Saving queue entry | tenant={tenant.id} service={service_id} agent={assigned_agent_id} date={queue_date} customer={customer_num}")
            with Session(engine) as s:
                total_waiting = len(s.exec(
                    select(QueueEntry).where(
                        QueueEntry.tenant_id  == tenant.id,
                        QueueEntry.queue_date == queue_date,
                        QueueEntry.status     == "Waiting"
                    )
                ).all())

                position = total_waiting + 1

                entry = QueueEntry(
                    tenant_id          = tenant.id,
                    service_id         = service_id,
                    agent_id           = assigned_agent_id,
                    preferred_agent_id = preferred_agent_id,
                    customer_number    = customer_num,
                    customer_name      = customer_name,
                    queue_date         = queue_date,
                    estimated_start    = eta,
                    position           = position,
                    booked_via         = "whatsapp"
                )
                s.add(entry)
                s.commit()
                s.refresh(entry)

                agent   = s.get(Agent, assigned_agent_id)
                service = s.get(Service, service_id)

            clear_session(tenant.id, customer_num)

            # Confirm to customer
            date_display = datetime.strptime(queue_date, "%Y-%m-%d").strftime("%a %d %b")
            send_text(tenant, customer_num,
                f"✅ *You're in the queue!*\n\n"
                f"📍 Position: #{position}\n"
                f"👤 {tenant.agent_label}: {agent.name if agent else 'TBD'}\n"
                f"💼 {tenant.service_label}: {service.name if service else 'TBD'}\n"
                f"📅 Date: {date_display}\n"
                f"⏰ Estimated time: {format_eta(eta)}\n\n"
                f"We'll notify you when you're 2 away and when you're next.\n"
                f"Reply *status* anytime to check your position."
            )

            # Notify owner
            if tenant.owner_number:
                send_text(tenant, normalize_number(tenant.owner_number),
                    f"🔔 *New Queue Entry*\n\n"
                    f"👤 {customer_name}\n"
                    f"💼 {service.name if service else ''}\n"
                    f"👷 {agent.name if agent else ''}\n"
                    f"📍 Position #{position} | ⏰ {format_eta(eta)}"
                )
        else:
            send_text(tenant, customer_num,
                f"Please reply with a number to choose your {tenant.agent_label.lower()}.")
        return {"status": "success"}

    # ── FALLBACK ──────────────────────────────────────────────────────────
    clear_session(tenant.id, customer_num)
    send_main_menu(tenant, customer_num)
    return {"status": "success"}


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
    """
    Update a queue entry status.
    Valid transitions: Waiting→InService, InService→Done, Waiting/InService→NoShow
    After update, recalculates queue for that agent.
    """
    new_status = body.get("status")
    if new_status not in ["InService", "Done", "NoShow", "Cancelled"]:
        raise HTTPException(status_code=400, detail="Invalid status")

    with Session(engine) as s:
        entry = s.get(QueueEntry, entry_id)
        if not entry:
            raise HTTPException(status_code=404, detail="Entry not found")

        tenant     = s.get(Tenant, entry.tenant_id)
        agent      = s.get(Agent, entry.agent_id)
        service    = s.get(Service, entry.service_id)
        agent_id   = entry.agent_id
        tenant_id  = entry.tenant_id
        queue_date = entry.queue_date

        entry.status = new_status
        s.add(entry)
        s.commit()

    # Recalculate after commit
    recalculate_queue(tenant_id, agent_id, queue_date)
    return {"status": "updated", "entry_id": entry_id, "new_status": new_status}


@app.post("/admin/queue/walkin")
def add_walkin(body: Dict[str, Any]):
    """Add a walk-in customer directly from the admin dashboard."""
    tenant_id   = body.get("tenant_id")
    service_id  = body.get("service_id")
    agent_id    = body.get("agent_id")        # optional preferred agent
    name        = body.get("customer_name", "Walk-in")
    queue_date  = body.get("queue_date", today_str())

    with Session(engine) as s:
        tenant = s.get(Tenant, tenant_id)
        if not tenant:
            raise HTTPException(status_code=404, detail="Tenant not found")

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

        entry = QueueEntry(
            tenant_id       = tenant_id,
            service_id      = service_id,
            agent_id        = assigned_agent_id,
            customer_number = "walkin",
            customer_name   = name,
            queue_date      = queue_date,
            estimated_start = eta,
            position        = total_waiting + 1,
            booked_via      = "walkin"
        )
        s.add(entry)
        s.commit()
        s.refresh(entry)

        agent   = s.get(Agent, assigned_agent_id)
        service = s.get(Service, service_id)

    return {
        "id":              entry.id,
        "customer_name":   entry.customer_name,
        "service":         service.name if service else "—",
        "agent":           agent.name if agent else "—",
        "position":        entry.position,
        "estimated_start": format_eta(entry.estimated_start),
    }


# =============================================================================
# 11. ADMIN — TENANTS
# =============================================================================

@app.get("/admin/tenants")
def list_tenants():
    with Session(engine) as s:
        return s.exec(select(Tenant)).all()


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