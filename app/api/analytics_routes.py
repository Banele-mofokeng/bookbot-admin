"""Completed-work reporting. Read-only over closed queue entries."""
import math
from datetime import datetime, timedelta, time, date
from typing import Optional, Dict, Any, List
from fastapi import APIRouter, HTTPException, Depends
from sqlmodel import SQLModel, Session, select

from app import config, core
from app.core import WEEKDAY_NAMES
from app.auth import get_current_user, require_super, ensure_tenant_access
from app.db import engine
from app.models import (Tenant, User, Service, Agent, AgentService,
                        AgentSchedule, AgentBlock, QueueEntry, OutboxMessage)

# Same guard the single admin router used to carry: every route below
# authenticates, then scopes to the caller's tenant.
router = APIRouter(dependencies=[Depends(get_current_user)])

ANALYTICS_MAX_DAYS = 366


def _median(values: List[float]) -> Optional[int]:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return int(ordered[mid])
    return int((ordered[mid - 1] + ordered[mid]) / 2)


def _percentile(values: List[float], pct: float) -> Optional[int]:
    """
    Nearest-rank percentile: the smallest value at or below which `pct` of the
    sample falls. Samples here are small, so no interpolation.

    p90 of nine 5s and one 120 is 5, not 120 — the point is "90% of customers
    waited no longer than this", which the slowest single outlier does not set.
    """
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, min(len(ordered), math.ceil(pct / 100 * len(ordered))))
    return int(ordered[rank - 1])


def _duration_stats(values: List[float]) -> Dict[str, Any]:
    """
    Report a duration only when there is something to report. An empty result
    says so rather than showing a zero that reads like a real measurement.
    """
    return {
        "available": bool(values),
        "samples":   len(values),
        "median_minutes": _median(values),
        "p90_minutes":    _percentile(values, 90),
    }


