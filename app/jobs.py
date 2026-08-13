"""Scheduled work: proactive customer notifications and the midnight sweep."""
from datetime import datetime, timedelta
from typing import Optional
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app import config, core
from app.core import format_eta, normalize_number
from app.db import engine
from app.models import (Tenant, Service, Agent, NotificationLog, Order,
                        QueueEntry)
from app.messaging import send_text

# =============================================================================
# 8. BACKGROUND JOBS
# =============================================================================

def get_notify_number(entry) -> str:
    """Returns the number to notify. Handles walk-ins with captured phone."""
    if entry.booked_via == "walkin":
        return normalize_number(entry.customer_phone) if entry.customer_phone else ""
    return entry.customer_number


# Notification rules. A rule names one notification for one entry. It is stored
# as data, never interpolated into SQL, so new ones cost nothing but a name —
# which is what a reminder ladder (T-24h, T-2h, T-30m) will need.
RULE_TWO_AWAY   = "two_away"
RULE_YOURE_NEXT = "youre_next"

# The boolean columns these two rules used to live in. Written alongside the log
# so a rollout where old and new processes overlap cannot re-send: the old code
# reads the column and knows nothing about the log. Delete this map, and the
# columns, once no process reading them is still running.
_LEGACY_COLUMNS = {
    RULE_TWO_AWAY:   "notified_two_away",
    RULE_YOURE_NEXT: "notified_next",
}


def _already_notified(s: Session, entry_id: int, rule: str) -> bool:
    """Whether this notification has already been claimed for this entry."""
    return s.exec(
        select(NotificationLog).where(
            NotificationLog.entry_id == entry_id,
            NotificationLog.rule     == rule,
        )
    ).first() is not None


