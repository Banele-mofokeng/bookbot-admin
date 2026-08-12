"""
Appointment reminders.

Derived from state on every tick, like the queue's 15-minute warning and for the
same reason: no persisted timer, so a restart cannot lose one. The interesting
behaviour is at the edges — what happens when the process was down, when
reminders are switched on halfway through a day that is already booked, and when
a rung comes due at three in the morning.
"""
from datetime import datetime, timedelta

import pytest
from sqlmodel import Session, select

import main  # noqa: F401 — binds the test engine before app modules load
from app import core, db, jobs
from app.models import (Tenant, Service, Agent, NotificationLog, OutboxMessage,
                        QueueEntry)

DATE = "2026-06-22"
CUSTOMER = "27820000001@s.whatsapp.net"


def _at(hour, minute=0, day_offset=0):
    base = datetime.strptime(DATE, "%Y-%m-%d") + timedelta(days=day_offset)
    return base.replace(hour=hour, minute=minute)


def _freeze(monkeypatch, when):
    monkeypatch.setattr(core, "now", lambda: when)


def _seed(offsets="1440,120", opens=8, closes=18, mode="appointments"):
    with Session(db.engine) as s:
        t = Tenant(
            business_name="Test Salon", whatsapp_number="27810000000",
            evolution_instance="i", evolution_api_key="k",
            evolution_api_url="http://x",
            queue_opens=opens, queue_closes=closes, mode=mode,
            reminder_offsets_minutes=offsets, agent_label="Stylist",
        )
        s.add(t); s.commit(); s.refresh(t)
        svc = Service(tenant_id=t.id, name="Cut", duration_minutes=45)
        s.add(svc); s.commit(); s.refresh(svc)
        a = Agent(tenant_id=t.id, name="Nomsa")
        s.add(a); s.commit(); s.refresh(a)
        return t.id, svc.id, a.id


def _appointment(tid, svc, aid, start, status="Waiting", is_fixed=True,
                 number=CUSTOMER):
    with Session(db.engine) as s:
        e = QueueEntry(
            tenant_id=tid, service_id=svc, agent_id=aid,
            customer_number=number, customer_name="Thabo",
            queue_date=start.date().isoformat(), status=status,
            estimated_start=start,
            slot_end=start + timedelta(minutes=45) if is_fixed else None,
            is_fixed=is_fixed,
        )
        s.add(e); s.commit(); s.refresh(e)
        return e.id


def _outbox():
    with Session(db.engine) as s:
        return s.exec(select(OutboxMessage).order_by(OutboxMessage.id)).all()


def _bodies():
    return [m.body for m in _outbox()]


# =============================================================================
# parse_reminder_offsets
# =============================================================================
def test_offsets_parse_largest_first():
    assert core.parse_reminder_offsets("120, 1440, 30") == [1440, 120, 30]


def test_duplicates_collapse():
    assert core.parse_reminder_offsets("120,120") == [120]


@pytest.mark.parametrize("raw", ["", None, "   ", "banana", "0", "-30", ","])
def test_nonsense_switches_reminders_off_rather_than_raising(raw):
    """
    Read on a hot path across every tenant. One bad character in one settings
    field must not stop everybody else's reminders.
    """
    assert core.parse_reminder_offsets(raw) == []


def test_partial_nonsense_keeps_the_good_part():
    assert core.parse_reminder_offsets("1440,banana,120") == [1440, 120]


# =============================================================================
# The rung windows
# =============================================================================
def test_a_rung_closes_when_its_lateness_budget_runs_out():
    """
    The day-ahead rung stays open until T-2h on the ladder alone. Half its lead
    time closes it far sooner — 12 hours — because "tomorrow" stops being true
    long before the next rung is due.
    """
    start = _at(14)
    offset, due_from, due_until = jobs.reminder_rungs([1440, 120], start)[0]

    assert due_from == start - timedelta(minutes=1440)
    assert due_until == due_from + timedelta(hours=12)


def test_a_rung_closes_when_the_next_one_takes_over():
    """
    The other bound. Rungs close together — 120 then 90 — so the next one is
    due at T-90 before the first one's hour of budget is spent.
    """
    start = _at(14)
    offset, due_from, due_until = jobs.reminder_rungs([120, 90], start)[0]

    assert due_from == start - timedelta(minutes=120)
    assert due_until == start - timedelta(minutes=90), \
        "the 90-minute rung should have closed this one, not the budget"


def test_the_last_rung_never_runs_past_the_appointment():
    start = _at(14)
    rungs = jobs.reminder_rungs([120], start)
    assert rungs[-1][2] == min(start, rungs[-1][1] + timedelta(minutes=60))


def test_a_short_rung_keeps_a_floor_of_grace():
    """Half of ten minutes is five, which a five-minute sweep could step over."""
    start = _at(14)
    _, due_from, due_until = jobs.reminder_rungs([10], start)[0]
    assert due_until - due_from >= timedelta(minutes=10) or due_until == start


