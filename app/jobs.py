"""Scheduled work: proactive customer notifications and the midnight sweep."""
from datetime import datetime, timedelta
from typing import Optional
from sqlmodel import Session, select

from app import config, core
from app.core import format_eta, normalize_number
from app.db import engine
from app.models import Tenant, Service, Agent, Order, QueueEntry
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

def _sweep_stale_orders(today: str) -> int:
    """
    Cancel every unclosed order from a day before today. Returns how many.

    Never auto-Collected: that would book money as taken for food that may
    still be sitting on the pass. Cancelled and tagged as ours, so takings and
    reporting stay honest about what actually got sold.

    A blank order_date is skipped rather than swept. It sorts before every real
    date, so a range sweep would close rows whose day is simply unknown — and
    the old equality sweep never touched them.
    """
    total = 0
    while True:
        with Session(engine) as s:
            batch = s.exec(
                select(Order).where(
                    Order.order_date < today,
                    Order.order_date != "",
                    Order.status.in_(["Placed", "Preparing", "Ready"]),
                ).limit(config.SWEEP_BATCH)
            ).all()
            for order in batch:
                order.status      = "Cancelled"
                order.closed_by   = "system"
                order.finished_at = core.now()
                s.add(order)
            s.commit()
        total += len(batch)
        # Each pass moves its rows out of the filtered statuses, so the loop
        # always makes progress and a short batch means the table is drained.
        if len(batch) < config.SWEEP_BATCH:
            return total


def _sweep_stale_entries(today: str) -> int:
    """
    Close every queue entry left open on a day before today. Returns how many.

    Never auto-marked Done — staff never closed it, so it does not count as
    completed work. Abandoned entries close as NoShow, tagged as ours rather
    than the customer's: reporting keeps these out of the no-show rate, because
    nobody knows whether this customer turned up, only that the shop never
    closed the entry.
    """
    total = 0
    while True:
        with Session(engine) as s:
            batch = s.exec(
                select(QueueEntry).where(
                    QueueEntry.queue_date < today,
                    QueueEntry.queue_date != "",
                    QueueEntry.status.in_(["Waiting", "InService"]),
                ).limit(config.SWEEP_BATCH)
            ).all()
            for entry in batch:
                entry.status      = "NoShow"
                entry.closed_by   = "system"
                entry.finished_at = core.now()
                s.add(entry)
            s.commit()
        total += len(batch)
        if len(batch) < config.SWEEP_BATCH:
            return total


async def midnight_reset_job():
    """
    Runs at 00:01 every night, and once more on startup. Closes out every day
    before today that nobody closed — not yesterday alone.

    Sweeping a range rather than a single date is what makes the job durable.
    The 00:01 cron does not fire retroactively, so a process down across two
    midnights used to leave the older day open forever: its entries stay
    Waiting, keep occupying their agent in every backlog and ETA calculation,
    and never reach reporting as closed. One day's outage was untidy; anything
    booked further ahead than today makes it permanent.

    Today itself is never touched — the shop is still working it.
    """
    print("🌙 Running midnight reset...")
    today = core.today_str()

    stale_orders = _sweep_stale_orders(today)
    leftover     = _sweep_stale_entries(today)

    print(f"🌙 Reset complete. {leftover} entries and "
          f"{stale_orders} orders closed.")

