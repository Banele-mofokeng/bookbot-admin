"""
The day-view endpoint behind the calendar.

Everything here is about what the grid draws, and the answer is always the
same: it must draw the day the engine actually scheduled, and it must never
draw a day fuller or emptier than the one that exists. A calendar that quietly
loses a booking is worse than no calendar, so entries with no time and entries
with no agent come back in their own buckets rather than being filtered away.

DATE (2026-06-20) is a Saturday, weekday 5 — the same date the schedule tests
use, so window fixtures read the same way in both files.
"""
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

import main
from app import core, db
from app.models import (Tenant, Service, Agent, AgentService, AgentSchedule,
                        AgentBlock, QueueEntry)


DATE = "2026-06-20"          # Saturday
SATURDAY = 5


def at(hour, minute=0, day=20):
    return datetime(2026, 6, day, hour, minute)


@pytest.fixture
def client():
    return TestClient(main.app)


@pytest.fixture
def hdr(super_token):
    return {"Authorization": f"Bearer {super_token}"}


def test_date_really_is_a_saturday():
    assert datetime.strptime(DATE, "%Y-%m-%d").weekday() == SATURDAY


# ── fixtures ─────────────────────────────────────────────────────────────────
def _tenant(opens=8, closes=17, mode="appointments"):
    with Session(db.engine) as s:
        t = Tenant(business_name="Cal Co", whatsapp_number="27810000000",
                   evolution_instance="i", evolution_api_key="k",
                   evolution_api_url="http://x", mode=mode,
                   queue_opens=opens, queue_closes=closes, advance_days=7)
        s.add(t); s.commit(); s.refresh(t)
        return t


def _service(tenant_id, minutes=30, name=None):
    with Session(db.engine) as s:
        sv = Service(tenant_id=tenant_id, name=name or f"S{minutes}",
                     duration_minutes=minutes)
        s.add(sv); s.commit(); s.refresh(sv)
        return sv.id


def _agent(tenant_id, service_ids=(), name="Nomsa", is_active=True):
    with Session(db.engine) as s:
        a = Agent(tenant_id=tenant_id, name=name, is_active=is_active)
        s.add(a); s.commit(); s.refresh(a)
        for sid in service_ids:
            s.add(AgentService(agent_id=a.id, service_id=sid))
        s.commit()
        return a.id


def _schedule(tenant_id, agent_id, weekday, start_min, end_min):
    with Session(db.engine) as s:
        s.add(AgentSchedule(tenant_id=tenant_id, agent_id=agent_id,
                            weekday=weekday, start_minute=start_min,
                            end_minute=end_min))
        s.commit()


_seq = [0]


@pytest.fixture(autouse=True)
def _reset_seq():
    _seq[0] = 0
    yield


def _entry(tenant_id, service_id, agent_id, *, start, status="Waiting",
           is_fixed=False, slot_end=None, date=DATE, name="Thabo",
           booked_via="whatsapp"):
    _seq[0] += 1
    with Session(db.engine) as s:
        e = QueueEntry(tenant_id=tenant_id, service_id=service_id,
                       agent_id=agent_id, customer_number="27810001111@s.whatsapp.net",
                       customer_name=name, status=status, queue_date=date,
                       estimated_start=start, is_fixed=is_fixed,
                       slot_end=slot_end, booked_via=booked_via,
                       joined_at=at(7) + timedelta(seconds=_seq[0]))
        s.add(e); s.commit(); s.refresh(e)
        return e.id


def _get(client, hdr, tenant_id, date=DATE):
    r = client.get(f"/admin/timeline/{tenant_id}?queue_date={date}", headers=hdr)
    assert r.status_code == 200, r.text
    return r.json()


def _column(day, agent_id):
    return next(c for c in day["agents"] if c["agent_id"] == agent_id)