# =============================================================================
# Firing
# =============================================================================
def test_the_day_ahead_reminder_fires(monkeypatch):
    tid, svc, aid = _seed()
    _appointment(tid, svc, aid, _at(14, day_offset=1))

    _freeze(monkeypatch, _at(14))       # exactly 24 hours before
    assert jobs.reminder_sweep() == 1

    assert "tomorrow" in _bodies()[0]
    assert "14:00" in _bodies()[0]


def test_the_two_hour_reminder_fires(monkeypatch):
    tid, svc, aid = _seed()
    _appointment(tid, svc, aid, _at(14))

    _freeze(monkeypatch, _at(12))
    assert jobs.reminder_sweep() == 1

    assert "in about 2 hours" in _bodies()[0]


def test_nothing_fires_before_the_first_rung(monkeypatch):
    tid, svc, aid = _seed()
    _appointment(tid, svc, aid, _at(14, day_offset=3))

    _freeze(monkeypatch, _at(14))
    assert jobs.reminder_sweep() == 0
    assert _outbox() == []


def test_a_rung_fires_once_however_often_the_sweep_ticks(monkeypatch):
    tid, svc, aid = _seed()
    _appointment(tid, svc, aid, _at(14))

    _freeze(monkeypatch, _at(12))
    jobs.reminder_sweep()
    jobs.reminder_sweep()
    jobs.reminder_sweep()

    assert len(_outbox()) == 1


def test_both_rungs_fire_across_the_day(monkeypatch):
    tid, svc, aid = _seed()
    _appointment(tid, svc, aid, _at(14, day_offset=1))

    _freeze(monkeypatch, _at(14))            # T-24h
    jobs.reminder_sweep()
    _freeze(monkeypatch, _at(12, day_offset=1))   # T-2h
    jobs.reminder_sweep()

    bodies = _bodies()
    assert len(bodies) == 2
    assert "tomorrow" in bodies[0]
    assert "in about 2 hours" in bodies[1]


def test_a_late_tick_still_sends(monkeypatch):
    """
    The point of deriving from state. Nothing ticked at T-2h exactly; the first
    tick after the process came back still sends, late rather than never.
    """
    tid, svc, aid = _seed()
    _appointment(tid, svc, aid, _at(14))

    _freeze(monkeypatch, _at(12, 45))        # 45 min late, inside the 2h rung's hour
    assert jobs.reminder_sweep() == 1


def test_a_rung_missed_entirely_is_not_sent_late(monkeypatch):
    """
    Down for a day. "Your appointment is tomorrow" at T-1h is a lie, and the
    two-hour rung has already said something true. Skipping is the honest
    outcome, not a lost message.
    """
    tid, svc, aid = _seed()
    _appointment(tid, svc, aid, _at(14))

    _freeze(monkeypatch, _at(12, 45))
    jobs.reminder_sweep()

    bodies = _bodies()
    assert len(bodies) == 1
    assert "tomorrow" not in bodies[0]
    assert "in about 2 hours" in bodies[0]


def test_switching_reminders_on_mid_day_does_not_storm(monkeypatch):
    """
    The failure this shape is designed against: enabling reminders with a full
    diary must not send every customer a day-ahead notice at once for
    appointments starting within the hour.
    """
    tid, svc, aid = _seed()
    for hour in (10, 11, 12, 13):
        _appointment(tid, svc, aid, _at(hour),
                     number=f"2782000000{hour}@s.whatsapp.net")

    _freeze(monkeypatch, _at(9, 30))
    jobs.reminder_sweep()

    bodies = _bodies()
    assert all("tomorrow" not in b for b in bodies), \
        "a day-ahead notice went out for an appointment starting today"
    assert len(bodies) == 1, \
        "only the 11:00 is inside its two-hour rung's lateness budget at 09:30"
    assert "in about 2 hours" in bodies[0]


def test_nothing_fires_after_the_appointment_has_started(monkeypatch):
    tid, svc, aid = _seed()
    _appointment(tid, svc, aid, _at(14))

    _freeze(monkeypatch, _at(14, 30))
    assert jobs.reminder_sweep() == 0


def test_a_cancelled_appointment_is_not_reminded(monkeypatch):
    tid, svc, aid = _seed()
    _appointment(tid, svc, aid, _at(14), status="Cancelled")

    _freeze(monkeypatch, _at(12))
    assert jobs.reminder_sweep() == 0


def test_a_customer_already_in_the_chair_is_not_reminded(monkeypatch):
    tid, svc, aid = _seed()
    _appointment(tid, svc, aid, _at(14), status="InService")

    _freeze(monkeypatch, _at(12))
    assert jobs.reminder_sweep() == 0


# =============================================================================
# What it must not touch
# =============================================================================
def test_a_queue_entry_is_never_reminded(monkeypatch):
    """
    A queue entry has no promised time to remind anybody about, and keeps the
    15-minute warning it already had. Nothing changes for a queue tenant.
    """
    tid, svc, aid = _seed(mode="queue")
    _appointment(tid, svc, aid, _at(14), is_fixed=False)

    _freeze(monkeypatch, _at(12))
    assert jobs.reminder_sweep() == 0
    assert _outbox() == []


