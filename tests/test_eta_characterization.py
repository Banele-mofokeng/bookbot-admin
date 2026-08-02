"""
Characterization tests for the four ETA functions.

These are NOT specifications. They pin down what the queue engine does *today*,
exact minute by exact minute, so that per-agent schedules (#3) — which has to
reach into all four of these — cannot change behaviour silently. Every one of
them was written by reading the code, not the intent.

    calculate_estimated_start      backlog minutes -> a wall-clock datetime
    get_agent_backlog_minutes      how long until this agent is free
    recalculate_queue              rewrite estimated_start + position
    find_walkin_insert_joined_at   can a new entry fill an idle gap?

Behaviour that is arguably wrong is pinned anyway and tagged `QUIRK:`. When a
QUIRK test fails during the schedules work that is the point — read it, decide
whether the new behaviour is the fix, and update the pin deliberately. A test
without that tag failing is a regression.

Everything runs against a frozen clock (`frozen_now`), because every function
here reads now() and half of them read it more than once.
"""
from datetime import datetime, timedelta

import pytest
from sqlmodel import Session, select

from app import core, db, queue_engine
from app.models import Tenant, Service, Agent, AgentService, QueueEntry


DATE = "2026-06-20"
NOW  = datetime(2026, 6, 20, 10, 0)      # mid-morning, queue already open
OPENS, CLOSES = 8, 17


def at(hour, minute=0):
    """A datetime on DATE."""
    return datetime(2026, 6, 20, hour, minute)


@pytest.fixture
def frozen_now(monkeypatch):
    """Freeze now() at 10:00 on DATE. Returns it so tests can do arithmetic."""
    monkeypatch.setattr(core, "now", lambda: NOW)
    return NOW


# ── fixtures ─────────────────────────────────────────────────────────────────
def _tenant(opens=OPENS, closes=CLOSES):
    with Session(db.engine) as s:
        t = Tenant(
            business_name="Pin Co", whatsapp_number="27810000000",
            evolution_instance="i", evolution_api_key="k",
            evolution_api_url="http://x",
            queue_opens=opens, queue_closes=closes, advance_days=1,
        )
        s.add(t); s.commit(); s.refresh(t)
        return t


def _service(tenant_id, minutes, name=None):
    with Session(db.engine) as s:
        sv = Service(tenant_id=tenant_id, name=name or f"S{minutes}",
                     duration_minutes=minutes)
        s.add(sv); s.commit(); s.refresh(sv)
        return sv.id


def _agent(tenant_id, service_ids=(), name="A"):
    with Session(db.engine) as s:
        a = Agent(tenant_id=tenant_id, name=name)
        s.add(a); s.commit(); s.refresh(a)
        for sid in service_ids:
            s.add(AgentService(agent_id=a.id, service_id=sid))
        s.commit()
        return a.id


_joined_seq = [0]


@pytest.fixture(autouse=True)
def _reset_joined_seq():
    """Keep default joined_at values well before NOW no matter how many tests run."""
    _joined_seq[0] = 0
    yield


def _entry(tenant_id, service_id, agent_id, *, status="Waiting",
           estimated_start=None, earliest_arrival=None, joined_at=None,
           date=DATE, name="C", position=0):
    """Entries default to strictly increasing joined_at, in creation order."""
    _joined_seq[0] += 1
    with Session(db.engine) as s:
        e = QueueEntry(
            tenant_id=tenant_id, service_id=service_id, agent_id=agent_id,
            customer_number="2781000", customer_name=name, status=status,
            queue_date=date, estimated_start=estimated_start,
            earliest_arrival=earliest_arrival, position=position,
            joined_at=joined_at or (at(7) + timedelta(seconds=_joined_seq[0])),
        )
        s.add(e); s.commit(); s.refresh(e)
        return e.id


def _read(entry_id):
    with Session(db.engine) as s:
        return s.get(QueueEntry, entry_id)


