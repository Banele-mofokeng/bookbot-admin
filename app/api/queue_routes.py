"""Dashboard queue reads, status changes, and walk-in booking."""
from datetime import datetime, timedelta, time, date
from typing import Optional, Dict, Any, List
from fastapi import APIRouter, HTTPException, Depends
from sqlmodel import SQLModel, Session, select

from app import config, core
from app.auth import get_current_user, require_super, ensure_tenant_access
from app.db import engine
from app.models import (Tenant, User, Service, Agent, AgentService,
                        AgentSchedule, AgentBlock, QueueEntry, OutboxMessage)

# Same guard the single admin router used to carry: every route below
# authenticates, then scopes to the caller's tenant.
router = APIRouter(dependencies=[Depends(get_current_user)])

from app.core import format_eta, normalize_number
from app.jobs import _fire_youre_next
from app.messaging import queue_is_open_today, send_text
from app.queue_engine import (_entry_end, _service_map, assign_agent,
                              calculate_estimated_start, cancel_party,
                              find_walkin_insert_joined_at,
                              get_agent_backlog_minutes,
                              get_working_windows_for, recalculate_queue)
from app.sessions import booking_lock

@router.get("/admin/queue/{tenant_id}")
def get_queue(tenant_id: int, queue_date: Optional[str] = None,
              user: User = Depends(get_current_user)):
    """Get full queue for a tenant on a given date (defaults to today)."""
    ensure_tenant_access(user, tenant_id)
    target_date = queue_date or core.today_str()
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


# Terminal statuses are left off the timeline on purpose. A cancelled booking
# gives its time back — drawing it would show the day as fuller than it is,
# which is the one thing a calendar must never do.
TIMELINE_STATUSES = ["Waiting", "InService", "Done"]


