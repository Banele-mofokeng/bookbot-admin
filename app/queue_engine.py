"""Working windows, backlogs, ETAs, agent assignment, requeueing.
The scheduling brain — no I/O beyond the database.
"""
import math
import re
from datetime import datetime, timedelta, time, date
from typing import Optional, Dict, Any, List
from sqlmodel import Session, select

from app import core
from app.db import engine
from app.models import (Tenant, Service, Agent, AgentService, AgentSchedule,
                        AgentBlock, QueueEntry)

# =============================================================================
# 6. QUEUE ENGINE
# =============================================================================

# ── Working windows ──────────────────────────────────────────────────────────
# A "window" is a (start, end) pair of naive datetimes on one calendar day
# during which one agent is actually available. Everything downstream — ETAs,
# backlog placement, gap-fill — schedules inside windows instead of assuming
# the agent is free from opening to closing.

Window = "tuple"   # (datetime, datetime); aliased for readability only


def _merge_windows(windows: List) -> List:
    """Sort and coalesce touching or overlapping windows."""
    out: List = []
    for start, end in sorted(windows):
        if end <= start:
            continue
        if out and start <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


def _subtract_windows(windows: List, blocks: List) -> List:
    """
    Remove every blocked interval from every window. A block in the middle of a
    shift splits it in two, which is what makes a lunch break work.
    """
    out = list(windows)
    for b_start, b_end in blocks:
        if b_end <= b_start:
            continue
        nxt: List = []
        for w_start, w_end in out:
            if b_end <= w_start or b_start >= w_end:
                nxt.append((w_start, w_end))       # no overlap
                continue
            if b_start > w_start:
                nxt.append((w_start, b_start))     # keep the head
            if b_end < w_end:
                nxt.append((b_end, w_end))         # keep the tail
        out = nxt
    return [(s, e) for s, e in sorted(out) if e > s]


def get_working_windows_for(agent_ids: List[int], tenant: Tenant,
                            queue_date: str) -> Dict[int, List]:
    """
    Working windows for several agents on one date — three queries total, so
    assign_agent can rank every candidate without a per-agent round trip.

    Fallback rules, in order:
      * agent has schedule rows for this weekday  → those windows
      * agent has no schedule rows at all         → the tenant's opening hours
      * agent has rows, but none for this weekday → [] (off today)

    The middle case is what keeps existing deployments unchanged: nobody has a
    schedule yet, so nobody's hours move until someone sets one.
    """
    if not agent_ids:
        return {}

    day = datetime.strptime(queue_date, "%Y-%m-%d")
    weekday = day.weekday()
    day_end = day + timedelta(days=1)

    with Session(engine) as s:
        rows = s.exec(
            select(AgentSchedule).where(AgentSchedule.agent_id.in_(agent_ids))
        ).all()
        blocks = s.exec(
            select(AgentBlock).where(
                AgentBlock.agent_id.in_(agent_ids),
                AgentBlock.starts_at < day_end,
                AgentBlock.ends_at   > day,
            )
        ).all()

    has_schedule   = {r.agent_id for r in rows}
    todays_windows: Dict[int, List] = {}
    for r in rows:
        if r.weekday != weekday:
            continue
        todays_windows.setdefault(r.agent_id, []).append((
            day + timedelta(minutes=r.start_minute),
            day + timedelta(minutes=r.end_minute),
        ))

    blocks_by_agent: Dict[int, List] = {}
    for b in blocks:
        # Clip to the day — a block running overnight only removes today's part.
        blocks_by_agent.setdefault(b.agent_id, []).append(
            (max(b.starts_at, day), min(b.ends_at, day_end))
        )

    tenant_hours = [(
        day + timedelta(hours=tenant.queue_opens),
        day + timedelta(hours=tenant.queue_closes),
    )]

    result: Dict[int, List] = {}
    for aid in agent_ids:
        if aid in has_schedule:
            base = todays_windows.get(aid, [])
        else:
            base = tenant_hours
        result[aid] = _subtract_windows(_merge_windows(base),
                                        blocks_by_agent.get(aid, []))
    return result