# =============================================================================
# calculate_estimated_start
# =============================================================================
def test_ces_anchors_to_now_once_the_queue_has_opened(frozen_now):
    t = _tenant()
    assert queue_engine.calculate_estimated_start(t, 1, DATE, 90) == at(11, 30)


def test_ces_anchors_to_opening_time_before_the_queue_opens(monkeypatch):
    monkeypatch.setattr(core, "now", lambda: at(7, 0))
    t = _tenant()
    # Not 08:30 from 07:00 — the base is opening time, not the current time.
    assert queue_engine.calculate_estimated_start(t, 1, DATE, 90) == at(9, 30)


def test_ces_zero_backlog_is_the_base_itself(frozen_now):
    t = _tenant()
    assert queue_engine.calculate_estimated_start(t, 1, DATE, 0) == NOW


def test_ces_declared_arrival_pushes_the_start_later(frozen_now):
    t = _tenant()
    assert queue_engine.calculate_estimated_start(
        t, 1, DATE, 90, earliest_arrival=at(13, 0)) == at(13, 0)


def test_ces_declared_arrival_earlier_than_the_queue_is_ignored(frozen_now):
    """You can't jump the queue by claiming an early arrival."""
    t = _tenant()
    assert queue_engine.calculate_estimated_start(
        t, 1, DATE, 90, earliest_arrival=at(9, 0)) == at(11, 30)


def test_ces_for_a_future_date_anchors_to_that_days_opening(frozen_now):
    t = _tenant()
    assert queue_engine.calculate_estimated_start(t, 1, "2026-06-21", 60) == \
        datetime(2026, 6, 21, 9, 0)


def test_ces_QUIRK_ignores_closing_time_entirely(frozen_now):
    """
    QUIRK: a quote that overflows the agent's last working window is returned
    unchanged, so a big enough backlog still reads as after hours or past
    midnight. Narrowed by per-agent schedules — a quote landing inside the
    working day is now snapped to a moment the agent is genuinely available —
    but kept for the overflow case, where the alternatives are quoting a time
    nobody can honour or refusing the booking outright.
    """
    t = _tenant(opens=8, closes=17)
    assert queue_engine.calculate_estimated_start(t, 1, DATE, 600) == at(20, 0)
    assert queue_engine.calculate_estimated_start(t, 1, DATE, 20 * 60) == \
        datetime(2026, 6, 21, 6, 0)


# =============================================================================
# get_agent_backlog_minutes
# =============================================================================
def test_backlog_of_an_empty_queue_is_zero(frozen_now):
    t = _tenant(); svc = _service(t.id, 60); a = _agent(t.id, [svc])
    assert queue_engine.get_agent_backlog_minutes(a, t.id, DATE) == 0


def test_backlog_without_an_estimated_start_is_the_full_duration(frozen_now):
    t = _tenant(); svc = _service(t.id, 45); a = _agent(t.id, [svc])
    _entry(t.id, svc, a)
    assert queue_engine.get_agent_backlog_minutes(a, t.id, DATE) == 45


def test_backlog_of_a_scheduled_waiting_entry_runs_to_its_finish(frozen_now):
    """estimated_start 10:30 + 60min = finishes 11:30, i.e. 90 min from now."""
    t = _tenant(); svc = _service(t.id, 60); a = _agent(t.id, [svc])
    _entry(t.id, svc, a, estimated_start=at(10, 30))
    assert queue_engine.get_agent_backlog_minutes(a, t.id, DATE) == 90


def test_backlog_of_a_waiting_entry_never_drops_below_its_duration(frozen_now):
    """Scheduled to have finished at 09:00, but it hasn't started — still 60."""
    t = _tenant(); svc = _service(t.id, 60); a = _agent(t.id, [svc])
    _entry(t.id, svc, a, estimated_start=at(8, 0))
    assert queue_engine.get_agent_backlog_minutes(a, t.id, DATE) == 60


