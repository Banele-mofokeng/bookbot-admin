"""
The appointment conversation: pick a day, a service, a person, and a time.

Runs for tenants with mode == "appointments". The queue flow in webhook.py and
the ordering flow in orders_flow.py are untouched by anything here — the three
share the tenant, the session store and the outbox, and nothing else.

The difference from the queue flow is one sentence: a queue customer is told
when they will probably be seen, an appointment customer chooses when they will
be seen and is held to it. Everything below follows from that. The times on
offer come from the slot engine, the booking goes through reserve_appointment
so it cannot collide, and nothing recalculates it afterwards.

Every state name is prefixed "apt_" so a tenant that switches modes mid-session
can never land in a queue or ordering state.
"""
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlmodel import Session, select

from app import core, queue_engine
from app.core import format_eta, normalize_number
from app.db import engine
from app.messaging import send_text
from app.models import Agent, QueueEntry, Service, Tenant
from app.sessions import clear_session, set_session

DIGITS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

# Words that take a customer back to the top from wherever they are.
GREETINGS = {"menu", "start", "hi", "hello", "hey", "book", "booking", "howzit"}

# A numbered list on WhatsApp stops being readable somewhere around ten. Both
# the day list and the time list page rather than growing — a clinic booking
# six weeks out would otherwise send a wall of forty numbered dates.
DAYS_PER_PAGE  = 7
SLOTS_PER_PAGE = 8

# Nobody picks a date sixty days out from a numbered list, whatever
# advance_days says. It bounds the session payload too.
MAX_ADVANCE_DAYS = 60


def _digit(i: int) -> str:
    return DIGITS[i] if i < len(DIGITS) else f"*{i + 1}.*"


def _parse_choice(text: str, count: int) -> Optional[int]:
    """A reply that is a valid 1-based position, as a 0-based index."""
    if not text.isdigit():
        return None
    n = int(text)
    return n - 1 if 1 <= n <= count else None


def _day_label(iso: str) -> str:
    d = datetime.strptime(iso, "%Y-%m-%d").date()
    today = core.now().date()
    label = d.strftime("%a %d %b")
    if d == today:
        return f"{label} _(today)_"
    if d == today + timedelta(days=1):
        return f"{label} _(tomorrow)_"
    return label


def _page(items: List, page: int, per_page: int) -> List:
    return items[page * per_page:(page + 1) * per_page]


# =============================================================================
# MESSAGES
# =============================================================================

def send_appointment_main_menu(tenant: Tenant, number: str):
    send_text(tenant, number,
        f"*Welcome to {tenant.business_name}* 👋\n\n"
        f"What would you like to do?\n\n"
        f"1️⃣ Book an appointment\n"
        f"2️⃣ My appointment\n"
        f"3️⃣ Cancel my appointment\n\n"
        f"Reply with *1*, *2*, or *3*"
    )


def _send_paged(tenant: Tenant, number: str, title: str, labels: List[str],
                page: int, per_page: int, total: int, footer: str):
    """One numbered page, with 'more' only when there is genuinely more."""
    lines = [f"{_digit(i)} {label}" for i, label in enumerate(labels)]
    nav = []
    if (page + 1) * per_page < total:
        nav.append("*more* for later options")
    if page > 0:
        nav.append("*back* for earlier ones")
    nav.append("*0* to go back")
    send_text(tenant, number,
        f"{title}\n\n" + "\n".join(lines) + f"\n\n{footer}\n" + " · ".join(nav))


# =============================================================================
# FLOW
# =============================================================================