# =============================================================================
# Shape
# =============================================================================
def test_an_empty_day_still_returns_the_shop_hours(client, hdr):
    """
    An agent working 08:00–17:00 with nothing booked is the most common day a
    calendar is ever opened on, and the axis has to exist before the first
    booking does.
    """
    t = _tenant(); a = _agent(t.id)
    day = _get(client, hdr, t.id)

    assert day["day_start"] == at(8).isoformat()
    assert day["day_end"]   == at(17).isoformat()
    col = _column(day, a)
    assert col["windows"] == [{"start": at(8).isoformat(), "end": at(17).isoformat(),
                              "minutes": 540}]
    assert col["entries"] == []
    assert col["free_minutes"] == 540


def test_a_booking_comes_back_with_its_start_end_and_length(client, hdr):
    t = _tenant(); sv = _service(t.id, 45); a = _agent(t.id, [sv])
    _entry(t.id, sv, a, start=at(9), is_fixed=True, slot_end=at(9, 45))

    col = _column(_get(client, hdr, t.id), a)
    row = col["entries"][0]
    assert row["start"]   == at(9).isoformat()
    assert row["end"]     == at(9, 45).isoformat()
    assert row["minutes"] == 45
    assert row["is_fixed"] is True
    assert row["customer_name"] == "Thabo"
    assert row["service"] == "S45"


def test_the_whatsapp_suffix_is_stripped_from_the_number(client, hdr):
    """Same as the queue list — staff read a phone number, not a JID."""
    t = _tenant(); sv = _service(t.id); a = _agent(t.id, [sv])
    _entry(t.id, sv, a, start=at(9))
    row = _column(_get(client, hdr, t.id), a)["entries"][0]
    assert row["customer_number"] == "27810001111"


def test_a_flexible_entry_is_marked_as_not_fixed(client, hdr):
    """
    The distinction the view exists to show. A queue entry's 10:00 is a
    forecast the next walk-in may move; an appointment's 10:00 is a promise.
    """
    t = _tenant(); sv = _service(t.id, 30); a = _agent(t.id, [sv])
    _entry(t.id, sv, a, start=at(10))
    row = _column(_get(client, hdr, t.id), a)["entries"][0]
    assert row["is_fixed"] is False
    assert row["minutes"] == 30      # derived from the service, not slot_end


def test_a_fixed_entry_keeps_the_length_it_was_booked_at(client, hdr):
    """
    slot_end is frozen at booking. Retiming the service afterwards must not
    silently redraw an appointment somebody was already promised.
    """
    t = _tenant(); sv = _service(t.id, 30); a = _agent(t.id, [sv])
    _entry(t.id, sv, a, start=at(9), is_fixed=True, slot_end=at(10))

    with Session(db.engine) as s:
        svc = s.get(Service, sv); svc.duration_minutes = 90
        s.add(svc); s.commit()

    row = _column(_get(client, hdr, t.id), a)["entries"][0]
    assert row["minutes"] == 60


def test_entries_come_back_in_time_order(client, hdr):
    t = _tenant(); sv = _service(t.id, 30); a = _agent(t.id, [sv])
    _entry(t.id, sv, a, start=at(14), name="Late")
    _entry(t.id, sv, a, start=at(9),  name="Early")
    _entry(t.id, sv, a, start=at(11), name="Middle")

    names = [e["customer_name"] for e in _column(_get(client, hdr, t.id), a)["entries"]]
    assert names == ["Early", "Middle", "Late"]


# =============================================================================
# The axis
# =============================================================================
def test_the_axis_spans_every_agents_hours(client, hdr):
    """
    One shared axis, or two agents' 10:00 would not line up in the grid. An
    early starter and a late finisher between them set the bounds.
    """
    t = _tenant(opens=8, closes=17)
    early = _agent(t.id, name="Early"); late = _agent(t.id, name="Late")
    _schedule(t.id, early, SATURDAY, 7 * 60, 12 * 60)
    _schedule(t.id, late,  SATURDAY, 13 * 60, 19 * 60)

    day = _get(client, hdr, t.id)
    assert day["day_start"] == at(7).isoformat()
    assert day["day_end"]   == at(19).isoformat()