def test_backlog_of_an_inservice_entry_is_only_the_time_remaining(frozen_now):
    """Started 09:30, 60min service — 30 left, not 60."""
    t = _tenant(); svc = _service(t.id, 60); a = _agent(t.id, [svc])
    _entry(t.id, svc, a, status="InService", estimated_start=at(9, 30))
    assert queue_engine.get_agent_backlog_minutes(a, t.id, DATE) == 30


def test_backlog_of_an_overrunning_inservice_entry_is_zero(frozen_now):
    """
    Should have finished at 09:00. Contributes 0, not negative — so an agent
    running 3 hours late still reads as free right now.
    """
    t = _tenant(); svc = _service(t.id, 60); a = _agent(t.id, [svc])
    _entry(t.id, svc, a, status="InService", estimated_start=at(8, 0))
    assert queue_engine.get_agent_backlog_minutes(a, t.id, DATE) == 0


def test_backlog_QUIRK_a_late_appointment_counts_its_idle_wait_as_backlog(frozen_now):
    """
    QUIRK: one 16:00 booking makes the agent look 420 minutes deep at 10:00,
    even though they are free all morning. Backlog is "time until the last
    entry finishes", not "sum of work". Gap-filling exists to work around
    exactly this, and per-agent schedules will have to decide which it means.
    """
    t = _tenant(); svc = _service(t.id, 60); a = _agent(t.id, [svc])
    _entry(t.id, svc, a, estimated_start=at(16, 0), earliest_arrival=at(16, 0))
    assert queue_engine.get_agent_backlog_minutes(a, t.id, DATE) == 420


def test_backlog_sums_across_entries(frozen_now):
    t = _tenant(); svc = _service(t.id, 30); a = _agent(t.id, [svc])
    _entry(t.id, svc, a, status="InService", estimated_start=at(9, 45))  # 15 left
    _entry(t.id, svc, a)                                                 # 30 flat
    _entry(t.id, svc, a, estimated_start=at(10, 45))                     # -> 11:15, 75
    assert queue_engine.get_agent_backlog_minutes(a, t.id, DATE) == 15 + 30 + 75


def test_backlog_excludes_terminal_statuses(frozen_now):
    t = _tenant(); svc = _service(t.id, 60); a = _agent(t.id, [svc])
    for st in ("Done", "NoShow", "Cancelled"):
        _entry(t.id, svc, a, status=st)
    assert queue_engine.get_agent_backlog_minutes(a, t.id, DATE) == 0


def test_backlog_excludes_other_agents_dates_and_tenants(frozen_now):
    t = _tenant(); svc = _service(t.id, 60)
    a, other = _agent(t.id, [svc], "A"), _agent(t.id, [svc], "B")
    t2 = _tenant(); svc2 = _service(t2.id, 60); a2 = _agent(t2.id, [svc2], "X")

    _entry(t.id, svc, other)                       # other agent
    _entry(t.id, svc, a, date="2026-06-21")        # other date
    _entry(t2.id, svc2, a2)                        # other tenant
    _entry(t.id, svc, a)                           # the only one that counts

    assert queue_engine.get_agent_backlog_minutes(a, t.id, DATE) == 60


def test_backlog_exclude_entry_id_skips_that_entry(frozen_now):
    t = _tenant(); svc = _service(t.id, 60); a = _agent(t.id, [svc])
    keep = _entry(t.id, svc, a)
    drop = _entry(t.id, svc, a)
    assert queue_engine.get_agent_backlog_minutes(a, t.id, DATE, exclude_entry_id=drop) == 60


def test_backlog_QUIRK_an_entry_whose_service_vanished_contributes_nothing(frozen_now):
    """
    QUIRK: a deleted Service makes its entries invisible to backlog, so the
    agent reads as freer than they are. recalculate_queue disagrees — it
    falls back to 60 minutes for the same row. The two are inconsistent.
    """
    t = _tenant(); svc = _service(t.id, 60); a = _agent(t.id, [svc])
    _entry(t.id, svc, a)
    with Session(db.engine) as s:
        s.delete(s.get(Service, svc)); s.commit()
    assert queue_engine.get_agent_backlog_minutes(a, t.id, DATE) == 0