def get_working_windows(tenant: Tenant, agent_id: int, queue_date: str) -> List:
    """Single-agent convenience wrapper. See get_working_windows_for."""
    return get_working_windows_for([agent_id], tenant, queue_date).get(agent_id, [])


def place_in_windows(windows: List, floor: datetime,
                     duration_minutes: int) -> Optional[datetime]:
    """
    Earliest start at or after `floor` where `duration_minutes` fits inside one
    contiguous window. Returns None when the day has no room left.

    A service must fit in a single window: a 60-minute cut cannot start at
    12:30 if lunch begins at 13:00, because nobody actually works through it.

    `start < w_end` only bites for a zero duration, where it stops a bare
    "when is this agent next free?" snap from answering 13:00 on the dot — the
    exact minute the agent leaves for lunch.
    """
    need = timedelta(minutes=duration_minutes)
    for w_start, w_end in windows:
        start = max(w_start, floor)
        if start < w_end and start + need <= w_end:
            return start
    return None


def _service_map(s: Session, entries) -> Dict[int, Service]:
    """
    Every service referenced by these entries, in one query.

    The queue engine walks entries and needs each one's duration. Doing that
    with s.get(Service, ...) per entry costs a round trip per row, on the
    hottest paths in the app (backlog, ETA recalc, gap-fill, dashboard read).
    A day's queue only ever touches a handful of distinct services.
    """
    ids = {e.service_id for e in entries if e.service_id is not None}
    if not ids:
        return {}
    return {
        svc.id: svc
        for svc in s.exec(select(Service).where(Service.id.in_(ids))).all()
    }


def _backlog_from_entries(entries, services: Dict[int, Service],
                          current_time: datetime,
                          exclude_entry_id: Optional[int] = None) -> int:
    """
    Backlog in minutes for one already-fetched list of entries. Pure — no
    queries — so callers that need several agents' backlogs can fetch once
    and call this per agent.
    """
    total = 0
    for e in entries:
        if exclude_entry_id and e.id == exclude_entry_id:
            continue
        svc = services.get(e.service_id)
        if not svc:
            continue
        if e.estimated_start:
            # Use actual scheduled finish time so gaps (e.g. earliest_arrival)
            # are reflected — the agent isn't free until this entry is done.
            finish_time = e.estimated_start + timedelta(minutes=svc.duration_minutes)
            remaining = (finish_time - current_time).total_seconds() / 60
            if e.status == "InService":
                total += max(0, remaining)
            else:
                # Waiting: never less than the full service duration
                total += max(svc.duration_minutes, max(0, remaining))
        else:
            total += svc.duration_minutes
    return int(total)


def get_agent_backlogs_minutes(agent_ids: List[int], tenant_id: int,
                               queue_date: str) -> Dict[int, int]:
    """
    Backlog for several agents at once — two queries total, regardless of how
    many agents or entries are involved.

    assign_agent used to call get_agent_backlog_minutes once per candidate
    agent, each opening its own Session and re-querying services per entry.
    For a salon with 5 stylists and 20 people booked that was over 100 queries
    to answer one "who's free soonest?".
    """
    if not agent_ids:
        return {}
    with Session(engine) as s:
        entries = s.exec(
            select(QueueEntry).where(
                QueueEntry.agent_id.in_(agent_ids),
                QueueEntry.tenant_id  == tenant_id,
                QueueEntry.queue_date == queue_date,
                QueueEntry.status.in_(["Waiting", "InService"])
            ).order_by(QueueEntry.joined_at)
        ).all()
        services = _service_map(s, entries)

        current_time = core.now()
        by_agent: Dict[int, list] = {aid: [] for aid in agent_ids}
        for e in entries:
            by_agent.setdefault(e.agent_id, []).append(e)
        return {
            aid: _backlog_from_entries(by_agent.get(aid, []), services, current_time)
            for aid in agent_ids
        }


