"""Agents, their recurring schedules, and one-off unavailability blocks."""
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

from app.core import WEEKDAY_NAMES
from app.queue_engine import get_working_windows, recalculate_queue

@router.get("/admin/agents/{tenant_id}")
def list_agents(tenant_id: int, user: User = Depends(get_current_user)):
    ensure_tenant_access(user, tenant_id)
    with Session(engine) as s:
        agents = s.exec(
            select(Agent).where(Agent.tenant_id == tenant_id)
        ).all()
        result = []
        for agent in agents:
            service_ids = [
                row.service_id for row in s.exec(
                    select(AgentService).where(AgentService.agent_id == agent.id)
                ).all()
            ]
            result.append({**agent.dict(), "service_ids": service_ids})
        return result


@router.post("/admin/agents")
def create_agent(body: Dict[str, Any], user: User = Depends(get_current_user)):
    """Create an agent and assign their services in one call."""
    ensure_tenant_access(user, body.get("tenant_id"))
    service_ids = body.pop("service_ids", [])
    body.pop("id", None)  # never accept id from client
    with Session(engine) as s:
        agent = Agent(**{k: v for k, v in body.items() if hasattr(Agent, k) and k != "id"})
        s.add(agent)
        s.commit()
        s.refresh(agent)
        for sid in service_ids:
            s.add(AgentService(agent_id=agent.id, service_id=sid))
        s.commit()
    return {**agent.dict(), "service_ids": service_ids}