def test_backlog_truncates_rather_than_rounds(frozen_now):
    """90.5 minutes reads as 90."""
    t = _tenant(); svc = _service(t.id, 60); a = _agent(t.id, [svc])
    _entry(t.id, svc, a, estimated_start=at(10, 30) + timedelta(seconds=30))
    assert queue_engine.get_agent_backlog_minutes(a, t.id, DATE) == 90


def test_batched_backlog_agrees_with_the_single_agent_version(frozen_now):
    t = _tenant(); svc = _service(t.id, 60)
    a, b = _agent(t.id, [svc], "A"), _agent(t.id, [svc], "B")
    _entry(t.id, svc, a, estimated_start=at(10, 30))
    _entry(t.id, svc, b, status="InService", estimated_start=at(9, 30))

    assert queue_engine.get_agent_backlogs_minutes([a, b], t.id, DATE) == {a: 90, b: 30}


def test_batched_backlog_reports_zero_for_an_agent_with_no_entries(frozen_now):
    t = _tenant(); svc = _service(t.id, 60); a = _agent(t.id, [svc])
    assert queue_engine.get_agent_backlogs_minutes([a], t.id, DATE) == {a: 0}


# =============================================================================
# recalculate_queue
# =============================================================================
def test_recalc_packs_waiting_entries_back_to_back_from_now(frozen_now):
    t = _tenant(); svc = _service(t.id, 60); short = _service(t.id, 30)
    a = _agent(t.id, [svc, short])
    e1 = _entry(t.id, svc, a);  e2 = _entry(t.id, short, a);  e3 = _entry(t.id, svc, a)

    queue_engine.recalculate_queue(t.id, a, DATE)

    assert _read(e1).estimated_start == at(10, 0)
    assert _read(e2).estimated_start == at(11, 0)
    assert _read(e3).estimated_start == at(11, 30)


def test_recalc_before_opening_packs_from_opening_time(monkeypatch):
    monkeypatch.setattr(core, "now", lambda: at(6, 0))
    t = _tenant(); svc = _service(t.id, 60); a = _agent(t.id, [svc])
    e1 = _entry(t.id, svc, a)
    queue_engine.recalculate_queue(t.id, a, DATE)
    assert _read(e1).estimated_start == at(8, 0)


def test_recalc_leaves_inservice_frozen_and_resumes_after_it(frozen_now):
    """InService entries are never re-timed; the queue resumes at their finish."""
    t = _tenant(); svc = _service(t.id, 60); a = _agent(t.id, [svc])
    live = _entry(t.id, svc, a, status="InService", estimated_start=at(9, 30))
    nxt  = _entry(t.id, svc, a)

    queue_engine.recalculate_queue(t.id, a, DATE)

    assert _read(live).estimated_start == at(9, 30), "InService was re-timed"
    assert _read(nxt).estimated_start == at(10, 30)


def test_recalc_QUIRK_an_inservice_entry_with_no_eta_advances_nothing(frozen_now):
    """
    QUIRK: an InService row with estimated_start=None contributes zero, so the
    next customer is scheduled for right now even though someone is being
    served. Reachable for entries created before ETAs were persisted.
    """
    t = _tenant(); svc = _service(t.id, 60); a = _agent(t.id, [svc])
    _entry(t.id, svc, a, status="InService", estimated_start=None)
    nxt = _entry(t.id, svc, a)

    queue_engine.recalculate_queue(t.id, a, DATE)
    assert _read(nxt).estimated_start == at(10, 0)


def test_recalc_honours_a_declared_arrival_and_leaves_the_gap_idle(frozen_now):
    """
    QUIRK: the 10:30–14:00 gap is never filled by the entry behind. Everything
    after a late arrival slides, rather than moving up.
    """
    t = _tenant(); half = _service(t.id, 30); full = _service(t.id, 60)
    a = _agent(t.id, [half, full])
    e1 = _entry(t.id, half, a)
    e2 = _entry(t.id, full, a, earliest_arrival=at(14, 0))
    e3 = _entry(t.id, half, a)

    queue_engine.recalculate_queue(t.id, a, DATE)

    assert _read(e1).estimated_start == at(10, 0)
    assert _read(e2).estimated_start == at(14, 0)
    assert _read(e3).estimated_start == at(15, 0)


