"""Liveness probe and the destructive schema reset."""
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

# Health is unauthenticated — a probe has no token.
public_router = APIRouter()

@public_router.get("/health")
def health():
    try:
        config.redis_client.ping()
        redis_ok = True
    except Exception as e:
        redis_ok = str(e)
    with Session(engine) as s:
        tenants = len(s.exec(select(Tenant)).all())
        # A climbing pending count or any failures means messages aren't
        # reaching customers — worth alerting on.
        outbox_pending = len(s.exec(
            select(OutboxMessage).where(OutboxMessage.status == "Pending")
        ).all())
        outbox_failed = len(s.exec(
            select(OutboxMessage).where(OutboxMessage.status == "Failed")
        ).all())
    return {"status": "ok", "redis": redis_ok, "tenants": tenants,
            "webhook_auth": bool(config.WEBHOOK_SECRETS),
            "outbox_pending": outbox_pending, "outbox_failed": outbox_failed}


@router.post("/admin/migrate-reset")
def migrate_reset(_: User = Depends(require_super)):
    """Drop and recreate all tables using CASCADE. Destructive — super-admin only."""
    from sqlalchemy import text
    with engine.connect() as conn:
        conn.execute(text(
            "DROP TABLE IF EXISTS agentservice, queueentry, agent, service, tenant CASCADE"
        ))
        conn.commit()
    SQLModel.metadata.create_all(engine)
    return {"status": "done"}

