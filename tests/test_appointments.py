"""
The appointment conversation, driven through the webhook exactly as Evolution
drives it.

A queue customer is told when they will probably be seen. An appointment
customer chooses when they will be seen and is held to it. These tests are
mostly about that promise surviving contact with the rest of the system: a
queue tenant behaving identically, a slot taken between two messages, and a
customer who books twice.
"""
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

import main
from app import core, db, queue_engine
from app.models import (Tenant, Service, Agent, AgentService, OutboxMessage,
                        QueueEntry)

DATE = "2026-06-22"
CUSTOMER = "27820000001@s.whatsapp.net"
BUSINESS = "27810000000"


def _at(hour, minute=0):
    return datetime.strptime(DATE, "%Y-%m-%d").replace(hour=hour, minute=minute)


def _freeze(monkeypatch, when):
    """Freeze time everywhere the flow reads it."""
    monkeypatch.setattr(core, "now", lambda: when)


def _seed(mode="appointments", opens=9, closes=12, duration=45,
          granularity=30, n_agents=1, advance_days=1):
    with Session(db.engine) as s:
        t = Tenant(
            business_name="Test Salon", whatsapp_number=BUSINESS,
            evolution_instance="i", evolution_api_key="k",
            evolution_api_url="http://x",
            queue_opens=opens, queue_closes=closes, advance_days=advance_days,
            mode=mode, slot_granularity_minutes=granularity,
            agent_label="Stylist", service_label="Hair Service",
        )
        s.add(t); s.commit(); s.refresh(t)
        svc = Service(tenant_id=t.id, name="Cut", duration_minutes=duration)
        s.add(svc); s.commit(); s.refresh(svc)
        agent_ids = []
        for i in range(n_agents):
            a = Agent(tenant_id=t.id, name=f"Agent{i}")
            s.add(a); s.commit(); s.refresh(a)
            s.add(AgentService(agent_id=a.id, service_id=svc.id)); s.commit()
            agent_ids.append(a.id)
        return t.id, svc.id, agent_ids


def _say(text, sender=BUSINESS, customer=CUSTOMER, msg_id=None):
    """One inbound WhatsApp message, in Evolution's shape."""
    client = TestClient(main.app)
    return client.post("/webhook", json={
        "event": "messages.upsert",
        "sender": sender,
        "data": {
            "key": {"id": msg_id or f"m{datetime.now().timestamp()}{id(text)}",
                    "remoteJid": customer, "fromMe": False},
            "pushName": "Thabo",
            "message": {"conversation": text},
        },
    })


def _sent(customer=CUSTOMER):
    with Session(db.engine) as s:
        return [m.body for m in s.exec(
            select(OutboxMessage).where(OutboxMessage.to_number == customer)
            .order_by(OutboxMessage.id)).all()]


def _last(customer=CUSTOMER):
    msgs = _sent(customer)
    return msgs[-1] if msgs else ""


def _appointments(tid):
    with Session(db.engine) as s:
        return s.exec(
            select(QueueEntry).where(QueueEntry.tenant_id == tid,
                                     QueueEntry.is_fixed == True)
            .order_by(QueueEntry.id)
        ).all()


def _book_through(fake_redis, monkeypatch, **seed):
    """Walk the whole flow to a confirmed booking. Returns (tid, svc, agents)."""
    _freeze(monkeypatch, _at(8))
    tid, svc, agents = _seed(**seed)
    _say("hi")        # main menu
    _say("1")         # book
    _say("1")         # today
    _say("1")         # the only service
    _say("1")         # first free time
    _say("yes")       # confirm
    return tid, svc, agents


# =============================================================================
# Mode routing
# =============================================================================
def test_a_queue_tenant_is_untouched(fake_redis, monkeypatch):
    """The whole point of a third mode: the other two do not notice."""
    _freeze(monkeypatch, _at(10))
    _seed(mode="queue")

    _say("hi")

    assert "Join the queue" in _last()
    assert "Book an appointment" not in _last()


def test_an_appointment_tenant_gets_the_appointment_menu(fake_redis, monkeypatch):
    _freeze(monkeypatch, _at(8))
    _seed()

    _say("hi")

    assert "Book an appointment" in _last()


# =============================================================================
# Booking
# =============================================================================
def test_a_booking_end_to_end(fake_redis, monkeypatch):
    tid, svc, [aid] = _book_through(fake_redis, monkeypatch)

    rows = _appointments(tid)
    assert len(rows) == 1
    appt = rows[0]
    assert appt.is_fixed is True
    assert appt.estimated_start == _at(9)
    assert appt.slot_end == _at(9, 45)
    assert appt.agent_id == aid
    assert "Appointment confirmed" in _last()


def test_the_times_offered_are_on_the_tenant_grid(fake_redis, monkeypatch):
    _freeze(monkeypatch, _at(8))
    _seed(granularity=30, duration=45, opens=9, closes=12)
    _say("hi"); _say("1"); _say("1"); _say("1")

    offered = _last()

    assert "09:00" in offered
    assert "09:30" in offered
    assert "10:00" in offered
    assert "11:30" not in offered, "a 45-minute service cannot start at 11:30"