def get_agent_backlog_minutes(agent_id: int, tenant_id: int, queue_date: str,
                               exclude_entry_id: Optional[int] = None) -> int:
    """
    Minutes from NOW until the agent finishes all current work.
    - InService: time remaining until their service ends.
    - Waiting with estimated_start: time until their scheduled finish
      (estimated_start + duration), because the agent won't start them
      before that slot — so we can't claim the agent is free any earlier.
    - Waiting without estimated_start: full duration (fallback).
    """
    with Session(engine) as s:
        entries = s.exec(
            select(QueueEntry).where(
                QueueEntry.agent_id   == agent_id,
                QueueEntry.tenant_id  == tenant_id,
                QueueEntry.queue_date == queue_date,
                QueueEntry.status.in_(["Waiting", "InService"])
            ).order_by(QueueEntry.joined_at)
        ).all()
        services = _service_map(s, entries)
        return _backlog_from_entries(entries, services, core.now(), exclude_entry_id)


def calculate_estimated_start(tenant: Tenant, agent_id: int,
                               queue_date: str, backlog_minutes: int,
                               earliest_arrival: Optional[datetime] = None) -> datetime:
    """Convert backlog minutes into an actual datetime.
    Uses max(queue_opens, now) as the base so backlog is always added to
    the correct anchor — preventing stale opens-time calculations mid-day.

    The result is then snapped forward into the agent's next working window, so
    a quote never lands in a lunch break or on a day off. If it lands past the
    agent's last window the raw time is returned unchanged: the day is over-
    subscribed, and an honest after-hours ETA beats pretending otherwise.
    """
    opens = datetime.strptime(
        f"{queue_date} {tenant.queue_opens:02d}:00", "%Y-%m-%d %H:%M"
    )
    base   = max(opens, core.now())
    result = base + timedelta(minutes=backlog_minutes)
    # Respect customer's declared arrival time
    if earliest_arrival:
        result = max(result, earliest_arrival)

    windows = get_working_windows(tenant, agent_id, queue_date)
    snapped = place_in_windows(windows, result, 0)
    return snapped or result


def parse_arrival_time(text: str, queue_date: str) -> Optional[datetime]:
    """Parse 'now', 'HH:MM', or 'HHMM' into a datetime on queue_date."""
    t = text.strip().lower()
    if t == "now":
        return core.now()
    for fmt in ("%H:%M", "%H%M"):
        try:
            parsed = datetime.strptime(t, fmt)
            base   = datetime.strptime(queue_date, "%Y-%m-%d")
            return base.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)
        except ValueError:
            continue
    return None


def assign_agent(tenant: Tenant, service_id: int,
                 preferred_agent_id: Optional[int], queue_date: str) -> Optional[int]:
    with Session(engine) as s:
        # Get tenant agents first, then find which can do this service
        tenant_agent_ids = [
            a.id for a in s.exec(
                select(Agent).where(Agent.tenant_id == tenant.id, Agent.is_active == True)
            ).all()
        ]
        print(f"🔍 assign_agent | tenant={tenant.id} service={service_id} tenant_agents={tenant_agent_ids}")

        if not tenant_agent_ids:
            print(f"⚠️  No active agents for tenant {tenant.id}")
            return None

        capable_agent_ids = [
            row.agent_id for row in s.exec(
                select(AgentService).where(
                    AgentService.service_id == service_id,
                    AgentService.agent_id.in_(tenant_agent_ids)
                )
            ).all()
        ]
        print(f"🔍 assign_agent | capable_agents={capable_agent_ids}")

        if not capable_agent_ids:
            print(f"⚠️  No agents can do service {service_id} for tenant {tenant.id}")
            return None

        active_agents = s.exec(
            select(Agent).where(
                Agent.id.in_(capable_agent_ids),
                Agent.is_active == True
            )
        ).all()

        if not active_agents:
            print(f"⚠️  No active agents in capable list {capable_agent_ids}")
            return None

        candidate_ids = [a.id for a in active_agents]

    # Drop anyone not working on this date — off day, or blocked out for the
    # whole of it. Without this an agent on leave has an empty queue, so zero
    # backlog, so they would win every single assignment.
    windows = get_working_windows_for(candidate_ids, tenant, queue_date)
    candidate_ids = [aid for aid in candidate_ids if windows.get(aid)]
    if not candidate_ids:
        print(f"⚠️  No agents working on {queue_date} for tenant {tenant.id}")
        return None

    # Honor preference if that agent is capable and working.
    if preferred_agent_id and preferred_agent_id in candidate_ids:
        return preferred_agent_id

    # Assign to agent with shortest backlog. One batched read for all
    # candidates rather than a fresh Session and a fresh query per agent.
    # Ties break on the earlier candidate_ids position, matching min()'s old
    # first-wins behaviour over active_agents.
    backlogs = get_agent_backlogs_minutes(candidate_ids, tenant.id, queue_date)
    return min(candidate_ids, key=lambda aid: backlogs.get(aid, 0))