def test_recalc_orders_by_joined_at_not_by_position(frozen_now):
    t = _tenant(); svc = _service(t.id, 60); a = _agent(t.id, [svc])
    late  = _entry(t.id, svc, a, joined_at=at(9, 0), position=1)
    early = _entry(t.id, svc, a, joined_at=at(8, 0), position=2)

    queue_engine.recalculate_queue(t.id, a, DATE)

    assert _read(early).estimated_start == at(10, 0)
    assert _read(late).estimated_start == at(11, 0)


def test_recalc_falls_back_to_60_minutes_for_a_missing_service(frozen_now):
    """Contrast with backlog, which skips the row entirely — see the QUIRK above."""
    t = _tenant(); svc = _service(t.id, 15); a = _agent(t.id, [svc])
    orphan = _entry(t.id, svc, a)
    after  = _entry(t.id, svc, a)
    with Session(db.engine) as s:
        s.delete(s.get(Service, svc)); s.commit()

    queue_engine.recalculate_queue(t.id, a, DATE)
    assert _read(after).estimated_start == at(11, 0)


def test_recalc_renumbers_positions_across_every_agent_not_just_this_one(frozen_now):
    """
    Called with one agent_id, but positions are rewritten tenant-wide for the
    date, ordered by (estimated_start, joined_at). The customer-facing number
    is a whole-shop position, not a per-agent one.
    """
    t = _tenant(); svc = _service(t.id, 60)
    a, b = _agent(t.id, [svc], "A"), _agent(t.id, [svc], "B")
    on_a = _entry(t.id, svc, a, earliest_arrival=at(14, 0))
    on_b = _entry(t.id, svc, b, estimated_start=at(10, 0), joined_at=at(9, 0))

    queue_engine.recalculate_queue(t.id, a, DATE)

    assert _read(on_b).position == 1, "other agent's entry was not renumbered"
    assert _read(on_a).position == 2


def test_recalc_QUIRK_leaves_stale_positions_on_terminal_entries(frozen_now):
    """
    QUIRK: only Waiting rows are renumbered, so a Done entry keeps whatever
    position it had and can collide with a live one. Harmless today only
    because the dashboard sorts terminal entries to the bottom.
    """
    t = _tenant(); svc = _service(t.id, 60); a = _agent(t.id, [svc])
    done = _entry(t.id, svc, a, status="Done", position=1)
    live = _entry(t.id, svc, a)

    queue_engine.recalculate_queue(t.id, a, DATE)

    assert _read(done).position == 1
    assert _read(live).position == 1, "both rows now claim position 1"


def test_recalc_on_an_unknown_tenant_is_a_no_op(frozen_now):
    queue_engine.recalculate_queue(99999, 1, DATE)   # must not raise


def test_recalc_ignores_other_dates(frozen_now):
    t = _tenant(); svc = _service(t.id, 60); a = _agent(t.id, [svc])
    tomorrow = _entry(t.id, svc, a, date="2026-06-21", estimated_start=at(15, 0))
    queue_engine.recalculate_queue(t.id, a, DATE)
    assert _read(tomorrow).estimated_start == at(15, 0)


# =============================================================================
# find_walkin_insert_joined_at
# =============================================================================
def test_gapfill_returns_none_for_an_empty_queue(frozen_now):
    t = _tenant(); svc = _service(t.id, 30); a = _agent(t.id, [svc])
    assert queue_engine.find_walkin_insert_joined_at(a, t.id, t, DATE, svc) is None