def test_a_booked_time_stops_being_offered(fake_redis, monkeypatch):
    tid, svc, [aid] = _book_through(fake_redis, monkeypatch)

    _say("menu"); _say("3")            # cancel is the only way past "already booked"
    _say("menu"); _say("1"); _say("1"); _say("1")
    # Rebook the same 09:00, then start a third booking from another customer.
    _say("1"); _say("yes")
    _say("hi", customer="27820000002@s.whatsapp.net")
    _say("1", customer="27820000002@s.whatsapp.net")
    _say("1", customer="27820000002@s.whatsapp.net")
    _say("1", customer="27820000002@s.whatsapp.net")

    offered = _last("27820000002@s.whatsapp.net")
    assert "09:00" not in offered, "a taken time was offered to somebody else"


def test_the_owner_is_told(fake_redis, monkeypatch):
    _freeze(monkeypatch, _at(8))
    tid, svc, agents = _seed()
    with Session(db.engine) as s:
        t = s.get(Tenant, tid)
        t.owner_number = "0813130871"
        s.add(t); s.commit()

    _say("hi"); _say("1"); _say("1"); _say("1"); _say("1"); _say("yes")

    owner = _sent("27813130871")
    assert any("New Appointment" in m for m in owner)


def test_a_customer_cannot_hold_two_appointments(fake_redis, monkeypatch):
    tid, svc, agents = _book_through(fake_redis, monkeypatch)

    _say("menu")
    _say("1")

    assert "already have an appointment" in _last()
    assert len(_appointments(tid)) == 1


def test_no_preference_picks_an_agent(fake_redis, monkeypatch):
    _freeze(monkeypatch, _at(8))
    tid, svc, agents = _seed(n_agents=2)
    _say("hi"); _say("1"); _say("1"); _say("1")
    assert "No preference" in _last()

    _say("3")          # 2 agents, so 3 is "no preference"
    _say("1"); _say("yes")

    rows = _appointments(tid)
    assert len(rows) == 1
    assert rows[0].agent_id in agents


def test_one_capable_agent_skips_the_choice(fake_redis, monkeypatch):
    _freeze(monkeypatch, _at(8))
    _seed(n_agents=1)

    _say("hi"); _say("1"); _say("1"); _say("1")

    assert "Available times" in _last(), \
        "a single agent should not be presented as a choice"


# =============================================================================
# The promise holds
# =============================================================================
def test_a_walkin_never_moves_a_booked_appointment(fake_redis, monkeypatch):
    """
    The hybrid case. A walk-in arrives after the booking and must be scheduled
    around it, not through it.
    """
    tid, svc, [aid] = _book_through(fake_redis, monkeypatch,
                                    opens=9, closes=17, duration=45)
    appt = _appointments(tid)[0]

    with Session(db.engine) as s:
        s.add(QueueEntry(
            tenant_id=tid, service_id=svc, agent_id=aid,
            customer_number="walkin", customer_name="Off the street",
            queue_date=DATE, status="Waiting", booked_via="walkin"))
        s.commit()
    queue_engine.recalculate_queue(tid, aid, DATE)

    with Session(db.engine) as s:
        still = s.get(QueueEntry, appt.id)
        walkin = s.exec(select(QueueEntry).where(
            QueueEntry.booked_via == "walkin")).first()
    assert still.estimated_start == _at(9), "the appointment was moved"
    assert walkin.estimated_start == _at(9, 45)


def test_a_slot_taken_mid_conversation_is_reported_not_double_booked(
        fake_redis, monkeypatch):
    """
    Two customers reach the confirm step on the same time. The second is told
    plainly and shown what is left — never silently given a clashing booking,
    and never handed a different time they did not choose.
    """
    _freeze(monkeypatch, _at(8))
    tid, svc, [aid] = _seed()
    other = "27820000002@s.whatsapp.net"

    # Both get as far as looking at 09:00.
    _say("hi"); _say("1"); _say("1"); _say("1"); _say("1")
    _say("hi", customer=other); _say("1", customer=other)
    _say("1", customer=other); _say("1", customer=other)
    _say("1", customer=other)

    _say("yes")                       # first confirms
    _say("yes", customer=other)       # second confirms the same time

    rows = _appointments(tid)
    assert len(rows) == 1, "two customers were booked into one slot"
    assert "just took that time" in "".join(_sent(other))


# =============================================================================
# Managing an appointment
# =============================================================================
def test_status_reports_the_booking(fake_redis, monkeypatch):
    _book_through(fake_redis, monkeypatch)

    _say("menu"); _say("2")

    assert "Your appointment" in _last()
    assert "09:00" in _last()


def test_status_when_there_is_nothing_booked(fake_redis, monkeypatch):
    _freeze(monkeypatch, _at(8))
    _seed()

    _say("hi"); _say("2")

    assert "don't have an appointment" in _last()