def _timeline_row(entry: QueueEntry, services: Dict[int, Service]) -> Dict[str, Any]:
    end = _entry_end(entry, services)
    service = services.get(entry.service_id)
    return {
        "id":              entry.id,
        "customer_name":   entry.customer_name,
        "customer_number": entry.customer_number.replace("@s.whatsapp.net", ""),
        "service":         service.name if service else "—",
        "status":          entry.status,
        "booked_via":      entry.booked_via,
        # The distinction the whole view exists to show: a promised time versus
        # a forecast that the next walk-in may move.
        "is_fixed":        entry.is_fixed,
        "start":           entry.estimated_start.isoformat() if entry.estimated_start else None,
        "end":             end.isoformat() if end else None,
        "minutes":         int((end - entry.estimated_start).total_seconds() // 60)
                           if end and entry.estimated_start else 0,
    }


@router.get("/admin/timeline/{tenant_id}")
def get_day_timeline(tenant_id: int, queue_date: Optional[str] = None,
                     user: User = Depends(get_current_user)):
    """
    One day laid out per agent: the hours each one works, and the work already
    placed inside them.

    The queue list answers "who is next?". This answers "where are the gaps?" —
    the question a shop taking booked appointments actually asks, and one a
    table cannot answer, because a free hour is the absence of a row and
    absence is exactly what a list cannot draw.

    Batched like get_queue: windows for every agent in three queries, one
    service lookup, no per-agent round trip.
    """
    ensure_tenant_access(user, tenant_id)
    target = queue_date or core.today_str()
    day    = datetime.strptime(target, "%Y-%m-%d")

    with Session(engine) as s:
        tenant = s.get(Tenant, tenant_id)
        if not tenant:
            raise HTTPException(status_code=404, detail="Tenant not found")
        agents  = s.exec(select(Agent).where(Agent.tenant_id == tenant_id)).all()
        entries = s.exec(
            select(QueueEntry).where(
                QueueEntry.tenant_id  == tenant_id,
                QueueEntry.queue_date == target,
                QueueEntry.status.in_(TIMELINE_STATUSES),
            )
        ).all()
        services = _service_map(s, entries)

    by_agent: Dict[Any, List[QueueEntry]] = {}
    for e in entries:
        by_agent.setdefault(e.agent_id, []).append(e)

    # An agent switched off at lunchtime still has this morning's customers on
    # the board. Dropping them from the view because the agent is now inactive
    # would hide work that is still happening.
    visible  = [a for a in agents if a.is_active or a.id in by_agent]
    windows  = get_working_windows_for([a.id for a in visible], tenant, target)

    # One shared axis across every column, or two agents' 10:00 would not line
    # up. Widened to cover anything scheduled outside the working hours — an
    # overrun is precisely what someone opens this view to find.
    edges: List[datetime] = []
    for spans in windows.values():
        for w_start, w_end in spans:
            edges += [w_start, w_end]
    for e in entries:
        end = _entry_end(e, services)
        if e.estimated_start and end:
            edges += [e.estimated_start, end]

    if edges:
        day_start = min(edges).replace(minute=0, second=0, microsecond=0)
        latest    = max(edges)
        day_end   = latest.replace(minute=0, second=0, microsecond=0)
        if day_end < latest:
            day_end += timedelta(hours=1)
    else:
        day_start = day + timedelta(hours=tenant.queue_opens)
        day_end   = day + timedelta(hours=tenant.queue_closes)
    # A closed day, or a tenant configured opens == closes, would otherwise
    # hand the grid a zero-height axis to divide by.
    if day_end <= day_start:
        day_end = day_start + timedelta(hours=1)

    columns = []
    for agent in visible:
        spans   = windows.get(agent.id, [])
        mine    = sorted(by_agent.get(agent.id, []),
                         key=lambda e: (e.estimated_start or datetime.max))
        placed  = [e for e in mine if e.estimated_start and _entry_end(e, services)]
        drawn   = {e.id for e in placed}
        working = sum(int((w_end - w_start).total_seconds() // 60) for w_start, w_end in spans)
        booked  = sum(int((_entry_end(e, services) - e.estimated_start).total_seconds() // 60)
                      for e in placed)
        columns.append({
            "agent_id":   agent.id,
            "name":       agent.name,
            "is_active":  agent.is_active,
            "working":    bool(spans),
            "windows": [
                {"start": w_start.isoformat(), "end": w_end.isoformat(),
                 "minutes": int((w_end - w_start).total_seconds() // 60)}
                for w_start, w_end in spans
            ],
            "entries":    [_timeline_row(e, services) for e in placed],
            # Nothing should land here, but an entry with no start would
            # otherwise vanish from a view staff are trusting to be complete.
            "unplaced":   [_timeline_row(e, services) for e in mine if e.id not in drawn],
            "booked_minutes":  booked,
            "working_minutes": working,
            "free_minutes":    max(0, working - booked),
        })

    known = {a.id for a in visible}
    return {
        "tenant_id":  tenant_id,
        "queue_date": target,
        "day_start":  day_start.isoformat(),
        "day_end":    day_end.isoformat(),
        "agents":     columns,
        # Entries pointing at an agent row that no longer exists. Belongs in
        # the response rather than swallowed, for the same reason as unplaced.
        "orphaned":   [_timeline_row(e, services)
                       for e in entries if e.agent_id not in known],
    }


@router.patch("/admin/queue/{entry_id}/status")
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
            entry.started_at = core.now()
        if new_status in ("Done", "NoShow", "Cancelled"):
            entry.finished_at = core.now()
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


@router.post("/admin/queue/walkin")
def add_walkin(body: Dict[str, Any], user: User = Depends(get_current_user)):
    """Add a walk-in customer from the admin dashboard."""
    tenant_id        = body.get("tenant_id")
    ensure_tenant_access(user, tenant_id)
    service_id       = body.get("service_id")
    agent_id         = body.get("agent_id")
    name             = body.get("customer_name", "Walk-in")
    phone            = body.get("customer_phone", "")
    additional_names = body.get("additional_names", "")
    queue_date       = body.get("queue_date", core.today_str())

    # Same lock the WhatsApp path takes, so a walk-in landing mid-confirmation
    # can't be handed the agent that booking is about to fill.
    with booking_lock(tenant_id, queue_date):
        with Session(engine) as s:
            tenant = s.get(Tenant, tenant_id)
            if not tenant:
                raise HTTPException(status_code=404, detail="Tenant not found")

            # Block walk-ins after closing hours (today only)
            if queue_date == core.today_str() and not queue_is_open_today(tenant):
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