def test_a_tenant_with_reminders_off_sends_nothing(monkeypatch):
    tid, svc, aid = _seed(offsets="")
    _appointment(tid, svc, aid, _at(14))

    _freeze(monkeypatch, _at(12))
    assert jobs.reminder_sweep() == 0


def test_a_walkin_without_a_phone_number_is_skipped(monkeypatch):
    tid, svc, aid = _seed()
    eid = _appointment(tid, svc, aid, _at(14))
    with Session(db.engine) as s:
        e = s.get(QueueEntry, eid)
        e.booked_via = "walkin"
        e.customer_phone = ""
        s.add(e); s.commit()

    _freeze(monkeypatch, _at(12))
    assert jobs.reminder_sweep() == 0


# =============================================================================
# Quiet hours
# =============================================================================
def test_a_rung_due_overnight_waits_for_opening(monkeypatch):
    """
    Never 03:00. The rung stays due until the next takes over, so it goes out
    when the shop opens instead of being lost.
    """
    tid, svc, aid = _seed(offsets="1440", opens=8, closes=18)
    _appointment(tid, svc, aid, _at(3, day_offset=1))

    _freeze(monkeypatch, _at(3))            # T-24h, but the shop is shut
    assert jobs.reminder_sweep() == 0
    assert _outbox() == []

    _freeze(monkeypatch, _at(8, 5))         # opening, still inside the rung
    assert jobs.reminder_sweep() == 1


# =============================================================================
# Bookkeeping
# =============================================================================
def test_each_rung_is_claimed_under_its_own_rule(monkeypatch):
    tid, svc, aid = _seed()
    eid = _appointment(tid, svc, aid, _at(14, day_offset=1))

    _freeze(monkeypatch, _at(14))
    jobs.reminder_sweep()
    _freeze(monkeypatch, _at(12, day_offset=1))
    jobs.reminder_sweep()

    with Session(db.engine) as s:
        rules = {r.rule for r in s.exec(
            select(NotificationLog).where(NotificationLog.entry_id == eid)).all()}
    assert rules == {"reminder:1440", "reminder:120"}


def test_the_outbox_carries_a_dedupe_key(monkeypatch):
    tid, svc, aid = _seed()
    eid = _appointment(tid, svc, aid, _at(14))

    _freeze(monkeypatch, _at(12))
    jobs.reminder_sweep()

    assert _outbox()[0].dedupe_key == f"reminder:120:{eid}"


def test_two_tenants_do_not_interfere(monkeypatch):
    a_tid, a_svc, a_aid = _seed(offsets="120")
    b_tid, b_svc, b_aid = _seed(offsets="")
    _appointment(a_tid, a_svc, a_aid, _at(14))
    _appointment(b_tid, b_svc, b_aid, _at(14),
                 number="27829999999@s.whatsapp.net")

    _freeze(monkeypatch, _at(12))
    assert jobs.reminder_sweep() == 1


# =============================================================================
# Config
# =============================================================================
def test_reminders_can_be_configured(super_token):
    from fastapi.testclient import TestClient
    client = TestClient(main.app)
    r = client.post("/admin/tenants",
                    headers={"Authorization": f"Bearer {super_token}"},
                    json={"business_name": "Clinic",
                          "whatsapp_number": "27820000099",
                          "evolution_instance": "i", "evolution_api_key": "k",
                          "evolution_api_url": "http://x",
                          "mode": "appointments",
                          "reminder_offsets_minutes": "2880,1440,60"})
    assert r.status_code == 200, r.text
    assert r.json()["reminder_offsets_minutes"] == "2880,1440,60"


@pytest.mark.parametrize("bad", ["banana", "0", "-30", "1440,0",
                                 "60,120,180,240,300", "99999999"])
def test_a_nonsense_ladder_is_refused(super_token, bad):
    """
    The sweep tolerates nonsense so one tenant cannot break the others. The
    dashboard must not, or a typo would silently switch a clinic's reminders
    off until somebody failed to arrive.
    """
    from fastapi.testclient import TestClient
    client = TestClient(main.app)
    r = client.post("/admin/tenants",
                    headers={"Authorization": f"Bearer {super_token}"},
                    json={"business_name": "Clinic",
                          "whatsapp_number": "27820000098",
                          "evolution_instance": "i", "evolution_api_key": "k",
                          "evolution_api_url": "http://x",
                          "mode": "appointments",
                          "reminder_offsets_minutes": bad})
    assert r.status_code == 400, f"{bad!r} was accepted"


def test_reminders_can_be_switched_off_explicitly(super_token):
    from fastapi.testclient import TestClient
    client = TestClient(main.app)
    r = client.post("/admin/tenants",
                    headers={"Authorization": f"Bearer {super_token}"},
                    json={"business_name": "Clinic",
                          "whatsapp_number": "27820000097",
                          "evolution_instance": "i", "evolution_api_key": "k",
                          "evolution_api_url": "http://x",
                          "mode": "appointments",
                          "reminder_offsets_minutes": ""})
    assert r.status_code == 200, r.text