def test_gapfill_QUIRK_ignores_waiting_entries_with_no_declared_arrival(frozen_now):
    """
    QUIRK: only an entry carrying earliest_arrival opens a gap. A queue of
    ordinary waiters never yields an insert point, however idle the agent is.
    """
    t = _tenant(); svc = _service(t.id, 30); a = _agent(t.id, [svc])
    _entry(t.id, svc, a)
    _entry(t.id, svc, a)
    assert queue_engine.find_walkin_insert_joined_at(a, t.id, t, DATE, svc) is None


def test_gapfill_slots_in_one_second_before_the_late_arrival(frozen_now):
    """30min walk-in at 10:00 finishes 10:30, well before a 14:00 booking."""
    t = _tenant(); svc = _service(t.id, 30); a = _agent(t.id, [svc])
    later = _entry(t.id, svc, a, earliest_arrival=at(14, 0), joined_at=at(9, 0))

    got = queue_engine.find_walkin_insert_joined_at(a, t.id, t, DATE, svc)
    assert got == at(9, 0) - timedelta(seconds=1)


def test_gapfill_declines_when_the_walk_in_would_overrun(frozen_now):
    """Finishes 10:30, the booking is due 10:20 — one minute short is still no."""
    t = _tenant(); svc = _service(t.id, 30); a = _agent(t.id, [svc])
    _entry(t.id, svc, a, earliest_arrival=at(10, 20), joined_at=at(9, 0))
    assert queue_engine.find_walkin_insert_joined_at(a, t.id, t, DATE, svc) is None


def test_gapfill_accepts_a_finish_exactly_on_the_arrival_time(frozen_now):
    """The comparison is <=, so back-to-back counts as fitting."""
    t = _tenant(); svc = _service(t.id, 30); a = _agent(t.id, [svc])
    _entry(t.id, svc, a, earliest_arrival=at(10, 30), joined_at=at(9, 0))
    assert queue_engine.find_walkin_insert_joined_at(a, t.id, t, DATE, svc) is not None


def test_gapfill_respects_the_new_entrys_own_declared_arrival(frozen_now):
    """Can't start before 13:50, so it runs to 14:20 and no longer fits."""
    t = _tenant(); svc = _service(t.id, 30); a = _agent(t.id, [svc])
    _entry(t.id, svc, a, earliest_arrival=at(14, 0), joined_at=at(9, 0))
    assert queue_engine.find_walkin_insert_joined_at(
        a, t.id, t, DATE, svc, new_arrival=at(13, 50)) is None


def test_gapfill_pushes_the_start_past_an_inservice_entry(frozen_now):
    t = _tenant(); svc = _service(t.id, 30); long = _service(t.id, 60)
    a = _agent(t.id, [svc, long])
    _entry(t.id, long, a, status="InService", estimated_start=at(9, 30),
           joined_at=at(8, 0))                       # 30 min remaining
    _entry(t.id, svc, a, earliest_arrival=at(11, 0), joined_at=at(9, 0))

    # Walk-in starts 10:30, ends 11:00, exactly meets the 11:00 booking.
    assert queue_engine.find_walkin_insert_joined_at(a, t.id, t, DATE, svc) is not None
    # A 45-minute service would run to 11:15 and miss it.
    long45 = _service(t.id, 45)
    assert queue_engine.find_walkin_insert_joined_at(a, t.id, t, DATE, long45) is None


def test_gapfill_accumulates_the_queue_ahead_of_the_gap(frozen_now):
    t = _tenant(); svc = _service(t.id, 30); a = _agent(t.id, [svc])
    _entry(t.id, svc, a, joined_at=at(8, 0))                              # +30
    _entry(t.id, svc, a, joined_at=at(8, 30))                             # +30
    _entry(t.id, svc, a, earliest_arrival=at(12, 0), joined_at=at(9, 0))

    # Walk-in starts 11:00 after two 30-min entries, finishes 11:30 <= 12:00.
    assert queue_engine.find_walkin_insert_joined_at(a, t.id, t, DATE, svc) is not None