def test_the_axis_stretches_to_cover_an_overrun(client, hdr):
    """
    Work scheduled past closing is exactly what someone opens this view to
    find. Clipping the axis at 17:00 would hide it.
    """
    t = _tenant(opens=8, closes=17); sv = _service(t.id, 60); a = _agent(t.id, [sv])
    _entry(t.id, sv, a, start=at(17, 30), is_fixed=True, slot_end=at(18, 30))

    day = _get(client, hdr, t.id)
    assert day["day_end"] == at(19).isoformat()   # rounded up to the hour


def test_a_ragged_end_rounds_up_to_the_hour(client, hdr):
    t = _tenant(opens=8, closes=17)
    a = _agent(t.id)
    _schedule(t.id, a, SATURDAY, 8 * 60 + 30, 16 * 60 + 20)

    day = _get(client, hdr, t.id)
    assert day["day_start"] == at(8).isoformat()    # floored
    assert day["day_end"]   == at(17).isoformat()   # ceiled


def test_a_day_nobody_works_still_has_a_drawable_axis(client, hdr):
    """
    Every agent off, nothing booked: the grid still needs a height to divide
    by. Falls back to the shop's own hours.
    """
    t = _tenant(opens=9, closes=15); a = _agent(t.id)
    _schedule(t.id, a, (SATURDAY + 1) % 7, 8 * 60, 17 * 60)   # scheduled another day

    day = _get(client, hdr, t.id)
    assert day["day_start"] == at(9).isoformat()
    assert day["day_end"]   == at(15).isoformat()
    assert _column(day, a)["working"] is False
    assert _column(day, a)["windows"] == []


def test_a_zero_length_day_is_widened_rather_than_returned(client, hdr):
    """opens == closes would otherwise hand the grid a zero-height axis."""
    t = _tenant(opens=9, closes=9); _agent(t.id)
    day = _get(client, hdr, t.id)
    assert day["day_start"] == at(9).isoformat()
    assert day["day_end"]   == at(10).isoformat()


# =============================================================================
# What is drawn, and what is not
# =============================================================================
@pytest.mark.parametrize("status", ["Cancelled", "NoShow"])
def test_a_released_slot_is_not_drawn(client, hdr, status):
    """
    A cancelled booking gives its time back. Drawing it would show the day as
    fuller than it is — the one thing a calendar must never do.
    """
    t = _tenant(); sv = _service(t.id, 60); a = _agent(t.id, [sv])
    _entry(t.id, sv, a, start=at(9), is_fixed=True, slot_end=at(10), status=status)

    col = _column(_get(client, hdr, t.id), a)
    assert col["entries"] == []
    assert col["booked_minutes"] == 0
    assert col["free_minutes"]   == 540


def test_a_finished_appointment_stays_on_the_board(client, hdr):
    """The morning is still worth seeing at four in the afternoon."""
    t = _tenant(); sv = _service(t.id, 60); a = _agent(t.id, [sv])
    _entry(t.id, sv, a, start=at(9), is_fixed=True, slot_end=at(10), status="Done")

    col = _column(_get(client, hdr, t.id), a)
    assert [e["status"] for e in col["entries"]] == ["Done"]
    assert col["booked_minutes"] == 60


def test_an_entry_with_no_time_is_reported_not_dropped(client, hdr):
    """
    Nothing should reach this state. If something does, the grid cannot place
    it — so it is named in `unplaced` rather than vanishing from a view staff
    are trusting to be complete.
    """
    t = _tenant(); sv = _service(t.id, 30); a = _agent(t.id, [sv])
    _entry(t.id, sv, a, start=None, name="Nowhere")

    col = _column(_get(client, hdr, t.id), a)
    assert col["entries"] == []
    assert [e["customer_name"] for e in col["unplaced"]] == ["Nowhere"]


