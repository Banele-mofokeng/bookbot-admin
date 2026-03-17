import os
import json
import requests
import redis
from datetime import datetime, timedelta, time
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import SQLModel, Field, create_engine, Session, select
from typing import Optional, Dict, Any

# =============================================================================
# 1. CONFIGURATION
# =============================================================================

DATABASE_URL = os.getenv("DATABASE_URL", "")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
if not DATABASE_URL:
    DATABASE_URL = "postgresql://postgres:0e6dc8c3d4a23e601efe@whatsapp_bot_booking-db:5432/whatsapp_bot"

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

engine = create_engine(DATABASE_URL)

# Redis client — sessions survive container restarts
redis_client = redis.from_url(REDIS_URL, decode_responses=True)

SESSION_TTL = 60 * 30  # 30 minutes of inactivity clears the session

# =============================================================================
# 2. DATABASE MODELS
# =============================================================================

class Tenant(SQLModel, table=True):
    """
    One row per business using the bot.
    For the MVP, rows are inserted manually via POST /admin/seed or /admin/tenants.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    business_name: str                      # e.g. "Glow Hair Studio"
    whatsapp_number: str                    # sender number e.g. "27813130871"
    evolution_instance: str                 # Evolution API instance name
    evolution_api_key: str                  # That instance's API key
    evolution_api_url: str                  # e.g. "https://evo.yourdomain.com"
    working_hours_start: int = 9           # 9 = 09:00
    working_hours_end: int = 17            # 17 = 17:00
    service_name: str = "Appointment"      # shown in confirmations
    is_active: bool = True


class Appointment(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    tenant_id: int = Field(foreign_key="tenant.id")  # scoped to a business
    customer_number: str
    customer_name: str
    service_type: str
    appointment_date: datetime
    status: str = "Confirmed"


def create_db_and_tables():
    SQLModel.metadata.create_all(engine)


# =============================================================================
# 3. APP INITIALIZATION
# =============================================================================

app = FastAPI(title="WhatsApp Booking Bot")

# Allow the React admin frontend to call the API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # tighten this to your admin domain in production
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup():
    print("🚀 Bot starting up...")
    create_db_and_tables()
    print("✅ Database tables verified/created.")


# =============================================================================
# 4. TENANT HELPERS
# =============================================================================

def get_tenant_by_number(whatsapp_number: str) -> Optional[Tenant]:
    """
    Resolve the tenant from the Evolution API 'sender' field.
    Strips @s.whatsapp.net / @lid suffixes before matching.
    """
    clean = whatsapp_number.replace("@s.whatsapp.net", "").replace("@lid", "")
    with Session(engine) as session:
        return session.exec(
            select(Tenant).where(
                Tenant.whatsapp_number == clean,
                Tenant.is_active == True
            )
        ).first()


# =============================================================================
# 5. REDIS SESSION HELPERS
# =============================================================================

def get_session(tenant_id: int, customer_num: str) -> dict:
    raw = redis_client.get(f"session:{tenant_id}:{customer_num}")
    return json.loads(raw) if raw else {"state": "idle"}


def set_session(tenant_id: int, customer_num: str, data: dict):
    redis_client.setex(f"session:{tenant_id}:{customer_num}", SESSION_TTL, json.dumps(data))


def clear_session(tenant_id: int, customer_num: str):
    redis_client.delete(f"session:{tenant_id}:{customer_num}")


# =============================================================================
# 6. MESSAGING HELPERS
# =============================================================================

def send_text(tenant: Tenant, number: str, text: str):
    """Send a plain text message using this tenant's Evolution API credentials."""
    url = f"{tenant.evolution_api_url.rstrip('/')}/message/sendText/{tenant.evolution_instance}"
    headers = {"apikey": tenant.evolution_api_key, "Content-Type": "application/json"}
    try:
        response = requests.post(url, json={"number": number, "text": text}, headers=headers, timeout=10)
        print(f"📡 [{tenant.business_name}] sendText {response.status_code}")
        return response.json()
    except Exception as e:
        print(f"❌ [{tenant.business_name}] API Error: {e}")
        return {"error": str(e)}


def send_main_menu(tenant: Tenant, number: str):
    send_text(tenant, number,
        f"*{tenant.business_name} 📅*\n\n"
        f"How can we help you today?\n\n"
        f"1️⃣ Book for Tomorrow\n"
        f"2️⃣ My Appointments\n\n"
        f"Reply with *1* or *2*"
    )