@router.get("/admin/analytics/{tenant_id}")
def get_analytics(tenant_id: int, from_date: Optional[str] = None,
                  to_date: Optional[str] = None,
                  user: User = Depends(get_current_user)):
    """
    Aggregates for one business over a date range (default: the last 30 days,
    inclusive of today).

    The care taken here is mostly about *not* reporting things:

    * A no-show closed by the midnight sweep is not a no-show. It means nobody
      tapped Done. Those are counted separately as `unclosed`, and the no-show
      rate is withheld entirely (`null`) for any period containing rows from
      before provenance was tracked, rather than quietly reading low.
    * Rates are computed over closed entries only. Including a queue that is
      still running would drag every rate down as the day progresses.
    * Wait and service times come from `started_at` / `finished_at`, which only
      exist from the upgrade onwards. When there are no samples the block says
      `available: false` instead of showing zero.
    * `minutes_booked` is scheduled duration, not measured — labelled as such.
    """
    ensure_tenant_access(user, tenant_id)

    end   = to_date or core.today_str()
    start = from_date or (
        datetime.strptime(end, "%Y-%m-%d") - timedelta(days=29)
    ).date().isoformat()
    try:
        span = (datetime.strptime(end, "%Y-%m-%d")
                - datetime.strptime(start, "%Y-%m-%d")).days + 1
    except ValueError:
        raise HTTPException(status_code=400, detail="Dates must be YYYY-MM-DD")
    if span < 1:
        raise HTTPException(status_code=400, detail="to_date must not precede from_date")
    if span > ANALYTICS_MAX_DAYS:
        raise HTTPException(
            status_code=400,
            detail=f"Range is limited to {ANALYTICS_MAX_DAYS} days")

    with Session(engine) as s:
        entries = s.exec(
            select(QueueEntry).where(
                QueueEntry.tenant_id  == tenant_id,
                QueueEntry.queue_date >= start,
                QueueEntry.queue_date <= end,
            )
        ).all()
        # Batched, like everywhere else in the engine — never one query per row.
        agents = {a.id: a for a in s.exec(
            select(Agent).where(Agent.tenant_id == tenant_id)).all()}
        services = {sv.id: sv for sv in s.exec(
            select(Service).where(Service.tenant_id == tenant_id)).all()}

    done = no_show_staff = no_show_system = no_show_unknown = 0
    cancelled_customer = cancelled_other = still_open = 0
    by_day: Dict[str, Dict[str, int]] = {}
    by_weekday = [0] * 7
    by_hour    = [0] * 24
    channel    = {"whatsapp": 0, "walkin": 0}
    per_agent: Dict[int, Dict[str, int]] = {}
    per_service: Dict[int, Dict[str, int]] = {}
    wait_minutes: List[float]    = []
    service_minutes: List[float] = []

    for e in entries:
        day = by_day.setdefault(e.queue_date, {"bookings": 0, "done": 0})
        day["bookings"] += 1
        try:
            by_weekday[datetime.strptime(e.queue_date, "%Y-%m-%d").weekday()] += 1
        except ValueError:
            pass
        if e.joined_at:
            by_hour[e.joined_at.hour] += 1
        channel[e.booked_via if e.booked_via in channel else "walkin"] += 1

        a = per_agent.setdefault(e.agent_id, {"bookings": 0, "done": 0,
                                              "no_shows": 0, "unclosed": 0})
        a["bookings"] += 1
        sv = per_service.setdefault(e.service_id, {"bookings": 0, "minutes_booked": 0})
        sv["bookings"] += 1
        svc = services.get(e.service_id)
        sv["minutes_booked"] += svc.duration_minutes if svc else 0

        if e.status == "Done":
            done += 1
            day["done"] += 1
            a["done"] += 1
        elif e.status == "NoShow":
            if e.closed_by == "system":
                no_show_system += 1
                a["unclosed"] += 1
            elif e.closed_by:
                no_show_staff += 1
                a["no_shows"] += 1
            else:
                no_show_unknown += 1
        elif e.status == "Cancelled":
            if e.closed_by == "customer":
                cancelled_customer += 1
            else:
                cancelled_other += 1
        else:
            still_open += 1

        if e.started_at and e.joined_at:
            wait = (e.started_at - e.joined_at).total_seconds() / 60
            if wait >= 0:
                wait_minutes.append(wait)
        if e.status == "Done" and e.started_at and e.finished_at:
            served = (e.finished_at - e.started_at).total_seconds() / 60
            if served >= 0:
                service_minutes.append(served)

    cancelled = cancelled_customer + cancelled_other
    no_shows  = no_show_staff + no_show_unknown
    closed    = done + no_shows + no_show_system + cancelled

    def rate(count):
        return round(count / closed, 4) if closed else None

    # Withheld rather than understated: with untagged rows in the range there
    # is no way to tell a real no-show from an entry staff forgot to close.
    no_show_rate = rate(no_show_staff) if no_show_unknown == 0 else None

    return {
        "tenant_id": tenant_id,
        "from_date": start,
        "to_date":   end,
        "days":      span,
        "totals": {
            "bookings":           len(entries),
            "done":               done,
            "no_shows":           no_show_staff,
            "no_shows_untracked": no_show_unknown,
            "unclosed":           no_show_system,
            "cancelled":          cancelled,
            "cancelled_by_customer": cancelled_customer,
            "still_open":         still_open,
            "closed":             closed,
        },
        "rates": {
            "completion":   rate(done),
            "no_show":      no_show_rate,
            "no_show_note": None if no_show_unknown == 0 else (
                f"{no_show_unknown} no-shows in this range predate close tracking, "
                f"so a real no-show can't be told apart from an entry nobody "
                f"closed. Rate withheld."),
            "cancellation": rate(cancelled),
            "unclosed":     rate(no_show_system),
        },
        "channel": channel,
        "by_day": [
            {"date": d, "weekday": WEEKDAY_NAMES[
                datetime.strptime(d, "%Y-%m-%d").weekday()][:3],
             **by_day[d]}
            for d in sorted(by_day)
        ],
        "by_weekday": [
            {"weekday": i, "name": WEEKDAY_NAMES[i], "bookings": n}
            for i, n in enumerate(by_weekday)
        ],
        "by_hour": [{"hour": h, "bookings": n} for h, n in enumerate(by_hour)],
        "by_agent": sorted((
            {"agent_id": aid,
             "name": agents[aid].name if aid in agents else "(removed)",
             **vals}
            for aid, vals in per_agent.items()
        ), key=lambda r: -r["bookings"]),
        "by_service": sorted((
            {"service_id": sid,
             "name": services[sid].name if sid in services else "(removed)",
             **vals}
            for sid, vals in per_service.items()
        ), key=lambda r: -r["bookings"]),
        # Measured, not estimated — and only from the upgrade onwards.
        "wait_time":    _duration_stats(wait_minutes),
        "service_time": _duration_stats(service_minutes),
        "data_quality": {
            "unclosed": no_show_system,
            "untracked_closes": no_show_unknown,
            "note": ("Entries the midnight sweep closed because staff never "
                     "marked them Done. High numbers mean the dashboard isn't "
                     "being kept up to date, not that customers didn't arrive."),
        },
    }