def test_an_entry_whose_agent_is_gone_comes_back_orphaned(client, hdr):
    t = _tenant(); sv = _service(t.id, 30); a = _agent(t.id, [sv])
    _entry(t.id, sv, a, start=at(9), name="Adrift")
    with Session(db.engine) as s:
        s.delete(s.get(Agent, a)); s.commit()

    day = _get(client, hdr, t.id)
    assert day["agents"] == []
    assert [e["customer_name"] for e in day["orphaned"]] == ["Adrift"]


def test_another_days_bookings_do_not_leak_in(client, hdr):
    t = _tenant(); sv = _service(t.id, 30); a = _agent(t.id, [sv])
    _entry(t.id, sv, a, start=at(9, 0, 21), date="2026-06-21", name="Tomorrow")
    _entry(t.id, sv, a, start=at(9), name="Today")

    names = [e["customer_name"] for e in _column(_get(client, hdr, t.id), a)["entries"]]
    assert names == ["Today"]


def test_one_tenants_day_never_shows_anothers(client, hdr):
    mine   = _tenant(); theirs = _tenant()
    sv_m = _service(mine.id, 30);  a_m = _agent(mine.id, [sv_m], name="Mine")
    sv_t = _service(theirs.id, 30); a_t = _agent(theirs.id, [sv_t], name="Theirs")
    _entry(mine.id, sv_m, a_m, start=at(9), name="Mine")
    _entry(theirs.id, sv_t, a_t, start=at(9), name="Theirs")

    day = _get(client, hdr, mine.id)
    assert [c["name"] for c in day["agents"]] == ["Mine"]
    assert [e["customer_name"] for e in day["agents"][0]["entries"]] == ["Mine"]


# =============================================================================
# Agents
# =============================================================================
def test_an_agent_switched_off_mid_day_keeps_their_customers(client, hdr):
    """
    Deactivating an agent stops new bookings. It does not undo this morning's
    — hiding the column would hide work that is still happening.
    """
    t = _tenant(); sv = _service(t.id, 30); a = _agent(t.id, [sv], is_active=False)
    _entry(t.id, sv, a, start=at(9), name="Already booked")

    col = _column(_get(client, hdr, t.id), a)
    assert col["is_active"] is False
    assert [e["customer_name"] for e in col["entries"]] == ["Already booked"]


def test_an_inactive_agent_with_an_empty_day_is_not_shown(client, hdr):
    """The other half of the rule — an empty column nobody can book is noise."""
    t = _tenant()
    live = _agent(t.id, name="Live")
    _agent(t.id, name="Retired", is_active=False)

    day = _get(client, hdr, t.id)
    assert [c["agent_id"] for c in day["agents"]] == [live]


def test_a_block_cuts_a_hole_in_the_drawn_hours(client, hdr):
    """The grid draws what the engine schedules into — lunch included."""
    t = _tenant(opens=8, closes=17); a = _agent(t.id)
    with Session(db.engine) as s:
        s.add(AgentBlock(tenant_id=t.id, agent_id=a,
                         starts_at=at(12), ends_at=at(13), reason="Lunch"))
        s.commit()

    col = _column(_get(client, hdr, t.id), a)
    assert [(w["start"], w["end"]) for w in col["windows"]] == [
        (at(8).isoformat(), at(12).isoformat()),
        (at(13).isoformat(), at(17).isoformat()),
    ]
    assert col["working_minutes"] == 480


def test_split_shifts_come_back_as_two_windows(client, hdr):
    t = _tenant(); a = _agent(t.id)
    _schedule(t.id, a, SATURDAY, 8 * 60, 12 * 60)
    _schedule(t.id, a, SATURDAY, 14 * 60, 18 * 60)

    col = _column(_get(client, hdr, t.id), a)
    assert len(col["windows"]) == 2
    assert col["working_minutes"] == 480


