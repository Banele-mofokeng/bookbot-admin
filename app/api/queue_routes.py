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
from app.queue_engine import (_service_map, assign_agent,
                              calculate_estimated_start, cancel_party,
                              find_walkin_insert_joined_at,
                              get_agent_backlog_minutes, recalculate_queue)
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