@router.patch("/admin/agents/{agent_id}")
def update_agent(agent_id: int, updates: Dict[str, Any],
                 user: User = Depends(get_current_user)):
    service_ids = updates.pop("service_ids", None)
    with Session(engine) as s:
        agent = s.get(Agent, agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")
        ensure_tenant_access(user, agent.tenant_id)
        for k, v in updates.items():
            if hasattr(agent, k):
                setattr(agent, k, v)
        s.add(agent)
        if service_ids is not None:
            # Replace all service assignments
            existing = s.exec(
                select(AgentService).where(AgentService.agent_id == agent_id)
            ).all()
            for e in existing:
                s.delete(e)
            for sid in service_ids:
                s.add(AgentService(agent_id=agent_id, service_id=sid))
        s.commit()
        s.refresh(agent)
        new_service_ids = [
            row.service_id for row in s.exec(
                select(AgentService).where(AgentService.agent_id == agent_id)
            ).all()
        ]
    return {**agent.dict(), "service_ids": new_service_ids}


# =============================================================================
# 13b. ADMIN — AGENT SCHEDULES & BLOCKS
# =============================================================================



def _load_agent(s: Session, agent_id: int, user: User) -> Agent:
    agent = s.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    ensure_tenant_access(user, agent.tenant_id)
    return agent


def _hhmm(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


def _parse_dt(value: Any, field: str) -> datetime:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", ""))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400,
                            detail=f"{field} must be an ISO datetime")


@router.get("/admin/agents/{agent_id}/schedule")
def get_agent_schedule(agent_id: int, user: User = Depends(get_current_user)):
    """
    The agent's recurring weekly windows. An empty list means no schedule is
    set, and the agent falls back to the tenant's opening hours every day.
    """
    with Session(engine) as s:
        _load_agent(s, agent_id, user)
        rows = s.exec(
            select(AgentSchedule)
            .where(AgentSchedule.agent_id == agent_id)
            .order_by(AgentSchedule.weekday, AgentSchedule.start_minute)
        ).all()
        return {
            "agent_id": agent_id,
            "uses_tenant_hours": not rows,
            "windows": [
                {"id": r.id, "weekday": r.weekday,
                 "weekday_name": WEEKDAY_NAMES[r.weekday],
                 "start_minute": r.start_minute, "end_minute": r.end_minute,
                 "start": _hhmm(r.start_minute), "end": _hhmm(r.end_minute)}
                for r in rows
            ],
        }


@router.put("/admin/agents/{agent_id}/schedule")
def set_agent_schedule(agent_id: int, body: Dict[str, Any],
                       user: User = Depends(get_current_user)):
    """
    Replace the agent's whole weekly schedule.

    Body: {"windows": [{"weekday": 0, "start_minute": 480, "end_minute": 1020}, …]}

    Replace-all rather than per-row edits, because a schedule is read as a
    whole and a half-applied one would silently take a working day off the
    board. Sending an empty list clears the schedule and returns the agent to
    the tenant's opening hours.

    Two windows on the same weekday make a split shift; the gap between them is
    simply not worked. Overlapping windows are accepted and coalesce on read.
    """
    windows = body.get("windows")
    if not isinstance(windows, list):
        raise HTTPException(status_code=400, detail="windows must be a list")

    cleaned = []
    for w in windows:
        if not isinstance(w, dict):
            raise HTTPException(status_code=400, detail="each window must be an object")
        try:
            weekday = int(w["weekday"])
            start   = int(w["start_minute"])
            end     = int(w["end_minute"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(
                status_code=400,
                detail="each window needs integer weekday, start_minute, end_minute")
        if not 0 <= weekday <= 6:
            raise HTTPException(status_code=400,
                                detail="weekday must be 0 (Monday) to 6 (Sunday)")
        if not 0 <= start < end <= 24 * 60:
            raise HTTPException(
                status_code=400,
                detail="need 0 <= start_minute < end_minute <= 1440")
        cleaned.append((weekday, start, end))

    with Session(engine) as s:
        agent = _load_agent(s, agent_id, user)
        tenant_id = agent.tenant_id
        for row in s.exec(
            select(AgentSchedule).where(AgentSchedule.agent_id == agent_id)
        ).all():
            s.delete(row)
        for weekday, start, end in cleaned:
            s.add(AgentSchedule(tenant_id=tenant_id, agent_id=agent_id,
                                weekday=weekday, start_minute=start,
                                end_minute=end))
        s.commit()

    # Today's ETAs were computed against the old hours — restate them now
    # rather than leaving customers holding times nobody will honour.
    recalculate_queue(tenant_id, agent_id, core.today_str())
    return get_agent_schedule(agent_id, user)


@router.get("/admin/agents/{agent_id}/blocks")
def list_agent_blocks(agent_id: int, from_date: Optional[str] = None,
                      to_date: Optional[str] = None,
                      user: User = Depends(get_current_user)):
    """One-off unavailable windows. Defaults to today onward."""
    start = _parse_dt(f"{from_date}T00:00:00", "from_date") if from_date \
        else datetime.strptime(core.today_str(), "%Y-%m-%d")
    with Session(engine) as s:
        _load_agent(s, agent_id, user)
        conditions = [AgentBlock.agent_id == agent_id, AgentBlock.ends_at > start]
        if to_date:
            conditions.append(
                AgentBlock.starts_at < _parse_dt(f"{to_date}T00:00:00", "to_date")
                + timedelta(days=1))
        rows = s.exec(
            select(AgentBlock).where(*conditions).order_by(AgentBlock.starts_at)
        ).all()
        return [
            {"id": r.id, "agent_id": r.agent_id, "reason": r.reason,
             "starts_at": r.starts_at.isoformat(), "ends_at": r.ends_at.isoformat()}
            for r in rows
        ]


@router.post("/admin/agents/{agent_id}/blocks")
def create_agent_block(agent_id: int, body: Dict[str, Any],
                       user: User = Depends(get_current_user)):
    """Body: {"starts_at": ISO, "ends_at": ISO, "reason": "Lunch"}"""
    starts_at = _parse_dt(body.get("starts_at"), "starts_at")
    ends_at   = _parse_dt(body.get("ends_at"), "ends_at")
    if ends_at <= starts_at:
        raise HTTPException(status_code=400, detail="ends_at must be after starts_at")

    with Session(engine) as s:
        agent = _load_agent(s, agent_id, user)
        block = AgentBlock(tenant_id=agent.tenant_id, agent_id=agent_id,
                           starts_at=starts_at, ends_at=ends_at,
                           reason=str(body.get("reason", ""))[:200])
        s.add(block)
        s.commit()
        s.refresh(block)
        result = {"id": block.id, "agent_id": agent_id, "reason": block.reason,
                  "starts_at": block.starts_at.isoformat(),
                  "ends_at": block.ends_at.isoformat()}
        tenant_id = agent.tenant_id

    # Anyone already booked into the blocked window needs a new time.
    recalculate_queue(tenant_id, agent_id, starts_at.date().isoformat())
    return result


@router.delete("/admin/blocks/{block_id}")
def delete_agent_block(block_id: int, user: User = Depends(get_current_user)):
    with Session(engine) as s:
        block = s.get(AgentBlock, block_id)
        if not block:
            raise HTTPException(status_code=404, detail="Block not found")
        ensure_tenant_access(user, block.tenant_id)
        tenant_id, agent_id = block.tenant_id, block.agent_id
        queue_date = block.starts_at.date().isoformat()
        s.delete(block)
        s.commit()

    recalculate_queue(tenant_id, agent_id, queue_date)
    return {"status": "deleted", "block_id": block_id}


@router.get("/admin/agents/{agent_id}/windows")
def get_agent_windows(agent_id: int, queue_date: Optional[str] = None,
                      user: User = Depends(get_current_user)):
    """
    The windows actually used for scheduling on a date — schedule resolved for
    that weekday, blocks already subtracted. This is what the queue engine
    sees, so it's the honest answer to "why was nobody booked at 14:00?".
    """
    target = queue_date or core.today_str()
    with Session(engine) as s:
        agent  = _load_agent(s, agent_id, user)
        tenant = s.get(Tenant, agent.tenant_id)

    windows = get_working_windows(tenant, agent_id, target)
    return {
        "agent_id": agent_id,
        "queue_date": target,
        "working": bool(windows),
        "windows": [
            {"start": w[0].isoformat(), "end": w[1].isoformat(),
             "minutes": int((w[1] - w[0]).total_seconds() // 60)}
            for w in windows
        ],
    }

