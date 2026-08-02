"""Service catalogue CRUD."""
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

@router.get("/admin/services/{tenant_id}")
def list_services(tenant_id: int, user: User = Depends(get_current_user)):
    ensure_tenant_access(user, tenant_id)
    with Session(engine) as s:
        return s.exec(
            select(Service).where(Service.tenant_id == tenant_id)
        ).all()


class ServiceCreate(SQLModel):
    tenant_id:         int
    name:              str
    duration_minutes:  int  = 60
    is_active:         bool = True


@router.post("/admin/services")
def create_service(data: ServiceCreate, user: User = Depends(get_current_user)):
    ensure_tenant_access(user, data.tenant_id)
    service = Service(**data.dict())
    with Session(engine) as s:
        s.add(service)
        s.commit()
        s.refresh(service)
    return service


@router.patch("/admin/services/{service_id}")
def update_service(service_id: int, updates: Dict[str, Any],
                   user: User = Depends(get_current_user)):
    with Session(engine) as s:
        svc = s.get(Service, service_id)
        if not svc:
            raise HTTPException(status_code=404, detail="Service not found")
        ensure_tenant_access(user, svc.tenant_id)
        for k, v in updates.items():
            if hasattr(svc, k):
                setattr(svc, k, v)
        s.add(svc)
        s.commit()
        s.refresh(svc)
    return svc