def handle_message(tenant: Tenant, customer_num: str, customer_name: str,
                   raw_text: str, text: str, sess: Dict[str, Any],
                   state: str) -> Dict[str, str]:
    """
    One inbound WhatsApp message for an appointment tenant.

    Same contract as the queue and ordering handlers: the caller has resolved
    the tenant, loaded the session and lowercased the text.
    """
    # ── GLOBAL TRIGGERS ───────────────────────────────────────────────────
    # Whole words only, never substrings — "hi" lives inside plenty of ordinary
    # replies and must not throw a half-finished booking away.
    words = text.split()
    if words and (text in GREETINGS or words[0] in GREETINGS):
        return _show_main_menu(tenant, customer_num)

    # ── BACK ──────────────────────────────────────────────────────────────
    if text == "0":
        if state == "apt_service":
            return _ask_date(tenant, customer_num)
        if state == "apt_agent":
            return _ask_service(tenant, customer_num, sess["apt_date"])
        if state == "apt_slot":
            # Only step back to the agent menu if there was one. A service
            # only one person can do skips that screen, and sending them back
            # to it would re-skip it and land them on the time list again —
            # a *0* that visibly does nothing, with no way out of the flow.
            if sess.get("agent_choice"):
                return _ask_agent(tenant, customer_num, sess["apt_date"],
                                  sess["service_id"])
            return _ask_service(tenant, customer_num, sess["apt_date"])
        if state == "apt_confirm":
            return _ask_slot(tenant, customer_num, sess["apt_date"],
                             sess["service_id"], sess.get("agent_id"),
                             agent_choice=sess.get("agent_choice", False))
        return _show_main_menu(tenant, customer_num)

    # ── PAGING ────────────────────────────────────────────────────────────
    if text in ("more", "back") and state in ("apt_date", "apt_slot"):
        step = 1 if text == "more" else -1
        page = max(0, sess.get("page", 0) + step)
        if state == "apt_date":
            return _ask_date(tenant, customer_num, page=page)
        return _ask_slot(tenant, customer_num, sess["apt_date"],
                         sess["service_id"], sess.get("agent_id"), page=page,
                         agent_choice=sess.get("agent_choice", False))

    # ── MAIN MENU ─────────────────────────────────────────────────────────
    if state in ("idle", "apt_menu"):
        if text == "1":
            return _start_booking(tenant, customer_num)
        if text == "2":
            return _report_appointment(tenant, customer_num)
        if text == "3":
            return _cancel_appointment(tenant, customer_num)
        return _show_main_menu(tenant, customer_num)

    # ── DAY ───────────────────────────────────────────────────────────────
    if state == "apt_date":
        shown = _page(sess.get("dates", []), sess.get("page", 0), DAYS_PER_PAGE)
        idx = _parse_choice(text, len(shown))
        if idx is None:
            send_text(tenant, customer_num,
                f"Please reply with a number between *1* and *{len(shown)}*, "
                f"or *0* to go back.")
            return {"status": "success"}
        return _ask_service(tenant, customer_num, shown[idx])

    # ── SERVICE ───────────────────────────────────────────────────────────
    if state == "apt_service":
        service_ids = sess.get("service_ids", [])
        idx = _parse_choice(text, len(service_ids))
        if idx is None:
            send_text(tenant, customer_num,
                f"Please reply with a number between *1* and *{len(service_ids)}*, "
                f"or *0* to go back.")
            return {"status": "success"}
        return _ask_agent(tenant, customer_num, sess["apt_date"], service_ids[idx])

    # ── PERSON ────────────────────────────────────────────────────────────
    if state == "apt_agent":
        agent_ids = sess.get("agent_ids", [])
        no_pref   = len(agent_ids) + 1
        if not text.isdigit():
            send_text(tenant, customer_num, "Please reply with a number, or *0* to go back.")
            return {"status": "success"}
        choice = int(text)
        if choice == no_pref:
            return _ask_slot(tenant, customer_num, sess["apt_date"],
                             sess["service_id"], None, agent_choice=True)
        idx = _parse_choice(text, len(agent_ids))
        if idx is None:
            send_text(tenant, customer_num,
                f"Please reply with a number between *1* and *{no_pref}*, "
                f"or *0* to go back.")
            return {"status": "success"}
        return _ask_slot(tenant, customer_num, sess["apt_date"],
                         sess["service_id"], agent_ids[idx], agent_choice=True)

    # ── TIME ──────────────────────────────────────────────────────────────
    if state == "apt_slot":
        shown = _page(sess.get("slots", []), sess.get("page", 0), SLOTS_PER_PAGE)
        idx = _parse_choice(text, len(shown))
        if idx is None:
            send_text(tenant, customer_num,
                f"Please reply with a number between *1* and *{len(shown)}*, "
                f"or *0* to go back.")
            return {"status": "success"}
        return _ask_confirm(tenant, customer_num, sess, shown[idx])

    # ── CONFIRM ───────────────────────────────────────────────────────────
    if state == "apt_confirm":
        if text not in ("yes", "y", "confirm", "ok"):
            send_text(tenant, customer_num,
                "Reply *yes* to confirm the appointment, or *0* to go back.")
            return {"status": "success"}
        return _book(tenant, customer_num, customer_name, sess)

    # ── FALLBACK ──────────────────────────────────────────────────────────
    return _show_main_menu(tenant, customer_num)