def send_slots_menu(tenant: Tenant, number: str, slots: list[str], date_str: str):
    lines = [f"{i+1}️⃣  {slot}" for i, slot in enumerate(slots)]
    send_text(tenant, number,
        f"*Available slots for {date_str}* 🕒\n\n"
        + "\n".join(lines)
        + "\n\nReply with the *number* of the slot you want."
    )


# =============================================================================
# 7. BOOKING HELPERS
# =============================================================================

def get_available_slots(tenant: Tenant, target_date: datetime.date) -> list[str]:
    """Returns free 1-hour slots for this tenant on the given date."""
    start = time(tenant.working_hours_start, 0)
    end = time(tenant.working_hours_end, 0)

    with Session(engine) as session:
        booked = session.exec(
            select(Appointment).where(
                Appointment.tenant_id == tenant.id,
                Appointment.appointment_date >= datetime.combine(target_date, time.min),
                Appointment.appointment_date <= datetime.combine(target_date, time.max),
                Appointment.status == "Confirmed"
            )
        ).all()
        booked_times = {a.appointment_date.strftime("%H:%M") for a in booked}

    slots = []
    curr = datetime.combine(target_date, start)
    while curr.time() < end:
        t_str = curr.strftime("%H:%M")
        if t_str not in booked_times:
            slots.append(t_str)
        curr += timedelta(hours=1)
    return slots


# =============================================================================
# 8. WEBHOOK HANDLER
# =============================================================================

@app.post("/webhook")
async def handle_webhook(request: Request):
    data = await request.json()
    event = data.get("event")

    if event != "messages.upsert":
        return {"status": "ignored"}

    msg_data = data.get("data", {})

    # Ignore outbound messages
    if msg_data.get("key", {}).get("fromMe"):
        return {"status": "ignored"}

    # ── Resolve tenant from the sender field ──────────────────────────────
    sender_raw = data.get("sender", "")
    tenant = get_tenant_by_number(sender_raw)
    if not tenant:
        print(f"⚠️  No active tenant for sender: {sender_raw}")
        return {"status": "unknown_tenant"}

    # ── Extract message text ──────────────────────────────────────────────
    customer_num = msg_data["key"]["remoteJid"]
    customer_name = msg_data.get("pushName", "Valued Customer")
    message_obj = msg_data.get("message", {})
    text = (
        message_obj.get("conversation")
        or message_obj.get("extendedTextMessage", {}).get("text")
        or ""
    ).strip().lower()

    session = get_session(tenant.id, customer_num)
    state = session.get("state", "idle")

    print(f"📩 [{tenant.business_name}] {customer_num} | state={state} | text='{text}'")

    # ── IDLE / MENU TRIGGERS ──────────────────────────────────────────────
    if state == "idle" or any(w in text for w in ["hi", "hello", "menu", "start", "hey"]):
        set_session(tenant.id, customer_num, {"state": "main_menu"})
        send_main_menu(tenant, customer_num)
        return {"status": "success"}

    # ── MAIN MENU RESPONSE ────────────────────────────────────────────────
    if state == "main_menu":
        if text == "1":
            tomorrow = datetime.now() + timedelta(days=1)
            date_str = tomorrow.strftime("%Y-%m-%d")
            slots = get_available_slots(tenant, tomorrow.date())

            if not slots:
                send_text(tenant, customer_num, "😔 Sorry, tomorrow is fully booked! Reply *menu* to go back.")
                clear_session(tenant.id, customer_num)
            else:
                set_session(tenant.id, customer_num, {
                    "state": "awaiting_slot",
                    "date": date_str,
                    "slots": slots
                })
                send_slots_menu(tenant, customer_num, slots, date_str)

        elif text == "2":
            with Session(engine) as db_session:
                appointments = db_session.exec(
                    select(Appointment).where(
                        Appointment.tenant_id == tenant.id,
                        Appointment.customer_number == customer_num,
                        Appointment.status == "Confirmed"
                    )
                ).all()

            if not appointments:
                send_text(tenant, customer_num, "📭 You have no upcoming appointments.\n\nReply *menu* to go back.")
            else:
                lines = [
                    f"📌 {a.appointment_date.strftime('%Y-%m-%d at %H:%M')} — {a.service_type}"
                    for a in appointments
                ]
                send_text(tenant, customer_num,
                    "*Your Appointments* 📋\n\n" + "\n".join(lines) + "\n\nReply *menu* to go back."
                )
            clear_session(tenant.id, customer_num)

        else:
            send_text(tenant, customer_num, "Please reply with *1* or *2* to choose an option.")

        return {"status": "success"}

    # ── SLOT SELECTION ────────────────────────────────────────────────────
    if state == "awaiting_slot":
        slots = session.get("slots", [])
        date_str = session.get("date", "")

        if text.isdigit():
            slot_index = int(text) - 1
            if 0 <= slot_index < len(slots):
                chosen_time = slots[slot_index]
                dt_obj = datetime.strptime(f"{date_str} {chosen_time}", "%Y-%m-%d %H:%M")

                with Session(engine) as db_session:
                    db_session.add(Appointment(
                        tenant_id=tenant.id,
                        customer_number=customer_num,
                        customer_name=customer_name,
                        service_type=tenant.service_name,
                        appointment_date=dt_obj
                    ))
                    db_session.commit()

                clear_session(tenant.id, customer_num)
                send_text(tenant, customer_num,
                    f"✅ *Booking Confirmed!*\n\n"
                    f"Hi {customer_name}, you're all set!\n"
                    f"📅 Date: {date_str}\n"
                    f"🕒 Time: {chosen_time}\n"
                    f"💼 Service: {tenant.service_name}\n\n"
                    f"Reply *menu* anytime to book again."
                )
            else:
                send_text(tenant, customer_num, f"Please reply with a number between 1 and {len(slots)}.")
        else:
            send_text(tenant, customer_num, "Please reply with a number to pick a time slot, e.g. *1*")

        return {"status": "success"}

    # ── FALLBACK ──────────────────────────────────────────────────────────
    clear_session(tenant.id, customer_num)
    send_main_menu(tenant, customer_num)
    return {"status": "success"}


