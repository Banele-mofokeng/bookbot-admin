"""Outbound WhatsApp: the outbox table, the drain worker, and the menu
builders the conversation flow sends.
"""
import asyncio
import httpx
from datetime import datetime, timedelta
from typing import Optional, List
from sqlmodel import Session, select

from app import config, core
from app.core import format_duration, format_eta
from app.db import engine
from app.models import (Tenant, Service, Agent, QueueEntry, OutboxMessage)
from app.queue_engine import get_working_windows
from app.sessions import set_session

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
            # binds core.now() at class-definition time.
            created_at      = core.now(),
            next_attempt_at = core.now(),
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
        timeout=config.OUTBOX_SEND_TIMEOUT,
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
                OutboxMessage.next_attempt_at <= core.now(),
            ).order_by(OutboxMessage.id).limit(config.OUTBOX_BATCH)
        ).all()

        for msg in due:
            tenant = s.get(Tenant, msg.tenant_id)
            msg.attempts += 1
            try:
                if not tenant:
                    raise RuntimeError(f"tenant {msg.tenant_id} no longer exists")
                await _send_one(client, tenant, msg)
                msg.status  = "Sent"
                msg.sent_at = core.now()
                sent += 1
            except Exception as exc:
                msg.last_error = str(exc)[:500]
                if msg.attempts >= config.OUTBOX_MAX_ATTEMPTS:
                    msg.status = "Failed"
                    print(f"❌ outbox giving up on msg {msg.id} after "
                          f"{msg.attempts} attempts: {exc}")
                else:
                    # Exponential backoff, capped at 5 min.
                    delay = min(300, 5 * (2 ** (msg.attempts - 1)))
                    msg.next_attempt_at = core.now() + timedelta(seconds=delay)
                    print(f"⚠️  outbox retry {msg.attempts}/{config.OUTBOX_MAX_ATTEMPTS} "
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
            await asyncio.sleep(config.OUTBOX_POLL_SECONDS)


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
            finish_time = (in_service.estimated_start or core.now()) + timedelta(minutes=duration)
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
    current_hour = core.now().hour
    return tenant.queue_opens <= current_hour < tenant.queue_closes


def send_date_menu(tenant: Tenant, number: str):
    today      = core.now().date()
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