# =============================================================================
# STEPS
# =============================================================================

def _show_main_menu(tenant: Tenant, customer_num: str) -> Dict[str, str]:
    set_session(tenant.id, customer_num, {"state": "apt_menu"})
    send_appointment_main_menu(tenant, customer_num)
    return {"status": "success"}


def _start_booking(tenant: Tenant, customer_num: str) -> Dict[str, str]:
    existing = _open_appointment(tenant.id, customer_num)
    if existing:
        # Said before they walk the whole flow again, not after. Double-booking
        # one customer wastes a slot the shop could have sold.
        send_text(tenant, customer_num,
            f"You already have an appointment with us:\n\n"
            f"{_describe(tenant, existing)}\n\n"
            f"Reply *3* to cancel it first, or *menu* to go back."
        )
        return {"status": "success"}
    return _ask_date(tenant, customer_num)


def _ask_date(tenant: Tenant, customer_num: str, page: int = 0) -> Dict[str, str]:
    today = core.now().date()
    span  = max(0, min(tenant.advance_days, MAX_ADVANCE_DAYS))
    dates = [(today + timedelta(days=d)).isoformat() for d in range(span + 1)]

    shown = _page(dates, page, DAYS_PER_PAGE)
    if not shown:
        # Paged past the end. Land them back on the first page rather than on
        # an empty list with no way forward.
        return _ask_date(tenant, customer_num, page=0)

    set_session(tenant.id, customer_num, {
        "state": "apt_date", "dates": dates, "page": page,
    })
    _send_paged(tenant, customer_num, "*Which day suits you?* 📅",
                [_day_label(d) for d in shown], page, DAYS_PER_PAGE, len(dates),
                "Reply with the *number* of the day.")
    return {"status": "success"}


def _ask_service(tenant: Tenant, customer_num: str, apt_date: str) -> Dict[str, str]:
    with Session(engine) as s:
        services = s.exec(
            select(Service).where(Service.tenant_id == tenant.id,
                                  Service.is_active == True)
        ).all()
        rows = [(sv.id, sv.name, sv.duration_minutes) for sv in services]

    if not rows:
        send_text(tenant, customer_num,
            f"No {tenant.service_label.lower()}s are set up yet — "
            f"please contact {tenant.business_name} directly.")
        clear_session(tenant.id, customer_num)
        return {"status": "success"}

    set_session(tenant.id, customer_num, {
        "state": "apt_service", "apt_date": apt_date,
        "service_ids": [r[0] for r in rows],
    })
    lines = [f"{_digit(i)} {name} ({core.format_duration(mins)})"
             for i, (_, name, mins) in enumerate(rows)]
    send_text(tenant, customer_num,
        f"*Which {tenant.service_label.lower()} do you need?* 💼\n\n"
        + "\n".join(lines)
        + "\n\nReply with the *number*, or *0* to go back."
    )
    return {"status": "success"}


def _ask_agent(tenant: Tenant, customer_num: str, apt_date: str,
               service_id: int) -> Dict[str, str]:
    duration = _duration_of(service_id)
    by_agent = queue_engine.free_slots_by_agent(
        tenant, service_id, apt_date, duration)

    if not by_agent:
        send_text(tenant, customer_num,
            f"Sorry, there's nothing free on "
            f"{_day_label(apt_date)} for that {tenant.service_label.lower()}.\n\n"
            f"Reply *0* to pick another day."
        )
        # Left in apt_service so *0* goes back to the day list, not the top.
        set_session(tenant.id, customer_num, {
            "state": "apt_service", "apt_date": apt_date,
            "service_ids": [service_id],
        })
        return {"status": "success"}

    with Session(engine) as s:
        agents = s.exec(
            select(Agent).where(Agent.id.in_(list(by_agent.keys())))
        ).all()
        rows = [(a.id, a.name) for a in agents]
    rows.sort(key=lambda r: r[1])

    # One person who can do it is not a choice worth making them read.
    if len(rows) == 1:
        return _ask_slot(tenant, customer_num, apt_date, service_id, rows[0][0])

    set_session(tenant.id, customer_num, {
        "state": "apt_agent", "apt_date": apt_date, "service_id": service_id,
        "agent_ids": [r[0] for r in rows],
    })
    lines = [f"{_digit(i)} {name}" for i, (_, name) in enumerate(rows)]
    lines.append(f"{_digit(len(rows))} No preference _(earliest available)_")
    send_text(tenant, customer_num,
        f"*Do you have a preferred {tenant.agent_label.lower()}?* 👤\n\n"
        + "\n".join(lines)
        + "\n\nReply with a number, or *0* to go back."
    )
    return {"status": "success"}