def _claim_notification(entry_id: int, rule: str) -> bool:
    """
    Atomically claim the right to send one notification for one entry.

    Inserts the (entry, rule) pair and reports whether this caller is the one
    that got it in. The unique constraint arbitrates, so the reconciler and a
    live status change racing on the same entry cannot both decide to send —
    a read-then-write would let them. Returns False if already claimed.
    """
    with Session(engine) as s:
        s.add(NotificationLog(entry_id=entry_id, rule=rule, sent_at=core.now()))
        try:
            s.commit()
        except IntegrityError:
            s.rollback()
            return False

    # Mirror into the legacy column, for as long as one exists for this rule.
    # Deliberately after the claim: the log is the decision, this is a copy.
    column = _LEGACY_COLUMNS.get(rule)
    if column:
        with Session(engine) as s:
            entry = s.get(QueueEntry, entry_id)
            if entry:
                setattr(entry, column, True)
                s.add(entry)
                s.commit()
    return True


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
        if not next_entry or _already_notified(s, next_entry.id, RULE_TWO_AWAY):
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
    if not _claim_notification(entry_id, RULE_TWO_AWAY):
        return
    send_text(tenant, notify_to, body, dedupe_key=f"{RULE_TWO_AWAY}:{entry_id}")


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
    state, and once this entry is claimed the warning is suppressed by claiming
    RULE_TWO_AWAY alongside it.
    """
    with Session(engine) as s:
        tenant = s.get(Tenant, tenant_id)
        if not tenant:
            return
        next_entry = _next_waiting(s, tenant_id, agent_id, queue_date)
        if not next_entry or _already_notified(s, next_entry.id, RULE_YOURE_NEXT):
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
    if not _claim_notification(entry_id, RULE_YOURE_NEXT):
        return
    # Being told "you're up next" makes the 15-minute warning redundant.
    _claim_notification(entry_id, RULE_TWO_AWAY)
    send_text(tenant, notify_to, body, dedupe_key=f"{RULE_YOURE_NEXT}:{entry_id}")

# =============================================================================
# APPOINTMENT REMINDERS
# =============================================================================
# Derived from state on every tick, exactly like the queue's 15-minute warning
# and for the same reason: there is no persisted timer, so a restart, a crash or
# a missed tick cannot lose a reminder.

def reminder_rungs(offsets: list, start: datetime) -> list:
    """
    Each configured offset paired with the span during which it is still the
    truthful thing to say: [(offset_minutes, due_from, due_until)].

    Two things close that window, and the earlier one wins.

    The next rung taking over. Once "in about two hours" is due, "tomorrow" has
    nothing left to say.

    Its own lateness budget — half its lead time. A rung's message is worded
    from its nominal offset, so how late it may go out is bounded by when that
    wording stops being true: "your appointment is tomorrow" survives eleven
    hours of outage and is absurd twenty-three hours late, by which point the
    appointment is today. Without this, switching reminders on with a full diary
    would send every customer a day-ahead notice for appointments starting
    within the hour, because the day-ahead rung stays open until T-2h.

    A rung whose whole window is missed is not sent late. It is skipped, and
    the next rung says something true instead.
    """
    rungs = []
    for i, offset in enumerate(offsets):
        due_from  = start - timedelta(minutes=offset)
        next_rung = (start - timedelta(minutes=offsets[i + 1])
                     if i + 1 < len(offsets) else start)
        grace = max(offset * config.REMINDER_GRACE_FRACTION,
                    config.REMINDER_MIN_GRACE_MINUTES)
        rungs.append((offset, due_from,
                      min(next_rung, due_from + timedelta(minutes=grace))))
    return rungs


def _due_rung(offsets: list, start: datetime, now: datetime):
    """The rung that is currently truthful, or None."""
    for offset, due_from, due_until in reminder_rungs(offsets, start):
        if due_from <= now < due_until:
            return offset
    return None


def _within_business_hours(tenant: Tenant, when: datetime) -> bool:
    """
    Reminders go out during the business's own day, never at 03:00.

    A rung stays due until the next one takes over, so one that comes due
    overnight simply goes out when the shop opens. Only a rung whose entire
    window sits outside opening hours is lost — which takes a deliberately odd
    configuration, and losing it beats waking a customer.
    """
    return tenant.queue_opens <= when.hour < tenant.queue_closes


def _reminder_body(tenant: Tenant, entry: QueueEntry, offset: int,
                   service_name: str, agent_name: str) -> str:
    return (
        f"⏰ *Reminder — {tenant.business_name}*\n\n"
        f"Your appointment is {core.describe_gap(offset)}.\n\n"
        f"📅 {entry.queue_date}\n"
        f"🕐 {format_eta(entry.estimated_start)}"
        f"{f' – {format_eta(entry.slot_end)}' if entry.slot_end else ''}\n"
        f"💼 {service_name}\n"
        f"👤 {tenant.agent_label}: {agent_name}\n\n"
        f"Reply *3* if you can no longer make it."
    )


def reminder_sweep() -> int:
    """
    Send any appointment reminder that has come due. Returns how many.

    Only fixed entries — a queue entry has no promised time to remind anyone
    about, and keeps the 15-minute warning it already had. Nothing here changes
    for a queue or ordering tenant.
    """
    now = core.now()
    due = []

    with Session(engine) as s:
        entries = s.exec(
            select(QueueEntry).where(
                QueueEntry.is_fixed   == True,
                QueueEntry.status     == "Waiting",
                QueueEntry.queue_date >= core.today_str(),
            )
        ).all()
        if not entries:
            return 0

        tenants = {
            t.id: t for t in s.exec(
                select(Tenant).where(
                    Tenant.id.in_({e.tenant_id for e in entries})
                )
            ).all()
        }

        for entry in entries:
            tenant = tenants.get(entry.tenant_id)
            if not tenant or not entry.estimated_start:
                continue
            offsets = core.parse_reminder_offsets(tenant.reminder_offsets_minutes)
            if not offsets:
                continue
            offset = _due_rung(offsets, entry.estimated_start, now)
            if offset is None:
                continue
            if not _within_business_hours(tenant, now):
                continue

            rule = f"reminder:{offset}"
            if _already_notified(s, entry.id, rule):
                continue
            notify_to = get_notify_number(entry)
            if not notify_to:
                continue

            service = s.get(Service, entry.service_id)
            agent   = s.get(Agent, entry.agent_id)
            due.append((
                tenant, entry.id, rule, notify_to,
                _reminder_body(tenant, entry, offset,
                               service.name if service else "",
                               agent.name if agent else tenant.agent_label),
            ))

    fired = 0
    for tenant, entry_id, rule, notify_to, body in due:
        # Claim before queueing, so two workers cannot both send it.
        if not _claim_notification(entry_id, rule):
            continue
        send_text(tenant, notify_to, body, dedupe_key=f"{rule}:{entry_id}")
        fired += 1
    return fired


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

