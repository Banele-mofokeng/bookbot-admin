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


@router.get("/admin/tenants")
def list_tenants(user: User = Depends(get_current_user)):
    with Session(engine) as s:
        if user.is_super:
            return s.exec(select(Tenant)).all()
        # Tenant users only ever see their own business
        t = s.get(Tenant, user.tenant_id) if user.tenant_id else None
        return [t] if t else []

@router.post("/admin/tenants")
def create_tenant(data: TenantCreate, _: User = Depends(require_super)):
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
        for k, v in updates.items():
            if hasattr(tenant, k):
                setattr(tenant, k, v)
        s.add(tenant)
        s.commit()
        s.refresh(tenant)
    return tenant