def recalculate_queue(tenant_id: int, agent_id: int, queue_date: str):
    """
    After any status change, recalculate estimated_start for all
    Waiting entries on this agent and update their positions.
    """
    with Session(engine) as s:
        tenant = s.get(Tenant, tenant_id)
        if not tenant:
            return

        # Get all entries for this agent sorted by joined_at
        entries = s.exec(
            select(QueueEntry).where(
                QueueEntry.agent_id   == agent_id,
                QueueEntry.tenant_id  == tenant_id,
                QueueEntry.queue_date == queue_date,
                QueueEntry.status.in_(["Waiting", "InService"])
            ).order_by(QueueEntry.joined_at)
        ).all()

        services = _service_map(s, entries)
        windows = get_working_windows(tenant, agent_id, queue_date)
        current_time = core.now()
        opens = datetime.strptime(
            f"{queue_date} {tenant.queue_opens:02d}:00", "%Y-%m-%d %H:%M"
        )
        # next_free tracks the absolute datetime when the agent becomes free
        next_free = max(opens, current_time)
        for entry in entries:
            svc = services.get(entry.service_id)
            duration = svc.duration_minutes if svc else 60

            if entry.status == "InService":
                # Frozen — advance next_free to when this service actually finishes
                if entry.estimated_start:
                    finish_time = entry.estimated_start + timedelta(minutes=duration)
                    next_free = max(next_free, finish_time)
            else:
                # Start no earlier than next_free, and no earlier than earliest_arrival
                start_time = next_free
                if entry.earliest_arrival:
                    start_time = max(start_time, entry.earliest_arrival)
                # Then push forward to somewhere the agent is actually working.
                # If nothing fits — day oversubscribed, or the agent is off —
                # keep the raw back-to-back time rather than dropping the
                # customer. Their ETA reads after hours, which is the truth.
                start_time = place_in_windows(windows, start_time, duration) or start_time
                entry.estimated_start = start_time
                s.add(entry)
                next_free = start_time + timedelta(minutes=duration)

        # Recalculate display positions across the full tenant queue for this date
        all_waiting = s.exec(
            select(QueueEntry).where(
                QueueEntry.tenant_id  == tenant_id,
                QueueEntry.queue_date == queue_date,
                QueueEntry.status     == "Waiting"
            ).order_by(QueueEntry.estimated_start, QueueEntry.joined_at)
        ).all()

        for i, e in enumerate(all_waiting):
            e.position = i + 1
            s.add(e)

        s.commit()