def test_gapfill_takes_the_first_fitting_gap_not_the_best(frozen_now):
    t = _tenant(); svc = _service(t.id, 30); a = _agent(t.id, [svc])
    first  = _entry(t.id, svc, a, earliest_arrival=at(13, 0), joined_at=at(9, 0))
    second = _entry(t.id, svc, a, earliest_arrival=at(16, 0), joined_at=at(9, 30))

    got = queue_engine.find_walkin_insert_joined_at(a, t.id, t, DATE, svc)
    assert got == at(9, 0) - timedelta(seconds=1)


def test_gapfill_exclude_entry_id_skips_that_entry(frozen_now):
    """
    Used when re-slotting an existing entry, so it doesn't block itself.

    The blocker is due 10:20, which a 30-min walk-in starting now can't beat,
    and it also pushes the entry behind it out of reach: 10:00 + 30 = 10:30
    start, finishing 11:00, past the 10:45 booking. Exclude the blocker and the
    same walk-in starts at 10:00, finishes 10:30, and fits.
    """
    t = _tenant(); svc = _service(t.id, 30); a = _agent(t.id, [svc])
    blocker = _entry(t.id, svc, a, earliest_arrival=at(10, 20), joined_at=at(9, 0))
    behind  = _entry(t.id, svc, a, earliest_arrival=at(10, 45), joined_at=at(9, 30))

    assert queue_engine.find_walkin_insert_joined_at(a, t.id, t, DATE, svc) is None
    assert queue_engine.find_walkin_insert_joined_at(
        a, t.id, t, DATE, svc, exclude_entry_id=blocker
    ) == at(9, 30) - timedelta(seconds=1)


def test_gapfill_returns_none_when_the_service_is_missing(frozen_now):
    t = _tenant(); svc = _service(t.id, 30); a = _agent(t.id, [svc])
    _entry(t.id, svc, a, earliest_arrival=at(14, 0), joined_at=at(9, 0))
    assert queue_engine.find_walkin_insert_joined_at(a, t.id, t, DATE, 99999) is None


def test_gapfill_QUIRK_ignores_closing_time(frozen_now):
    """
    QUIRK: a 22:00 declared arrival is still treated as a normal gap even
    though the shop shuts at 17:00 — nothing rejects an appointment made for
    after hours. The walk-in slotted in front of it IS now placed inside the
    agent's working windows, so what survives is only the far end: an existing
    booking outside working hours still counts as a gap boundary.
    """
    t = _tenant(opens=8, closes=17); svc = _service(t.id, 30)
    a = _agent(t.id, [svc])
    _entry(t.id, svc, a, earliest_arrival=at(22, 0), joined_at=at(9, 0))
    assert queue_engine.find_walkin_insert_joined_at(a, t.id, t, DATE, svc) is not None


# =============================================================================
# The four together — one booking end to end
# =============================================================================
def test_end_to_end_a_second_booking_lands_behind_the_first(frozen_now):
    """
    Ties the functions together in the order a real booking uses them, so a
    change that keeps each one individually green but breaks the composition
    still fails.
    """
    t = _tenant(); svc = _service(t.id, 60); a = _agent(t.id, [svc])

    assert queue_engine.get_agent_backlog_minutes(a, t.id, DATE) == 0
    first_eta = queue_engine.calculate_estimated_start(t, a, DATE, 0)
    assert first_eta == at(10, 0)
    first = _entry(t.id, svc, a, estimated_start=first_eta)

    backlog = queue_engine.get_agent_backlog_minutes(a, t.id, DATE)
    assert backlog == 60
    second_eta = queue_engine.calculate_estimated_start(t, a, DATE, backlog)
    assert second_eta == at(11, 0)
    second = _entry(t.id, svc, a, estimated_start=second_eta)

    queue_engine.recalculate_queue(t.id, a, DATE)

    assert _read(first).estimated_start == at(10, 0)
    assert _read(second).estimated_start == at(11, 0)
    assert (_read(first).position, _read(second).position) == (1, 2)