def test_cancelling_frees_the_slot(fake_redis, monkeypatch):
    tid, svc, agents = _book_through(fake_redis, monkeypatch)

    _say("menu"); _say("3")

    assert "cancelled" in _last().lower()
    rows = _appointments(tid)
    assert rows[0].status == "Cancelled"
    assert rows[0].closed_by == "customer", \
        "a customer cancelling must not be reported as a no-show"


def test_a_cancelled_slot_can_be_sold_again(fake_redis, monkeypatch):
    tid, svc, agents = _book_through(fake_redis, monkeypatch)
    _say("menu"); _say("3")

    _say("menu"); _say("1"); _say("1"); _say("1"); _say("1"); _say("yes")

    live = [a for a in _appointments(tid) if a.status == "Waiting"]
    assert len(live) == 1
    assert live[0].estimated_start == _at(9)


# =============================================================================
# Navigation
# =============================================================================
def test_zero_goes_back_to_services_when_one_agent(fake_redis, monkeypatch):
    """
    A service only one person can do skips the agent screen. Stepping *0* back
    to that screen would re-skip it and land on the time list again — a reply
    that visibly does nothing, with no way back out of the flow.
    """
    _freeze(monkeypatch, _at(8))
    _seed(n_agents=1)
    _say("hi"); _say("1"); _say("1"); _say("1")
    assert "Available times" in _last()

    _say("0")

    assert "which hair service" in _last().lower(), \
        "0 bounced back to the time list — the customer is stuck"


def test_zero_goes_back_to_the_agent_menu_when_there_was_one(fake_redis, monkeypatch):
    _freeze(monkeypatch, _at(8))
    _seed(n_agents=2)
    _say("hi"); _say("1"); _say("1"); _say("1")
    assert "preferred stylist" in _last().lower()
    _say("1")
    assert "Available times" in _last()

    _say("0")

    assert "preferred stylist" in _last().lower()


def test_paging_through_days(fake_redis, monkeypatch):
    _freeze(monkeypatch, _at(8))
    _seed(advance_days=13)

    _say("hi"); _say("1")
    first_page = _last()
    _say("more")
    second_page = _last()

    assert first_page != second_page
    assert "more" in first_page, "a fortnight of days must offer a next page"


def test_a_greeting_mid_flow_returns_to_the_menu(fake_redis, monkeypatch):
    _freeze(monkeypatch, _at(8))
    _seed()
    _say("hi"); _say("1"); _say("1")

    _say("menu")

    assert "Book an appointment" in _last()


def test_nonsense_at_the_time_list_reprompts(fake_redis, monkeypatch):
    _freeze(monkeypatch, _at(8))
    tid, _, _ = _seed()
    _say("hi"); _say("1"); _say("1"); _say("1")

    _say("banana")

    assert "Please reply with a number" in _last()
    assert _appointments(tid) == []


def test_a_day_with_nothing_free_says_so(fake_redis, monkeypatch):
    """Closed before the working day starts, so today has no times at all."""
    _freeze(monkeypatch, _at(13))
    _seed(opens=9, closes=12)

    _say("hi"); _say("1"); _say("1"); _say("1")

    assert "nothing free" in _last() or "no times left" in _last()


# =============================================================================
# Config
# =============================================================================
def test_appointments_is_an_accepted_mode(super_token):
    client = TestClient(main.app)
    r = client.post("/admin/tenants",
                    headers={"Authorization": f"Bearer {super_token}"},
                    json={"business_name": "Clinic",
                          "whatsapp_number": "27820000099",
                          "evolution_instance": "i", "evolution_api_key": "k",
                          "evolution_api_url": "http://x",
                          "mode": "appointments",
                          "slot_granularity_minutes": 15})
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "appointments"
    assert r.json()["slot_granularity_minutes"] == 15


@pytest.mark.parametrize("bad", [0, -30, 999])
def test_a_nonsense_grid_is_refused(super_token, bad):
    """
    A zero granularity makes the slot engine offer nothing at all, silently,
    for as long as it takes somebody to notice.
    """
    client = TestClient(main.app)
    r = client.post("/admin/tenants",
                    headers={"Authorization": f"Bearer {super_token}"},
                    json={"business_name": "Clinic",
                          "whatsapp_number": "27820000098",
                          "evolution_instance": "i", "evolution_api_key": "k",
                          "evolution_api_url": "http://x",
                          "mode": "appointments",
                          "slot_granularity_minutes": bad})
    assert r.status_code == 400


def test_an_unknown_mode_is_still_refused(super_token):
    client = TestClient(main.app)
    r = client.post("/admin/tenants",
                    headers={"Authorization": f"Bearer {super_token}"},
                    json={"business_name": "X", "whatsapp_number": "27820000097",
                          "evolution_instance": "i", "evolution_api_key": "k",
                          "evolution_api_url": "http://x", "mode": "diary"})
    assert r.status_code == 400