# =============================================================================
# Load arithmetic
# =============================================================================
def test_booked_and_free_add_up_to_the_working_day(client, hdr):
    t = _tenant(opens=8, closes=17); sv = _service(t.id, 60); a = _agent(t.id, [sv])
    _entry(t.id, sv, a, start=at(9),  is_fixed=True, slot_end=at(10))
    _entry(t.id, sv, a, start=at(11), is_fixed=True, slot_end=at(12))

    col = _column(_get(client, hdr, t.id), a)
    assert col["working_minutes"] == 540
    assert col["booked_minutes"]  == 120
    assert col["free_minutes"]    == 420


def test_work_outside_the_working_hours_never_makes_free_time_negative(client, hdr):
    """
    A block dropped on top of an existing booking leaves more booked than
    worked. Free time is clamped rather than reported as minus two hours.
    """
    t = _tenant(opens=8, closes=17); sv = _service(t.id, 60); a = _agent(t.id, [sv])
    _entry(t.id, sv, a, start=at(9), is_fixed=True, slot_end=at(10))
    with Session(db.engine) as s:
        s.add(AgentBlock(tenant_id=t.id, agent_id=a,
                         starts_at=at(8), ends_at=at(17), reason="Sick"))
        s.commit()

    col = _column(_get(client, hdr, t.id), a)
    assert col["working_minutes"] == 0
    assert col["booked_minutes"]  == 60
    assert col["free_minutes"]    == 0


def test_load_is_counted_per_agent_not_pooled(client, hdr):
    t = _tenant(opens=8, closes=17); sv = _service(t.id, 60)
    busy = _agent(t.id, [sv], name="Busy"); idle = _agent(t.id, [sv], name="Idle")
    _entry(t.id, sv, busy, start=at(9), is_fixed=True, slot_end=at(10))

    day = _get(client, hdr, t.id)
    assert _column(day, busy)["booked_minutes"] == 60
    assert _column(day, idle)["booked_minutes"] == 0
    assert _column(day, idle)["free_minutes"]   == 540


# =============================================================================
# Access
# =============================================================================
def test_the_day_needs_a_token(client):
    t = _tenant()
    assert client.get(f"/admin/timeline/{t.id}").status_code == 401


def test_a_client_cannot_read_another_businesses_day(client, hdr):
    """Per-tenant auth applies here as it does to every other admin read."""
    mine = _tenant(); theirs = _tenant()
    r = client.post("/admin/users", headers=hdr, json={
        "email": "owner@cal.test", "password": "ownerpass123",
        "tenant_id": mine.id, "is_super": False})
    assert r.status_code in (200, 201), r.text

    login = client.post("/auth/login", json={"email": "owner@cal.test",
                                             "password": "ownerpass123"})
    assert login.status_code == 200, login.text
    own_hdr = {"Authorization": f"Bearer {login.json()['access_token']}"}

    assert client.get(f"/admin/timeline/{mine.id}", headers=own_hdr).status_code == 200
    assert client.get(f"/admin/timeline/{theirs.id}", headers=own_hdr).status_code == 403


def test_an_unknown_tenant_is_a_404(client, hdr):
    assert client.get("/admin/timeline/9999", headers=hdr).status_code == 404


def test_the_date_defaults_to_today(client, hdr, monkeypatch):
    t = _tenant(); sv = _service(t.id, 30); a = _agent(t.id, [sv])
    monkeypatch.setattr(core, "today_str", lambda: DATE)
    _entry(t.id, sv, a, start=at(9), name="Today")

    r = client.get(f"/admin/timeline/{t.id}", headers=hdr)
    assert r.status_code == 200, r.text
    assert r.json()["queue_date"] == DATE
    assert [e["customer_name"] for e in r.json()["agents"][0]["entries"]] == ["Today"]
