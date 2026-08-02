"""Scheduled work: proactive customer notifications and the midnight sweep."""
from datetime import datetime, timedelta
from typing import Optional
from sqlmodel import Session, select

from app import core
from app.core import format_eta, normalize_number
from app.db import engine
from app.models import Tenant, Service, Agent, QueueEntry
from app.messaging import send_text

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

    Runs every config.RECONCILE_SECONDS. For every agent currently serving someone, if
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
                QueueEntry.queue_date >= core.yesterday_str(),
            )
        ).all()
        for entry in in_service:
            svc      = s.get(Service, entry.service_id)
            duration = svc.duration_minutes if svc else 60
            start    = entry.estimated_start or entry.joined_at
            if core.now() >= start + timedelta(minutes=duration - 15):
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
    yesterday = core.yesterday_str()

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
            entry.finished_at = core.now()
            s.add(entry)

        s.commit()
    print(f"🌙 Reset complete. {len(leftover)} entries closed.")

