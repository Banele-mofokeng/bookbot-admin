"""Tenant CRUD. Creation is super-admin only."""
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
    # Which bot this business runs. Defaults to the queue so an existing
    # onboarding form that doesn't send this keeps creating queue tenants.
    mode:                   str = "queue"
    currency_symbol:        str = "R"
    kitchen_parallel_items: int = 4
    slot_granularity_minutes: int = 30
    reminder_offsets_minutes: str = "1440,120"


@router.get("/admin/tenants")
def list_tenants(user: User = Depends(get_current_user)):
    with Session(engine) as s:
        if user.is_super:
            return s.exec(select(Tenant)).all()
        # Tenant users only ever see their own business
        t = s.get(Tenant, user.tenant_id) if user.tenant_id else None
        return [t] if t else []

MODES = ["queue", "orders", "appointments"]

# A grid coarser than this is not a booking system, and one finer is unreadable
# on WhatsApp. Guarded because a zero would make the slot engine offer nothing
# at all, silently, for as long as it took someone to notice.
MIN_GRANULARITY, MAX_GRANULARITY = 5, 240


# Five reminders about one haircut is not a service, it is a nuisance. The
# ceiling is a fortnight because a rung further out than the booking window can
# never come due.
MAX_REMINDERS       = 4
MAX_REMINDER_OFFSET = 14 * 24 * 60


def _validate_granularity(value):
    if not isinstance(value, int) or not (MIN_GRANULARITY <= value <= MAX_GRANULARITY):
        raise HTTPException(
            status_code=400,
            detail=f"slot_granularity_minutes must be between "
                   f"{MIN_GRANULARITY} and {MAX_GRANULARITY}")


def _validate_reminders(value):
    """
    Rejected here rather than tolerated, even though the sweep itself drops
    nonsense rather than raising. A typo that silently switched a clinic's
    reminders off would not be noticed until somebody failed to arrive.
    """
    if not isinstance(value, str):
        raise HTTPException(status_code=400,
                            detail="reminder_offsets_minutes must be a string")
    raw = [p.strip() for p in value.split(",") if p.strip()]
    for part in raw:
        if not part.lstrip("-").isdigit() or int(part) <= 0:
            raise HTTPException(
                status_code=400,
                detail=f"reminder_offsets_minutes: {part!r} is not a "
                       f"positive number of minutes")
        if int(part) > MAX_REMINDER_OFFSET:
            raise HTTPException(
                status_code=400,
                detail=f"reminder_offsets_minutes: {part} is more than "
                       f"{MAX_REMINDER_OFFSET} minutes ahead")
    if len(core.parse_reminder_offsets(value)) > MAX_REMINDERS:
        raise HTTPException(
            status_code=400,
            detail=f"at most {MAX_REMINDERS} reminders per appointment")


@router.post("/admin/tenants")
def create_tenant(data: TenantCreate, _: User = Depends(require_super)):
    if data.mode not in MODES:
        raise HTTPException(status_code=400,
                            detail=f"mode must be one of {', '.join(MODES)}")
    _validate_granularity(data.slot_granularity_minutes)
    _validate_reminders(data.reminder_offsets_minutes)
    tenant = Tenant(**data.dict())
    with Session(engine) as s:
        s.add(tenant)
        s.commit()
        s.refresh(tenant)
    return tenant


@router.patch("/admin/tenants/{tenant_id}")
def update_tenant(tenant_id: int, updates: Dict[str, Any],
                  user: User = Depends(get_current_user)):
    ensure_tenant_access(user, tenant_id)
    with Session(engine) as s:
        tenant = s.get(Tenant, tenant_id)
        if not tenant:
            raise HTTPException(status_code=404, detail="Tenant not found")
        if "mode" in updates and updates["mode"] not in MODES:
            raise HTTPException(status_code=400,
                                detail=f"mode must be one of {', '.join(MODES)}")
        if "slot_granularity_minutes" in updates:
            _validate_granularity(updates["slot_granularity_minutes"])
        if "reminder_offsets_minutes" in updates:
            _validate_reminders(updates["reminder_offsets_minutes"])
        for k, v in updates.items():
            if hasattr(tenant, k):
                setattr(tenant, k, v)
        s.add(tenant)
        s.commit()
        s.refresh(tenant)
    return tenant