def _ask_slot(tenant: Tenant, customer_num: str, apt_date: str, service_id: int,
              agent_id: Optional[int], page: int = 0,
              agent_choice: bool = False) -> Dict[str, str]:
    """
    `agent_choice` records whether the customer was actually offered a choice
    of person. It is what *0* from here steps back to — see the back handler.
    """
    duration = _duration_of(service_id)

    if agent_id:
        starts = queue_engine.get_free_slots(tenant, agent_id, apt_date, duration)
    else:
        # No preference: offer every time anybody can do, once each. Which
        # agent takes it is settled at booking, against live availability.
        by_agent = queue_engine.free_slots_by_agent(
            tenant, service_id, apt_date, duration)
        starts = sorted({sl for slots in by_agent.values() for sl in slots})

    if not starts:
        send_text(tenant, customer_num,
            f"Sorry, there are no times left on {_day_label(apt_date)}.\n\n"
            f"Reply *0* to pick another day."
        )
        set_session(tenant.id, customer_num, {
            "state": "apt_service", "apt_date": apt_date,
            "service_ids": [service_id],
        })
        return {"status": "success"}

    slots = [sl.isoformat() for sl in starts]
    shown = _page(slots, page, SLOTS_PER_PAGE)
    if not shown:
        return _ask_slot(tenant, customer_num, apt_date, service_id, agent_id,
                         page=0, agent_choice=agent_choice)

    set_session(tenant.id, customer_num, {
        "state": "apt_slot", "apt_date": apt_date, "service_id": service_id,
        "agent_id": agent_id, "slots": slots, "page": page,
        "agent_choice": agent_choice,
    })
    _send_paged(tenant, customer_num,
                f"*Available times — {_day_label(apt_date)}* ⏰",
                [format_eta(datetime.fromisoformat(sl)) for sl in shown],
                page, SLOTS_PER_PAGE, len(slots),
                "Reply with the *number* of your time.")
    return {"status": "success"}


def _ask_confirm(tenant: Tenant, customer_num: str, sess: Dict[str, Any],
                 slot_iso: str) -> Dict[str, str]:
    service_id = sess["service_id"]
    apt_date   = sess["apt_date"]
    agent_id   = sess.get("agent_id")

    with Session(engine) as s:
        service = s.get(Service, service_id)
        agent   = s.get(Agent, agent_id) if agent_id else None

    set_session(tenant.id, customer_num, {
        "state": "apt_confirm", "apt_date": apt_date,
        "service_id": service_id, "agent_id": agent_id, "slot": slot_iso,
        "agent_choice": sess.get("agent_choice", False),
    })
    who = f"\n👤 {tenant.agent_label}: {agent.name}" if agent else ""
    send_text(tenant, customer_num,
        f"*Confirm your appointment* 📋\n\n"
        f"📅 {_day_label(apt_date)}\n"
        f"⏰ {format_eta(datetime.fromisoformat(slot_iso))}\n"
        f"💼 {tenant.service_label}: {service.name if service else ''}"
        f"{who}\n\n"
        f"Reply *yes* to confirm, or *0* to pick a different time."
    )
    return {"status": "success"}