# =============================================================================
# 9. ADMIN ENDPOINTS
# NOTE: These have no auth for the MVP. Lock them down before going public
#       either via EasyPanel network rules or by adding an API key check.
# =============================================================================

@app.get("/admin/tenants")
def list_tenants():
    with Session(engine) as session:
        return session.exec(select(Tenant)).all()


@app.post("/admin/tenants")
def create_tenant(tenant: Tenant):
    """Register a new business tenant manually."""
    with Session(engine) as session:
        session.add(tenant)
        session.commit()
        session.refresh(tenant)
    return tenant


@app.patch("/admin/tenants/{tenant_id}")
def update_tenant(tenant_id: int, updates: Dict[str, Any]):
    """Update any fields on a tenant — working hours, name, active status, etc."""
    with Session(engine) as session:
        tenant = session.get(Tenant, tenant_id)
        if not tenant:
            raise HTTPException(status_code=404, detail="Tenant not found")
        for key, value in updates.items():
            if hasattr(tenant, key):
                setattr(tenant, key, value)
        session.add(tenant)
        session.commit()
        session.refresh(tenant)
    return tenant


@app.get("/admin/appointments/{tenant_id}")
def list_appointments(tenant_id: int):
    """All confirmed appointments for a given tenant."""
    with Session(engine) as session:
        return session.exec(
            select(Appointment).where(
                Appointment.tenant_id == tenant_id,
                Appointment.status == "Confirmed"
            ).order_by(Appointment.appointment_date)
        ).all()


@app.patch("/admin/appointments/{appointment_id}/cancel")
def cancel_appointment(appointment_id: int):
    with Session(engine) as session:
        appt = session.get(Appointment, appointment_id)
        if not appt:
            raise HTTPException(status_code=404, detail="Appointment not found")
        appt.status = "Cancelled"
        session.add(appt)
        session.commit()
    return {"status": "cancelled", "id": appointment_id}

@app.post("/admin/migrate-reset")
def migrate_reset():
    """
    Drops and recreates all tables. 
    WARNING: deletes all data. Only use before you have real bookings.
    """
    SQLModel.metadata.drop_all(engine)
    SQLModel.metadata.create_all(engine)
    return {"status": "done"}


# =============================================================================
# 10. SEED ENDPOINT  — inserts your first tenant for testing
#     Hit POST /admin/seed once after deploying, then leave it alone.
# =============================================================================

@app.post("/admin/seed")
def seed_tenant():
    sample = Tenant(
        business_name="Banele's Booking Bot",
        whatsapp_number="27813130871",
        evolution_instance="Banele",
        evolution_api_key="F4CDDB623FCA-4AF8-AC71-F7476D9A13D6",
        evolution_api_url="https://whatsapp-1-evolution-api.a8x5ve.easypanel.host",
        working_hours_start=9,
        working_hours_end=17,
        service_name="General Consultation",
        is_active=True
    )
    with Session(engine) as session:
        existing = session.exec(
            select(Tenant).where(Tenant.whatsapp_number == sample.whatsapp_number)
        ).first()
        if existing:
            return {"status": "already_exists", "tenant": existing}
        session.add(sample)
        session.commit()
        session.refresh(sample)
    return {"status": "created", "tenant": sample}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=9000)