def cancel_party(tenant_id: int, entry_id: int) -> List[int]:
    """
    Cancel a booking and everyone linked to it (parent + all children),
    so a family booking never leaves stranded child entries holding agent slots.

    Given any entry in the party, resolves the party root (the entry itself if
    it's a parent, else its parent_entry_id) and cancels every active
    (Waiting/InService) entry in that party.

    Returns the list of distinct agent_ids touched, for ETA recalculation.
    """
    touched_agents: set = set()
    with Session(engine) as s:
        entry = s.get(QueueEntry, entry_id)
        if not entry or entry.tenant_id != tenant_id:
            return []
        root_id = entry.parent_entry_id or entry.id
        party = s.exec(
            select(QueueEntry).where(
                QueueEntry.tenant_id == tenant_id,
                (QueueEntry.id == root_id) | (QueueEntry.parent_entry_id == root_id),
            )
        ).all()
        for e in party:
            if e.status in ("Waiting", "InService"):
                e.status      = "Cancelled"
                e.closed_by   = "customer"   # they cancelled over WhatsApp
                e.finished_at = core.now()
                s.add(e)
                touched_agents.add(e.agent_id)
        s.commit()
    return list(touched_agents)


def find_walkin_insert_joined_at(
    assigned_agent_id: int, tenant_id: int, tenant: "Tenant",
    queue_date: str, walk_in_service_id: int,
    new_arrival: Optional[datetime] = None,
    exclude_entry_id: Optional[int] = None,
) -> Optional[datetime]:
    """
    Check whether a new entry can be slotted in before a future appointment.

    Walks the agent's current queue in order.  At each Waiting entry that has
    an earliest_arrival (i.e. a customer who said they'll arrive at time T),
    it tests whether the new entry's own start (respecting its declared
    arrival new_arrival) plus its duration finishes before T:

        max(max(now, queue_opens) + accumulated_backlog, new_arrival)
            + duration  <=  T

    If yes, the new entry finishes before that customer is even due, so we
    return a joined_at 1 second before that entry's joined_at.
    recalculate_queue then places the new entry ahead of them.

    Used by both walk-ins (new_arrival=None → can start immediately) and
    WhatsApp joiners (new_arrival = their declared arrival time).

    Returns None if the new entry should go at the end of the queue as usual.
    """
    with Session(engine) as s:
        entries = s.exec(
            select(QueueEntry).where(
                QueueEntry.agent_id   == assigned_agent_id,
                QueueEntry.tenant_id  == tenant_id,
                QueueEntry.queue_date == queue_date,
                QueueEntry.status.in_(["Waiting", "InService"])
            ).order_by(QueueEntry.joined_at)
        ).all()

        services = _service_map(s, entries)
        walk_in_svc = services.get(walk_in_service_id) or s.get(Service, walk_in_service_id)
        if not walk_in_svc:
            return None
        walk_in_duration = walk_in_svc.duration_minutes
        windows = get_working_windows(tenant, assigned_agent_id, queue_date)

        opens = datetime.strptime(
            f"{queue_date} {tenant.queue_opens:02d}:00", "%Y-%m-%d %H:%M"
        )
        base         = max(opens, core.now())
        current_time = core.now()
        running_minutes = 0

        for entry in entries:
            if exclude_entry_id and entry.id == exclude_entry_id:
                continue
            svc = services.get(entry.service_id)
            duration = svc.duration_minutes if svc else 60

            if entry.status == "InService":
                # Can't insert before someone already being served
                if entry.estimated_start:
                    finish = entry.estimated_start + timedelta(minutes=duration)
                    remaining = (finish - current_time).total_seconds() / 60
                    running_minutes += max(0, remaining)
                else:
                    running_minutes += duration
                continue

            # Waiting entry with a declared arrival time — can we slip in before them?
            if entry.earliest_arrival:
                new_start = base + timedelta(minutes=running_minutes)
                if new_arrival:
                    # New entry can't start before its own declared arrival
                    new_start = max(new_start, new_arrival)
                # The gap only exists if the agent is working through it.
                placed = place_in_windows(windows, new_start, walk_in_duration)
                if placed is None:
                    # running_minutes only grows, so no later gap can fit either.
                    return None
                walk_in_finish = placed + timedelta(minutes=walk_in_duration)
                if walk_in_finish <= entry.earliest_arrival:
                    return entry.joined_at - timedelta(seconds=1)

            running_minutes += duration

    return None