def _book(tenant: Tenant, customer_num: str, customer_name: str,
          sess: Dict[str, Any]) -> Dict[str, str]:
    apt_date   = sess["apt_date"]
    service_id = sess["service_id"]
    start      = datetime.fromisoformat(sess["slot"])
    duration   = _duration_of(service_id)
    agent_id   = sess.get("agent_id")

    if not agent_id:
        agent_id = queue_engine.pick_agent_for_slot(
            tenant, service_id, apt_date, start, duration)
    if not agent_id:
        return _slot_gone(tenant, customer_num, sess)

    entry_id = queue_engine.reserve_appointment(
        tenant, agent_id, service_id, apt_date, start,
        customer_num, customer_name, duration_minutes=duration)
    if not entry_id:
        # Somebody confirmed the same time first. There is no version of this
        # worth papering over — say so and show what is actually left.
        return _slot_gone(tenant, customer_num, sess)

    clear_session(tenant.id, customer_num)
    with Session(engine) as s:
        entry   = s.get(QueueEntry, entry_id)
        agent   = s.get(Agent, agent_id)
        service = s.get(Service, service_id)
        agent_name   = agent.name if agent else ""
        service_name = service.name if service else ""

    send_text(tenant, customer_num,
        f"✅ *Appointment confirmed!*\n\n"
        f"📅 {_day_label(apt_date)}\n"
        f"⏰ {format_eta(entry.estimated_start)} – {format_eta(entry.slot_end)}\n"
        f"💼 {tenant.service_label}: {service_name}\n"
        f"👤 {tenant.agent_label}: {agent_name}\n\n"
        f"We'll remind you before the day.\n"
        f"Reply *2* anytime to check it, or *3* to cancel."
    )

    if tenant.owner_number:
        send_text(tenant, normalize_number(tenant.owner_number),
            f"📅 *New Appointment*\n\n"
            f"👤 {customer_name}\n"
            f"💼 {service_name}\n"
            f"👷 {agent_name}\n"
            f"🕐 {_day_label(apt_date)} at {format_eta(entry.estimated_start)}"
        )
    return {"status": "success"}


def _slot_gone(tenant: Tenant, customer_num: str,
               sess: Dict[str, Any]) -> Dict[str, str]:
    send_text(tenant, customer_num,
        "😞 Sorry — somebody just took that time.\n\nHere's what's still free:")
    return _ask_slot(tenant, customer_num, sess["apt_date"],
                     sess["service_id"], sess.get("agent_id"),
                     agent_choice=sess.get("agent_choice", False))


def _report_appointment(tenant: Tenant, customer_num: str) -> Dict[str, str]:
    entry = _open_appointment(tenant.id, customer_num)
    if not entry:
        send_text(tenant, customer_num,
            "You don't have an appointment booked with us.\n\n"
            "Reply *1* to book one. 📅")
        return {"status": "success"}
    send_text(tenant, customer_num,
        f"*Your appointment* 📋\n\n{_describe(tenant, entry)}")
    return {"status": "success"}


def _cancel_appointment(tenant: Tenant, customer_num: str) -> Dict[str, str]:
    entry = _open_appointment(tenant.id, customer_num)
    if not entry:
        send_text(tenant, customer_num, "You have no appointment to cancel.")
        return {"status": "success"}

    entry_id, apt_date = entry.id, entry.queue_date
    described = _describe(tenant, entry)

    touched = queue_engine.cancel_party(tenant.id, entry_id)
    for aid in touched:
        # The freed time is now available to the flexible queue behind it.
        queue_engine.recalculate_queue(tenant.id, aid, apt_date)

    clear_session(tenant.id, customer_num)
    send_text(tenant, customer_num,
        f"❌ Appointment cancelled.\n\n{described}\n\n"
        f"Reply *menu* to book another.")
    if tenant.owner_number:
        send_text(tenant, normalize_number(tenant.owner_number),
            f"❌ *Appointment Cancelled*\n\n{described}")
    return {"status": "success"}


# =============================================================================
# HELPERS
# =============================================================================

def _duration_of(service_id: int) -> int:
    with Session(engine) as s:
        svc = s.get(Service, service_id)
    return svc.duration_minutes if svc else 60


def _open_appointment(tenant_id: int, customer_num: str) -> Optional[QueueEntry]:
    """This customer's next booked appointment, today or later."""
    with Session(engine) as s:
        return s.exec(
            select(QueueEntry).where(
                QueueEntry.tenant_id       == tenant_id,
                QueueEntry.customer_number == customer_num,
                QueueEntry.queue_date      >= core.today_str(),
                QueueEntry.is_fixed        == True,
                QueueEntry.status.in_(["Waiting", "InService"]),
            ).order_by(QueueEntry.estimated_start)
        ).first()


def _describe(tenant: Tenant, entry: QueueEntry) -> str:
    with Session(engine) as s:
        agent   = s.get(Agent, entry.agent_id)
        service = s.get(Service, entry.service_id)
    return (
        f"📅 {_day_label(entry.queue_date)}\n"
        f"⏰ {format_eta(entry.estimated_start)} – {format_eta(entry.slot_end)}\n"
        f"💼 {tenant.service_label}: {service.name if service else ''}\n"
        f"👤 {tenant.agent_label}: {agent.name if agent else 'TBD'}"
    )
