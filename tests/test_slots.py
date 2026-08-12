"""
The appointment slot engine.

A queue entry's start is derived — recalculate_queue rewrites it whenever
anything ahead of it moves. An appointment's start is a promise. Most of what
follows is about that one distinction holding under pressure: when the queue
churns around it, when a walk-in arrives, when two customers reach for the same
slot at once, and when Redis is not there to arbitrate.
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

import main  # noqa: F401 — binds the test engine before app modules load
from app import core, db, queue_engine
from app.models import (Tenant, Service, Agent, AgentService, AgentSchedule,
                        QueueEntry)

DATE = "2026-06-22"
WEEKDAY = datetime.strptime(DATE, "%Y-%m-%d").weekday()


def _at(hour, minute=0):
    return datetime.strptime(DATE, "%Y-%m-%d").replace(hour=hour, minute=minute)


def _freeze(monkeypatch, when):
    monkeypatch.setattr(core, "now", lambda: when)


def _seed(opens=9, closes=12, duration=45, granularity=30, n_agents=1):
    """A tenant whose agents have no schedule rows, so they work tenant hours."""
    with Session(db.engine) as s:
        t = Tenant(
            business_name="Test Co", whatsapp_number="27810000000",
            evolution_instance="i", evolution_api_key="k",
            evolution_api_url="http://x",
            queue_opens=opens, queue_closes=closes,
            slot_granularity_minutes=granularity,
        )
        s.add(t); s.commit(); s.refresh(t)
        svc = Service(tenant_id=t.id, name="Cut", duration_minutes=duration)
        s.add(svc); s.commit(); s.refresh(svc)
        agent_ids = []
        for i in range(n_agents):
            a = Agent(tenant_id=t.id, name=f"A{i}")
            s.add(a); s.commit(); s.refresh(a)
            s.add(AgentService(agent_id=a.id, service_id=svc.id)); s.commit()
            agent_ids.append(a.id)
        return t.id, svc.id, agent_ids


def _tenant(tid):
    with Session(db.engine) as s:
        return s.get(Tenant, tid)


def _appointment(tid, svc, aid, start, duration=45, name="Booked"):
    """A fixed entry written directly, bypassing reserve_appointment."""
    with Session(db.engine) as s:
        e = QueueEntry(
            tenant_id=tid, service_id=svc, agent_id=aid,
            customer_number="2781@s.whatsapp.net", customer_name=name,
            queue_date=DATE, status="Waiting",
            estimated_start=start, slot_end=start + timedelta(minutes=duration),
            earliest_arrival=start, is_fixed=True,
        )
        s.add(e); s.commit(); s.refresh(e)
        return e.id


def _walkin(tid, svc, aid, name="Walk-in", status="Waiting",
            joined_at=None, estimated_start=None):
    with Session(db.engine) as s:
        e = QueueEntry(
            tenant_id=tid, service_id=svc, agent_id=aid,
            customer_number="2782@s.whatsapp.net", customer_name=name,
            queue_date=DATE, status=status, booked_via="walkin",
            estimated_start=estimated_start,
        )
        if joined_at:
            e.joined_at = joined_at
        s.add(e); s.commit(); s.refresh(e)
        return e.id


def _start_of(entry_id):
    with Session(db.engine) as s:
        return s.get(QueueEntry, entry_id).estimated_start


# =============================================================================
# list_free_slots — pure grid arithmetic
# =============================================================================
def test_the_grid_case_from_the_spec():
    """
    Window 09:00-12:00, granularity 30, service 45min, 10:00-10:45 taken.

      09:00  free — ends 09:45
      09:30  would end 10:15, over the booking
      10:00  BOOKED
      10:30  inside the booking
      11:00  free — ends 11:45
      11:30  would end 12:15, past close

    A slot is only offerable if the *whole service* fits, which is why 09:30
    goes despite 09:30 itself being unbooked.
    """
    windows = [(_at(9), _at(12))]
    booked = [(_at(10), _at(10, 45))]
    slots = queue_engine.list_free_slots(windows, booked, 45, 30)
    assert slots == [_at(9), _at(11)]


def test_a_service_never_spills_past_the_end_of_its_window():
    """The 11:30 drop above, isolated: nobody works through the close."""
    slots = queue_engine.list_free_slots([(_at(9), _at(12))], [], 45, 30)
    assert _at(11, 30) not in slots
    assert slots[-1] == _at(11)


def test_a_service_never_spans_a_break():
    """
    Split shift 09:00-12:00 and 13:00-17:00. A 45-minute service cannot start
    at 11:30 and run through a lunch the agent actually takes.
    """
    windows = [(_at(9), _at(12)), (_at(13), _at(17))]
    slots = queue_engine.list_free_slots(windows, [], 45, 30)
    assert _at(11, 30) not in slots
    assert _at(13) in slots, "the afternoon window starts its own grid"


def test_the_grid_restarts_at_each_window():
    """Not a single grid from opening — 13:00 is a start even after a break."""
    windows = [(_at(9), _at(9, 40)), (_at(13, 15), _at(14, 15))]
    slots = queue_engine.list_free_slots(windows, [], 30, 30)
    assert slots == [_at(9), _at(13, 15), _at(13, 45)]


def test_a_booking_that_ends_on_a_slot_boundary_does_not_eat_it():
    """
    Half-open intervals. An appointment ending at 10:00 leaves 10:00 free —
    getting this wrong loses a slot on every boundary in the day.
    """
    windows = [(_at(9), _at(12))]
    booked = [(_at(9), _at(10))]
    slots = queue_engine.list_free_slots(windows, booked, 30, 30)
    assert _at(10) in slots
    assert _at(9) not in slots
    assert _at(9, 30) not in slots


def test_floor_drops_times_that_have_passed():
    slots = queue_engine.list_free_slots(
        [(_at(9), _at(12))], [], 30, 30, floor=_at(10, 15))
    assert slots[0] == _at(10, 30)


def test_no_windows_means_no_slots():
    assert queue_engine.list_free_slots([], [], 30, 30) == []


def test_limit_stops_early():
    slots = queue_engine.list_free_slots([(_at(9), _at(12))], [], 30, 30, limit=2)
    assert slots == [_at(9), _at(9, 30)]


@pytest.mark.parametrize("duration,granularity", [(0, 30), (30, 0), (-5, 30)])
def test_nonsense_inputs_return_nothing_rather_than_looping(duration, granularity):
    assert queue_engine.list_free_slots(
        [(_at(9), _at(12))], [], duration, granularity) == []


# =============================================================================
# get_free_slots — against the database
# =============================================================================
def test_free_slots_skip_an_existing_appointment(monkeypatch):
    _freeze(monkeypatch, _at(8))
    tid, svc, [aid] = _seed()
    _appointment(tid, svc, aid, _at(10))

    slots = queue_engine.get_free_slots(_tenant(tid), aid, DATE, 45)

    assert slots == [_at(9), _at(11)]


def test_a_booking_does_not_knock_the_rest_of_the_day_off_the_grid(monkeypatch):
    """
    Subtracting a 10:00-10:45 appointment from the shift would leave a window
    starting at 10:45, and the grid restarts at each window — so the afternoon
    would be offered as 10:45 and 11:15. The grid belongs to the shift, not to
    whatever fragments today's bookings leave behind.
    """
    _freeze(monkeypatch, _at(8))
    tid, svc, [aid] = _seed(opens=9, closes=17, duration=45)
    _appointment(tid, svc, aid, _at(10))

    slots = queue_engine.get_free_slots(_tenant(tid), aid, DATE, 45)

    assert all(sl.minute in (0, 30) for sl in slots), \
        f"slots drifted off the tenant's grid: {[s.strftime('%H:%M') for s in slots]}"
    assert _at(11) in slots


def test_free_slots_skip_a_walkin_already_being_served(monkeypatch):
    """
    Offering a time the agent is visibly mid-cut is how a shop loses trust,
    even though flexible work would technically reschedule around it.
    """
    _freeze(monkeypatch, _at(8))
    tid, svc, [aid] = _seed()
    _walkin(tid, svc, aid, status="InService", estimated_start=_at(9, 30))

    slots = queue_engine.get_free_slots(_tenant(tid), aid, DATE, 45)

    assert _at(9, 30) not in slots
    assert _at(10, 30) in slots


def test_free_slots_respect_an_agent_schedule(monkeypatch):
    _freeze(monkeypatch, _at(7))
    tid, svc, [aid] = _seed(opens=8, closes=17)
    with Session(db.engine) as s:
        s.add(AgentSchedule(tenant_id=tid, agent_id=aid, weekday=WEEKDAY,
                            start_minute=9 * 60, end_minute=11 * 60))
        s.commit()

    slots = queue_engine.get_free_slots(_tenant(tid), aid, DATE, 60)

    assert slots == [_at(9), _at(9, 30), _at(10)]


def test_free_slots_never_offer_a_time_that_has_passed(monkeypatch):
    _freeze(monkeypatch, _at(10, 10))
    tid, svc, [aid] = _seed()

    slots = queue_engine.get_free_slots(_tenant(tid), aid, DATE, 45)

    assert slots == [_at(10, 30), _at(11)]


# =============================================================================
# The pin — an appointment's start is a promise
# =============================================================================
def test_recalculate_never_moves_a_fixed_entry(monkeypatch):
    """
    The single change the whole feature rests on. Without it a customer books
    14:00, the queue ahead of them runs long, and the bot quietly reschedules
    them to 15:20 without saying a word.
    """
    _freeze(monkeypatch, _at(9))
    tid, svc, [aid] = _seed(opens=9, closes=17)
    appt = _appointment(tid, svc, aid, _at(14))
    _walkin(tid, svc, aid, name="Runs long")

    queue_engine.recalculate_queue(tid, aid, DATE)

    assert _start_of(appt) == _at(14)


def test_an_appointment_booked_earlier_does_not_push_this_morning(monkeypatch):
    """
    The joined_at trap. Entries are scheduled in joined_at order, and an
    appointment booked last week has the earliest joined_at of anyone in the
    shop. Treated as a queue position it would advance the agent's clock to
    14:45 before the 09:00 walk-in is even considered.
    """
    _freeze(monkeypatch, _at(9))
    tid, svc, [aid] = _seed(opens=9, closes=17)
    _appointment(tid, svc, aid, _at(14))
    walkin = _walkin(tid, svc, aid, joined_at=_at(9) - timedelta(days=7))

    queue_engine.recalculate_queue(tid, aid, DATE)

    assert _start_of(walkin) == _at(9), "the walk-in was pushed behind an afternoon booking"


def test_flexible_work_is_scheduled_around_an_appointment(monkeypatch):
    """A walk-in that would run into a booked slot starts after it instead."""
    _freeze(monkeypatch, _at(9, 30))
    tid, svc, [aid] = _seed(opens=9, closes=17, duration=45)
    _appointment(tid, svc, aid, _at(10))
    walkin = _walkin(tid, svc, aid)

    queue_engine.recalculate_queue(tid, aid, DATE)

    assert _start_of(walkin) == _at(10, 45), "a walk-in was placed on top of an appointment"


def test_a_walkin_fills_the_gap_between_two_appointments(monkeypatch):
    """
    Hybrid mode, which is the whole reason appointments reuse QueueEntry: a
    salon takes booked times and people off the street on the same day.
    """
    _freeze(monkeypatch, _at(9))
    tid, svc, [aid] = _seed(opens=9, closes=17, duration=45)
    _appointment(tid, svc, aid, _at(9), name="First")
    _appointment(tid, svc, aid, _at(11), name="Second")
    walkin = _walkin(tid, svc, aid)

    queue_engine.recalculate_queue(tid, aid, DATE)

    assert _start_of(walkin) == _at(9, 45), "the 45-minute gap went unused"


# =============================================================================
# Backlog and assignment
# =============================================================================
def test_an_afternoon_appointment_is_not_this_morning_s_backlog(monkeypatch):
    """
    Backlog answers "how long until this agent finishes what is queued". A
    16:00 booking is not queued work at 09:00 — counting it would quote a
    walk-in a seven-hour wait for an empty shop.
    """
    _freeze(monkeypatch, _at(9))
    tid, svc, [aid] = _seed(opens=9, closes=17)
    _appointment(tid, svc, aid, _at(16))

    assert queue_engine.get_agent_backlog_minutes(aid, tid, DATE) == 0


def test_an_agent_with_appointments_is_not_treated_as_busy(monkeypatch):
    """
    Otherwise every stylist with an afternoon booking loses assignment all
    morning to whoever happens to have an empty diary.
    """
    _freeze(monkeypatch, _at(9))
    tid, svc, [a0, a1] = _seed(opens=9, closes=17, n_agents=2)
    _appointment(tid, svc, a0, _at(16))
    _walkin(tid, svc, a1, estimated_start=_at(9))

    chosen = queue_engine.assign_agent(_tenant(tid), svc, None, DATE)

    assert chosen == a0, "the agent with only an afternoon booking should win"


def test_a_quote_is_never_placed_on_top_of_an_appointment(monkeypatch):
    _freeze(monkeypatch, _at(9))
    tid, svc, [aid] = _seed(opens=9, closes=17, duration=45)
    _appointment(tid, svc, aid, _at(9))

    eta = queue_engine.calculate_estimated_start(_tenant(tid), aid, DATE, 0)

    assert eta == _at(9, 45)


# =============================================================================
# reserve_appointment — three layers against a double booking
# =============================================================================
def test_reserve_writes_a_fixed_entry_with_its_own_end(fake_redis, monkeypatch):
    _freeze(monkeypatch, _at(8))
    tid, svc, [aid] = _seed(duration=45)

    entry_id = queue_engine.reserve_appointment(
        _tenant(tid), aid, svc, DATE, _at(10), "2781@s.whatsapp.net", "Thabo")

    assert entry_id is not None
    with Session(db.engine) as s:
        e = s.get(QueueEntry, entry_id)
    assert e.is_fixed is True
    assert e.estimated_start == _at(10)
    assert e.slot_end == _at(10, 45)
    assert e.earliest_arrival == _at(10), "choosing a time is declaring an arrival"


def test_the_same_slot_cannot_be_taken_twice(fake_redis, monkeypatch):
    _freeze(monkeypatch, _at(8))
    tid, svc, [aid] = _seed()
    t = _tenant(tid)

    first  = queue_engine.reserve_appointment(t, aid, svc, DATE, _at(10),
                                              "2781@s.whatsapp.net", "Thabo")
    second = queue_engine.reserve_appointment(t, aid, svc, DATE, _at(10),
                                              "2782@s.whatsapp.net", "Naledi")

    assert first is not None
    assert second is None, "two customers were sent to one chair"


def test_a_partial_overlap_is_refused(fake_redis, monkeypatch):
    """
    10:00 for 45 minutes against 10:30. Different starts, so the unique index
    cannot see this one — the in-transaction overlap check is what catches it.
    """
    _freeze(monkeypatch, _at(8))
    tid, svc, [aid] = _seed(duration=45)
    t = _tenant(tid)

    queue_engine.reserve_appointment(t, aid, svc, DATE, _at(10),
                                     "2781@s.whatsapp.net", "Thabo")
    clash = queue_engine.reserve_appointment(t, aid, svc, DATE, _at(10, 30),
                                             "2782@s.whatsapp.net", "Naledi")

    assert clash is None


def test_touching_appointments_are_allowed(fake_redis, monkeypatch):
    """10:00-10:45 and 10:45-11:30 do not overlap. Refusing them loses a slot."""
    _freeze(monkeypatch, _at(8))
    tid, svc, [aid] = _seed(duration=45)
    t = _tenant(tid)

    queue_engine.reserve_appointment(t, aid, svc, DATE, _at(10),
                                     "2781@s.whatsapp.net", "Thabo")
    back_to_back = queue_engine.reserve_appointment(t, aid, svc, DATE, _at(10, 45),
                                                    "2782@s.whatsapp.net", "Naledi")

    assert back_to_back is not None


def test_a_cancelled_appointment_frees_its_slot(fake_redis, monkeypatch):
    _freeze(monkeypatch, _at(8))
    tid, svc, [aid] = _seed()
    t = _tenant(tid)
    first = queue_engine.reserve_appointment(t, aid, svc, DATE, _at(10),
                                             "2781@s.whatsapp.net", "Thabo")
    queue_engine.cancel_party(tid, first)

    again = queue_engine.reserve_appointment(t, aid, svc, DATE, _at(10),
                                             "2782@s.whatsapp.net", "Naledi")

    assert again is not None, "a cancelled slot must be sellable again"


def test_a_walkin_in_the_way_is_not_a_conflict(fake_redis, monkeypatch):
    """Flexible work reschedules around appointments. That is what fixed means."""
    _freeze(monkeypatch, _at(9))
    tid, svc, [aid] = _seed(opens=9, closes=17, duration=45)
    _walkin(tid, svc, aid, estimated_start=_at(10))

    booked = queue_engine.reserve_appointment(
        _tenant(tid), aid, svc, DATE, _at(10), "2781@s.whatsapp.net", "Thabo")

    assert booked is not None


def test_reserve_still_refuses_a_clash_with_redis_down(broken_redis, monkeypatch):
    """
    booking_lock degrades open by design, so with Redis unreachable there is no
    serialisation at all. The database constraint is the only thing left, and
    for appointments it has to be enough.
    """
    _freeze(monkeypatch, _at(8))
    tid, svc, [aid] = _seed()
    t = _tenant(tid)

    first  = queue_engine.reserve_appointment(t, aid, svc, DATE, _at(10),
                                              "2781@s.whatsapp.net", "Thabo")
    second = queue_engine.reserve_appointment(t, aid, svc, DATE, _at(10),
                                              "2782@s.whatsapp.net", "Naledi")

    assert first is not None
    assert second is None


def test_the_unique_index_rejects_a_duplicate_written_directly():
    """
    Layer 3, tested past the application code. Any future path that writes a
    fixed entry — a dashboard booking, an import — hits this too.
    """
    tid, svc, [aid] = _seed()
    _appointment(tid, svc, aid, _at(10))

    with pytest.raises(IntegrityError):
        _appointment(tid, svc, aid, _at(10), name="Duplicate")


def test_booking_an_appointment_reschedules_the_flexible_queue(fake_redis, monkeypatch):
    _freeze(monkeypatch, _at(9))
    tid, svc, [aid] = _seed(opens=9, closes=17, duration=45)
    walkin = _walkin(tid, svc, aid)
    queue_engine.recalculate_queue(tid, aid, DATE)
    assert _start_of(walkin) == _at(9)

    queue_engine.reserve_appointment(
        _tenant(tid), aid, svc, DATE, _at(9), "2781@s.whatsapp.net", "Thabo")

    assert _start_of(walkin) == _at(9, 45), \
        "the walk-in kept a slot that has just been promised to someone else"
