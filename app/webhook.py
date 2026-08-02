"""The customer-facing WhatsApp conversation. One state machine, driven by
the Redis session, that books, reschedules and cancels queue entries.
"""
import json
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
from fastapi import APIRouter, Request, Depends
from sqlmodel import Session, select

from app import config, core, orders_flow
from app.auth import verify_webhook_secret
from app.core import format_duration, format_eta, normalize_number
from app.db import engine
from app.models import (Tenant, Service, Agent, AgentService, QueueEntry)
from app.messaging import (send_text, send_main_menu, send_service_menu,
                           send_agent_menu, send_date_menu, get_agent_status,
                           queue_is_open_today)
from app.queue_engine import (assign_agent, calculate_estimated_start,
                              cancel_party, find_walkin_insert_joined_at,
                              get_agent_backlog_minutes, parse_arrival_time,
                              recalculate_queue)
from app.sessions import get_session, set_session, clear_session, booking_lock
from app.tenants import get_tenant_by_number

router = APIRouter()

# =============================================================================
# 9. WEBHOOK HANDLER
# =============================================================================

def _do_assign(tenant, customer_num: str, customer_name: str,
               assigned_agent_id: int, service_id: int,
               queue_date: str, sess: dict,
               include_parent: bool = True,
               children_names: list = None,
               earliest_arrival: Optional[datetime] = None,
               customer_chose_agent: bool = True):
    """
    Saves queue entries and sends confirmation.
    include_parent=False means only child entries are created (parent is just escorting).
    children_names is a list of child name strings; each gets its own QueueEntry.
    Shared by single-agent auto-assign and manual agent pick paths.

    customer_chose_agent=False means assigned_agent_id was auto-picked rather
    than requested by name, so it may be re-resolved under the booking lock.
    Defaults True — never silently override a choice the customer made.
    """
    if children_names is None:
        children_names = []

    # Serialise assignment + insert. Without the lock two customers confirming
    # at the same instant both read the pre-insert backlogs and land on the
    # same agent.
    with booking_lock(tenant.id, queue_date):
        # Re-resolve the agent inside the lock unless the customer explicitly
        # picked one. The auto-pick made before the arrival-time prompt is a
        # whole round trip stale, and two customers prompted at the same moment
        # were handed the same agent. Re-resolving can only land on an agent
        # whose backlog is <= the one already quoted, so the ETA the customer
        # was shown still holds.
        if not customer_chose_agent:
            assigned_agent_id = assign_agent(
                tenant, service_id, None, queue_date
            ) or assigned_agent_id

        backlog  = get_agent_backlog_minutes(assigned_agent_id, tenant.id, queue_date)
        eta      = calculate_estimated_start(tenant, assigned_agent_id, queue_date, backlog, earliest_arrival)

        print(f"\U0001f4be Saving entry | tenant={tenant.id} service={service_id} agent={assigned_agent_id} date={queue_date} parent={include_parent} children={children_names}")

        child_entries = []
        parent_entry  = None
        agent         = None
        saved_ids     = []  # track all saved entry IDs for rollback
        try:
            with Session(engine) as s:
                total_waiting = len(s.exec(
                    select(QueueEntry).where(
                        QueueEntry.tenant_id  == tenant.id,
                        QueueEntry.queue_date == queue_date,
                        QueueEntry.status     == "Waiting"
                    )
                ).all())
                next_position = total_waiting + 1

                if include_parent:
                    parent_entry = QueueEntry(
                        tenant_id          = tenant.id,
                        service_id         = service_id,
                        agent_id           = assigned_agent_id,
                        customer_number    = customer_num,
                        customer_name      = customer_name,
                        queue_date         = queue_date,
                        estimated_start    = eta,
                        earliest_arrival   = earliest_arrival,
                        position           = next_position,
                        booked_via         = "whatsapp"
                    )
                    # If this customer finishes before a later appointment is even
                    # due, slot them into that idle gap instead of the queue tail.
                    insert_at = find_walkin_insert_joined_at(
                        assigned_agent_id, tenant.id, tenant, queue_date,
                        service_id, new_arrival=earliest_arrival
                    )
                    if insert_at:
                        parent_entry.joined_at = insert_at
                    s.add(parent_entry)
                    s.commit()
                    s.refresh(parent_entry)
                    saved_ids.append(parent_entry.id)
                    next_position += 1

                # Party root for linkage. With a parent it's the parent; for a
                # children-only booking the first child becomes the root and the
                # rest link to it, so cancel_party can find the whole party.
                party_root_id = parent_entry.id if parent_entry else None
                for child_name in children_names:
                    # Assign each child independently so free agents are used and
                    # backlogs are accurate after each commit
                    child_agent_id = assign_agent(tenant, service_id, None, queue_date) or assigned_agent_id
                    child_backlog  = get_agent_backlog_minutes(child_agent_id, tenant.id, queue_date)
                    child_eta      = calculate_estimated_start(tenant, child_agent_id, queue_date, child_backlog, earliest_arrival)
                    child_entry = QueueEntry(
                        tenant_id          = tenant.id,
                        service_id         = service_id,
                        agent_id           = child_agent_id,
                        customer_number    = customer_num,
                        customer_name      = child_name,
                        queue_date         = queue_date,
                        estimated_start    = child_eta,
                        position           = next_position,
                        booked_via         = "whatsapp",
                        parent_entry_id    = party_root_id,
                        earliest_arrival   = earliest_arrival,
                    )
                    child_insert_at = find_walkin_insert_joined_at(
                        child_agent_id, tenant.id, tenant, queue_date,
                        service_id, new_arrival=earliest_arrival
                    )
                    if child_insert_at:
                        child_entry.joined_at = child_insert_at
                    s.add(child_entry)
                    s.commit()  # commit before next child so backlog recalculates correctly
                    s.refresh(child_entry)
                    saved_ids.append(child_entry.id)
                    # First child in a parentless party becomes the root for siblings
                    if party_root_id is None:
                        party_root_id = child_entry.id
                    child_entries.append((child_name, next_position, child_agent_id, child_eta))
                    next_position += 1

                agent = s.get(Agent, assigned_agent_id)

        except Exception as exc:
            print(f"❌ _do_assign failed — rolling back {len(saved_ids)} entries: {exc}")
            if saved_ids:
                with Session(engine) as s:
                    for eid in saved_ids:
                        row = s.get(QueueEntry, eid)
                        if row:
                            s.delete(row)
                    s.commit()
            send_text(tenant, customer_num,
                "⚠️ Something went wrong while saving your booking. Please try again or contact the shop directly."
            )
            clear_session(tenant.id, customer_num)
            return

        # Recalculate ETAs for every agent touched by this booking
        agents_to_recalc = {assigned_agent_id}
        for _, _, child_agent_id, _ in child_entries:
            agents_to_recalc.add(child_agent_id)
        for aid in agents_to_recalc:
            recalculate_queue(tenant.id, aid, queue_date)

    with Session(engine) as s:
        service = s.get(Service, service_id)

    clear_session(tenant.id, customer_num)

    date_display = datetime.strptime(queue_date, "%Y-%m-%d").strftime("%a %d %b")

    # Build position summary lines
    position_lines = ""
    if include_parent:
        position_lines += (
            f"\U0001f4cd *Your position:* #{parent_entry.position} "
            f"| {tenant.agent_label}: {agent.name if agent else 'TBD'} "
            f"| \u23f0 {format_eta(eta)}\n"
        )
    for child_name, child_pos, child_agent_id, child_eta in child_entries:
        with Session(engine) as cs:
            child_agent = cs.get(Agent, child_agent_id)
        position_lines += (
            f"\U0001f476 *{child_name}:* #{child_pos} "
            f"| {tenant.agent_label}: {child_agent.name if child_agent else 'TBD'} "
            f"| \u23f0 {format_eta(child_eta)}\n"
        )

    # Notification promise is based on the first queued position
    first_position = parent_entry.position if include_parent else (child_entries[0][1] if child_entries else 1)
    if first_position == 1 and queue_date == core.today_str():
        notify_line = "You\'re first in line \U0001f3c6 — head over when you\'re ready."
    elif first_position == 1:
        notify_line = f"You\'re first in line for {date_display} \U0001f3c6 — we\'ll notify you on the day."
    elif first_position == 2:
        notify_line = "We\'ll notify you when you\'re up next."
    else:
        notify_line = f"We\'ll notify you when you\'re 2 away and when you\'re next."

    send_text(tenant, customer_num,
        f"\u2705 *You\'re in the queue!*\n\n"
        f"{position_lines}"
        f"\U0001f4bc {tenant.service_label}: {service.name if service else 'TBD'}\n"
        f"\U0001f4c5 Date: {date_display}\n\n"
        f"{notify_line}\n"
        f"Reply *status* anytime to check your position."
    )

    # Notify owner (report the first booked person)
    report_name = customer_name if include_parent else (children_names[0] if children_names else customer_name)
    report_position = parent_entry.position if include_parent else (child_entries[0][1] if child_entries else 1)
    report_eta = eta if include_parent else (child_entries[0][3] if child_entries else eta)
    # Report the agent actually assigned to the reported person (children may
    # land on a different agent than the parent's assigned_agent_id).
    if include_parent:
        report_agent_name = agent.name if agent else ""
    elif child_entries:
        with Session(engine) as ras:
            ra = ras.get(Agent, child_entries[0][2])
        report_agent_name = ra.name if ra else ""
    else:
        report_agent_name = agent.name if agent else ""
    if tenant.owner_number:
        send_text(tenant, normalize_number(tenant.owner_number),
            f"\U0001f514 *New Queue Entry*\n\n"
            f"\U0001f464 {report_name}\n"
            f"\U0001f4bc {service.name if service else ''}\n"
            f"\U0001f477 {report_agent_name}\n"
            f"\U0001f4cd Position #{report_position} | \u23f0 {format_eta(report_eta)}"
        )


@router.post("/webhook", dependencies=[Depends(verify_webhook_secret)])
async def handle_webhook(request: Request):
    data = await request.json()

    if data.get("event") != "messages.upsert":
        return {"status": "ignored"}

    msg_data = data.get("data", {})
    if msg_data.get("key", {}).get("fromMe"):
        return {"status": "ignored"}

    # Idempotency — Evolution retries deliver the same message id more than once.
    # Without this, a retried "1"/"now" reply creates duplicate queue entries.
    msg_id = msg_data.get("key", {}).get("id")
    if msg_id:
        if not config.redis_client.set(f"seen:{msg_id}", "1", nx=True, ex=600):
            return {"status": "duplicate"}

    tenant = get_tenant_by_number(data.get("sender", ""))
    if not tenant:
        return {"status": "unknown_tenant"}

    customer_num  = msg_data["key"]["remoteJid"]
    customer_name = msg_data.get("pushName", "Customer")
    message_obj   = msg_data.get("message", {})
    raw_text = (
        message_obj.get("conversation")
        or message_obj.get("extendedTextMessage", {}).get("text")
        or ""
    ).strip()
    text = raw_text.lower()

    sess  = get_session(tenant.id, customer_num)
    state = sess.get("state", "idle")

    print(f"\U0001f4e9 [{tenant.business_name}] {customer_num} | {state} | \'{text}\'")

    # A tenant runs one conversation or the other. Everything above this line —
    # idempotency, tenant resolution, session load — is shared; everything
    # below is the queue booking flow and does not apply to a takeaway.
    if tenant.mode == "orders":
        return orders_flow.handle_message(
            tenant, customer_num, customer_name, raw_text, text, sess, state)

    # ── GLOBAL TRIGGERS (work from any state) ─────────────────────────────
    if any(w in text for w in ["menu","start","hi","hello","hey"]) and state not in ["awaiting_booking_for", "awaiting_children", "awaiting_children_names", "awaiting_arrival_time"]:
        set_session(tenant.id, customer_num, {"state": "main_menu"})
        send_main_menu(tenant, customer_num)
        return {"status": "success"}

    # ── BACK HANDLER ──────────────────────────────────────────────────────
    if text == "0":
        if state == "awaiting_date":
            set_session(tenant.id, customer_num, {"state": "main_menu"})
            send_main_menu(tenant, customer_num)
        elif state == "awaiting_booking_for":
            # Back from "who for" → back to date picker or main menu
            date_options = sess.get("date_options")
            queue_date   = sess.get("pending_queue_date", core.today_str())
            if date_options:
                set_session(tenant.id, customer_num, {"state": "awaiting_date", "date_options": date_options})
                today_d = core.now().date()
                lines_out = []
                for i, d_str in enumerate(date_options):
                    d = datetime.strptime(d_str, "%Y-%m-%d").date()
                    label = d.strftime("%a %d %b")
                    if d == today_d:       label += " _(today)_"
                    elif d == today_d + timedelta(days=1): label += " _(tomorrow)_"
                    lines_out.append(f"{i+1}\u0031\ufe0f\u20e3 {label}")
                send_text(tenant, customer_num,
                    "*Which day would you like to queue for?* \U0001f4c5\n\n"
                    + "\n".join(lines_out)
                    + "\n\nReply with the *number* of the day, or *0* to go back."
                )
            else:
                set_session(tenant.id, customer_num, {"state": "main_menu"})
                send_main_menu(tenant, customer_num)
        elif state == "awaiting_service":
            # Back from service → back to "who for"
            queue_date   = sess.get("pending_queue_date", core.today_str())
            date_options = sess.get("date_options")
            set_session(tenant.id, customer_num, {
                "state":              "awaiting_booking_for",
                "pending_queue_date": queue_date,
                "date_options":       date_options,
            })
            send_text(tenant, customer_num,
                "Who are you booking for?\n"
                "1\ufe0f\u20e3 Just me\n"
                "2\ufe0f\u20e3 Me and my children\n"
                "3\ufe0f\u20e3 My children only\n\n"
                "Reply *1*, *2*, or *3*"
            )
        elif state == "awaiting_agent":
            # Back from agent → back to service menu
            queue_date   = sess.get("queue_date", core.today_str())
            service_ids  = sess.get("service_ids", [])
            if service_ids:
                set_session(tenant.id, customer_num, {
                    "state":              "awaiting_service",
                    "pending_queue_date": queue_date,
                    "service_ids":        service_ids,
                    "date_options":       sess.get("date_options"),
                    "include_parent":     sess.get("include_parent", True),
                    "children_collected": sess.get("children_collected", []),
                })
                with Session(engine) as s:
                    services = s.exec(select(Service).where(Service.id.in_(service_ids))).all()
                send_service_menu(tenant, customer_num, services)
            else:
                set_session(tenant.id, customer_num, {"state": "main_menu"})
                send_main_menu(tenant, customer_num)
        elif state in ("awaiting_children", "awaiting_children_names", "awaiting_arrival_time"):
            # Nothing saved yet — just cancel and return to main menu
            clear_session(tenant.id, customer_num)
            send_text(tenant, customer_num,
                "\u274c Booking cancelled.\n\nReply *menu* to start again."
            )
        elif state == "awaiting_rebook":
            set_session(tenant.id, customer_num, {"state": "main_menu"})
            send_main_menu(tenant, customer_num)
        else:
            set_session(tenant.id, customer_num, {"state": "main_menu"})
            send_main_menu(tenant, customer_num)
        return {"status": "success"}

    # ── MAIN MENU ─────────────────────────────────────────────────────────
    if state in ["idle", "main_menu"]:
        if state == "idle":
            set_session(tenant.id, customer_num, {"state": "main_menu"})
            send_main_menu(tenant, customer_num)
            return {"status": "success"}

        if text == "1":
            # Check if already in queue
            with Session(engine) as s:
                existing = s.exec(
                    select(QueueEntry).where(
                        QueueEntry.tenant_id      == tenant.id,
                        QueueEntry.customer_number == customer_num,
                        QueueEntry.queue_date      >= core.today_str(),
                        QueueEntry.status.in_(["Waiting", "InService"])
                    ).order_by(QueueEntry.queue_date, QueueEntry.joined_at)
                ).first()

            if existing:
                with Session(engine) as s:
                    agent   = s.get(Agent, existing.agent_id)
                    service = s.get(Service, existing.service_id)
                ahead_count = 0
                with Session(engine) as s:
                    ahead_count = len(s.exec(
                        select(QueueEntry).where(
                            QueueEntry.agent_id   == existing.agent_id,
                            QueueEntry.tenant_id  == tenant.id,
                            QueueEntry.queue_date == existing.queue_date,
                            QueueEntry.status     == "Waiting",
                            QueueEntry.position   < existing.position
                        )
                    ).all())
                set_session(tenant.id, customer_num, {
                    "state": "awaiting_rebook",
                    "existing_entry_id": existing.id,
                })
                existing_day = (
                    "today" if existing.queue_date == core.today_str()
                    else datetime.strptime(existing.queue_date, "%Y-%m-%d").strftime("%a %d %b")
                )
                send_text(tenant, customer_num,
                    f"You\'re already in the queue for {existing_day}!\n\n"
                    f"\U0001f4cd Position: #{existing.position}\n"
                    f"\U0001f464 {tenant.agent_label}: {agent.name if agent else 'TBD'}\n"
                    f"\U0001f4bc {tenant.service_label}: {service.name if service else 'TBD'}\n"
                    f"\u23f0 ETA: {format_eta(existing.estimated_start)}\n"
                    f"\U0001f465 People ahead: {ahead_count}\n\n"
                    f"Would you like to:\n"
                    f"1\ufe0f\u20e3 Keep my spot\n"
                    f"2\ufe0f\u20e3 Cancel and rebook for something else\n\n"
                    f"Reply *1* or *2*"
                )
                return {"status": "success"}

            # Queue closed check
            if tenant.advance_days == 0 and not queue_is_open_today(tenant):
                send_text(tenant, customer_num,
                    f"Sorry, the queue at *{tenant.business_name}* is currently closed.\n\n"
                    f"We open at {tenant.queue_opens:02d}:00. Please try again tomorrow! \U0001f64f"
                )
                clear_session(tenant.id, customer_num)
                return {"status": "success"}

            # Start booking flow — ask who first, then service
            if tenant.advance_days == 0:
                set_session(tenant.id, customer_num, {
                    "state": "awaiting_booking_for",
                    "pending_queue_date": core.today_str(),
                })
                send_text(tenant, customer_num,
                    "Who are you booking for?\n"
                    "1\ufe0f\u20e3 Just me\n"
                    "2\ufe0f\u20e3 Me and my children\n"
                    "3\ufe0f\u20e3 My children only\n\n"
                    "Reply *1*, *2*, or *3*"
                )
            else:
                send_date_menu(tenant, customer_num)

        elif text == "2":
            today = core.today_str()
            with Session(engine) as s:
                entries = s.exec(
                    select(QueueEntry).where(
                        QueueEntry.tenant_id       == tenant.id,
                        QueueEntry.customer_number == customer_num,
                        QueueEntry.queue_date      >= today,
                        QueueEntry.status.in_(["Waiting", "InService"])
                    ).order_by(QueueEntry.queue_date, QueueEntry.position)
                ).all()

                if not entries:
                    send_text(tenant, customer_num, "You\'re not currently in the queue.\n\nReply *menu* to join.")
                else:
                    agent   = s.get(Agent, entries[0].agent_id)
                    service = s.get(Service, entries[0].service_id)
                    ahead   = s.exec(
                        select(QueueEntry).where(
                            QueueEntry.agent_id   == entries[0].agent_id,
                            QueueEntry.tenant_id  == tenant.id,
                            QueueEntry.queue_date == entries[0].queue_date,
                            QueueEntry.status     == "Waiting",
                            QueueEntry.position   < entries[0].position
                        )
                    ).all()
                    ahead_count = len(ahead)

                    # Build position lines for all people in this booking
                    position_lines = ""
                    for e in entries:
                        position_lines += f"\U0001f522 *{e.customer_name}:* #{e.position}\n"

                    send_text(tenant, customer_num,
                        f"*Your Queue Status* \U0001f4cd\n\n"
                        f"{position_lines}"
                        f"\U0001f464 {tenant.agent_label}: {agent.name if agent else 'TBD'}\n"
                        f"\U0001f4bc {tenant.service_label}: {service.name if service else 'TBD'}\n"
                        f"\u23f0 Estimated time: {format_eta(entries[0].estimated_start)}\n"
                        f"\U0001f465 People ahead: {ahead_count}\n\n"
                        f"Reply *menu* for more options."
                    )
            clear_session(tenant.id, customer_num)

        elif text == "3":
            today = core.today_str()
            with Session(engine) as s:
                entry = s.exec(
                    select(QueueEntry).where(
                        QueueEntry.tenant_id      == tenant.id,
                        QueueEntry.customer_number == customer_num,
                        QueueEntry.queue_date      >= today,
                        QueueEntry.status          == "Waiting"
                    ).order_by(QueueEntry.queue_date, QueueEntry.joined_at)
                ).first()

                if not entry:
                    send_text(tenant, customer_num, "You\'re not currently in the queue.\n\nReply *menu* to go back.")
                else:
                    entry_id     = entry.id
                    party_date   = entry.queue_date
                    cust_name    = entry.customer_name
                    svc          = s.get(Service, entry.service_id)
                    svc_name     = svc.name if svc else ""
                    date_display = datetime.strptime(party_date, "%Y-%m-%d").strftime("%a %d %b")
            # Cancel the whole party (parent + children), then recalc each agent touched
            if entry:
                touched = cancel_party(tenant.id, entry_id)
                for aid in touched:
                    recalculate_queue(tenant.id, aid, party_date)
                send_text(tenant, customer_num,
                    f"\u2705 You\'ve been removed from the queue at *{tenant.business_name}*.\n\n"
                    f"Reply *menu* anytime to rejoin."
                )
                if tenant.owner_number:
                    send_text(tenant, normalize_number(tenant.owner_number),
                        f"\u274c *Queue Cancellation*\n\n"
                        f"\U0001f464 {cust_name}\n"
                        f"\U0001f4bc {svc_name}\n"
                        f"\U0001f4c5 {date_display}"
                    )
            clear_session(tenant.id, customer_num)

        else:
            send_text(tenant, customer_num, "Please reply with *1*, *2*, or *3*.")

        return {"status": "success"}

    # ── REBOOK CONFIRMATION ───────────────────────────────────────────────
    if state == "awaiting_rebook":
        existing_entry_id = sess.get("existing_entry_id")
        if text == "1":
            # Keep spot
            clear_session(tenant.id, customer_num)
            send_text(tenant, customer_num, "\U0001f44d Got it — your spot is safe! Reply *status* to check your position.")
        elif text == "2":
            # Cancel and rebook — cancel the whole party (parent + children)
            if existing_entry_id:
                with Session(engine) as s:
                    entry = s.get(QueueEntry, existing_entry_id)
                    party_date = entry.queue_date if entry else None
                if party_date:
                    touched = cancel_party(tenant.id, existing_entry_id)
                    for aid in touched:
                        recalculate_queue(tenant.id, aid, party_date)
            # Start fresh booking flow — ask who first, then service
            if tenant.advance_days == 0:
                set_session(tenant.id, customer_num, {
                    "state": "awaiting_booking_for",
                    "pending_queue_date": core.today_str(),
                })
                send_text(tenant, customer_num,
                    "Who are you booking for?\n"
                    "1\ufe0f\u20e3 Just me\n"
                    "2\ufe0f\u20e3 Me and my children\n"
                    "3\ufe0f\u20e3 My children only\n\n"
                    "Reply *1*, *2*, or *3*"
                )
            else:
                send_date_menu(tenant, customer_num)
        else:
            send_text(tenant, customer_num, "Please reply *1* to keep your spot or *2* to cancel and rebook.")
        return {"status": "success"}

    # ── DATE SELECTION ────────────────────────────────────────────────────
    if state == "awaiting_date":
        date_options = sess.get("date_options", [])
        if text == "0":
            set_session(tenant.id, customer_num, {"state": "main_menu"})
            send_main_menu(tenant, customer_num)
            return {"status": "success"}
        if text.isdigit() and 1 <= int(text) <= len(date_options):
            chosen_date = date_options[int(text) - 1]
            set_session(tenant.id, customer_num, {
                "state": "awaiting_booking_for",
                "pending_queue_date": chosen_date,
                "date_options": date_options,
            })
            send_text(tenant, customer_num,
                "Who are you booking for?\n"
                "1\ufe0f\u20e3 Just me\n"
                "2\ufe0f\u20e3 Me and my children\n"
                "3\ufe0f\u20e3 My children only\n\n"
                "Reply *1*, *2*, or *3*"
            )
        else:
            send_text(tenant, customer_num,
                f"Please reply with a number between 1 and {len(date_options)}, or *0* to go back.")
        return {"status": "success"}

    # ── SERVICE SELECTION ─────────────────────────────────────────────────
    if state == "awaiting_service":
        service_ids        = sess.get("service_ids", [])
        queue_date         = sess.get("pending_queue_date", core.today_str())
        include_parent     = sess.get("include_parent", True)
        children_collected = sess.get("children_collected", [])
        date_options       = sess.get("date_options")

        if text == "0":
            # Back → go to "who for" question (not date, since date is already known)
            set_session(tenant.id, customer_num, {
                "state":              "awaiting_booking_for",
                "pending_queue_date": queue_date,
                "date_options":       date_options,
            })
            send_text(tenant, customer_num,
                "Who are you booking for?\n"
                "1\ufe0f\u20e3 Just me\n"
                "2\ufe0f\u20e3 Me and my children\n"
                "3\ufe0f\u20e3 My children only\n\n"
                "Reply *1*, *2*, or *3*"
            )
            return {"status": "success"}

        if text.isdigit() and 1 <= int(text) <= len(service_ids):
            chosen_service_id  = service_ids[int(text) - 1]
            include_parent     = sess.get("include_parent", True)
            children_collected = sess.get("children_collected", [])
            with Session(engine) as s:
                tenant_agent_ids = [
                    a.id for a in s.exec(
                        select(Agent).where(Agent.tenant_id == tenant.id, Agent.is_active == True)
                    ).all()
                ]
                capable_ids = [
                    row.agent_id for row in s.exec(
                        select(AgentService).where(
                            AgentService.service_id == chosen_service_id,
                            AgentService.agent_id.in_(tenant_agent_ids)
                        )
                    ).all()
                ]
                agent_rows = s.exec(
                    select(Agent).where(Agent.id.in_(capable_ids), Agent.is_active == True)
                ).all()
                agents = [{"id": a.id, "name": a.name} for a in agent_rows]

            if not agents:
                send_text(tenant, customer_num,
                    f"Sorry, no {tenant.agent_label.lower()}s available for that service.\n\nReply *0* to go back.")
            elif len(agents) == 1:
                # Only one agent — skip agent menu, go straight to arrival time
                assigned_agent_id = agents[0]["id"]
                _backlog = get_agent_backlog_minutes(assigned_agent_id, tenant.id, queue_date)
                _eta     = calculate_estimated_start(tenant, assigned_agent_id, queue_date, _backlog)
                set_session(tenant.id, customer_num, {
                    "state":              "awaiting_arrival_time",
                    "pending_agent_id":   assigned_agent_id,
                    "pending_service_id": chosen_service_id,
                    "pending_queue_date": queue_date,
                    "include_parent":     include_parent,
                    "children_collected": children_collected,
                })
                send_text(tenant, customer_num,
                    f"\u23f0 Your {tenant.agent_label.lower()} is available around *{format_eta(_eta)}*.\n\n"
                    f"What time do you think you'll arrive?\n"
                    f"Reply with a time like *{format_eta(_eta)}* or *now* if you're already on your way."
                )
            else:
                set_session(tenant.id, customer_num, {
                    "state":              "awaiting_agent",
                    "queue_date":         queue_date,
                    "service_id":         chosen_service_id,
                    "agent_ids":          [a["id"] for a in agents],
                    "service_ids":        service_ids,
                    "date_options":       sess.get("date_options"),
                    "include_parent":     include_parent,
                    "children_collected": children_collected,
                })
                send_agent_menu(tenant, customer_num, agents, queue_date, chosen_service_id)
        else:
            send_text(tenant, customer_num,
                f"Please reply with a number between 1 and {len(service_ids)}, or *0* to go back.")
        return {"status": "success"}

    # ── AGENT SELECTION ───────────────────────────────────────────────────
    if state == "awaiting_agent":
        agent_ids          = sess.get("agent_ids", [])
        service_id         = sess.get("service_id")
        queue_date         = sess.get("queue_date", core.today_str())
        include_parent     = sess.get("include_parent", True)
        children_collected = sess.get("children_collected", [])
        no_pref_idx        = len(agent_ids) + 1

        if text == "0":
            service_ids = sess.get("service_ids", [])
            set_session(tenant.id, customer_num, {
                "state":              "awaiting_service",
                "pending_queue_date": queue_date,
                "service_ids":        service_ids,
                "date_options":       date_options,
                "include_parent":     include_parent,
                "children_collected": children_collected,
            })
            with Session(engine) as s:
                svc_objs = s.exec(select(Service).where(Service.id.in_(service_ids))).all()
            send_service_menu(tenant, customer_num, svc_objs)
            return {"status": "success"}

        if text.isdigit():
            choice = int(text)
            preferred_agent_id = None
            if 1 <= choice <= len(agent_ids):
                preferred_agent_id = agent_ids[choice - 1]
            elif choice != no_pref_idx:
                send_text(tenant, customer_num,
                    f"Please reply with a number between 1 and {no_pref_idx}, or *0* to go back.")
                return {"status": "success"}

            assigned_agent_id = assign_agent(tenant, service_id, preferred_agent_id, queue_date)
            if not assigned_agent_id:
                # Also reachable when everyone is simply off that day, so name
                # the date — another one may well work.
                day_name = datetime.strptime(queue_date, "%Y-%m-%d").strftime("%a %d %b")
                send_text(tenant, customer_num,
                    f"Sorry, no {tenant.agent_label.lower()}s are available on "
                    f"{day_name}.\n\nReply *0* to go back and try another day.")
                return {"status": "success"}

            _backlog = get_agent_backlog_minutes(assigned_agent_id, tenant.id, queue_date)
            _eta     = calculate_estimated_start(tenant, assigned_agent_id, queue_date, _backlog)
            set_session(tenant.id, customer_num, {
                "state":              "awaiting_arrival_time",
                "pending_agent_id":   assigned_agent_id,
                # Remember whether this was their pick or ours. The arrival-time
                # reply comes back a round trip later, by which point an
                # auto-pick may be stale — _do_assign redoes it under the lock.
                "pending_agent_explicit": preferred_agent_id is not None,
                "pending_service_id": service_id,
                "pending_queue_date": queue_date,
                "include_parent":     include_parent,
                "children_collected": children_collected,
            })
            send_text(tenant, customer_num,
                f"\u23f0 Your {tenant.agent_label.lower()} is available around *{format_eta(_eta)}*.\n\n"
                f"What time do you think you'll arrive?\n"
                f"Reply with a time like *{format_eta(_eta)}* or *now* if you're already on your way."
            )
        else:
            send_text(tenant, customer_num,
                f"Please reply with a number, or *0* to go back.")
        return {"status": "success"}

    # ── WHO ARE WE BOOKING FOR? ────────────────────────────────────────────
    if state == "awaiting_booking_for":
        pending_queue_date = sess.get("pending_queue_date", core.today_str())
        date_options       = sess.get("date_options")

        if text == "1":
            include_parent = True
            children_collected = []
        elif text in ("2", "3"):
            include_parent = (text == "2")
            set_session(tenant.id, customer_num, {
                "state":              "awaiting_children",
                "pending_queue_date": pending_queue_date,
                "date_options":       date_options,
                "include_parent":     include_parent,
            })
            send_text(tenant, customer_num,
                "How many children are you booking for?\n"
                "1\ufe0f\u20e3 1 child\n"
                "2\ufe0f\u20e3 2 children\n\n"
                "Reply *1* or *2*"
            )
            return {"status": "success"}
        else:
            send_text(tenant, customer_num,
                "Please reply *1* (just me), *2* (me + children), or *3* (children only)."
            )
            return {"status": "success"}

        # text == "1" path: go straight to service selection
        with Session(engine) as s:
            svc_objs = s.exec(
                select(Service).where(Service.tenant_id == tenant.id, Service.is_active == True)
            ).all()
            svc_list = [{"id": sv.id, "name": sv.name} for sv in svc_objs]
        if not svc_list:
            send_text(tenant, customer_num, f"No services configured. Contact {tenant.business_name} directly.")
            clear_session(tenant.id, customer_num)
        else:
            set_session(tenant.id, customer_num, {
                "state":              "awaiting_service",
                "pending_queue_date": pending_queue_date,
                "date_options":       date_options,
                "service_ids":        [sv["id"] for sv in svc_list],
                "include_parent":     include_parent,
                "children_collected": children_collected,
            })
            with Session(engine) as s:
                svc_objs = s.exec(select(Service).where(Service.id.in_([sv["id"] for sv in svc_list]))).all()
            send_service_menu(tenant, customer_num, svc_objs)
        return {"status": "success"}

    # ── HOW MANY CHILDREN? ────────────────────────────────────────────────
    if state == "awaiting_children":
        pending_queue_date = sess.get("pending_queue_date", core.today_str())
        date_options       = sess.get("date_options")
        include_parent     = sess.get("include_parent", True)

        if text.isdigit() and 1 <= int(text) <= 2:
            count = int(text)
            set_session(tenant.id, customer_num, {
                "state":              "awaiting_children_names",
                "pending_queue_date": pending_queue_date,
                "date_options":       date_options,
                "include_parent":     include_parent,
                "children_count":     count,
                "children_collected": [],
            })
            send_text(tenant, customer_num,
                f"Please send the name of child 1 of {count}:"
            )
        else:
            send_text(tenant, customer_num,
                "Reply *1* for 1 child or *2* for 2 children."
            )
        return {"status": "success"}

    # ── COLLECTING CHILDREN NAMES ─────────────────────────────────────────
    if state == "awaiting_children_names":
        pending_queue_date = sess.get("pending_queue_date", core.today_str())
        date_options       = sess.get("date_options")
        include_parent     = sess.get("include_parent", True)
        count              = sess.get("children_count", 1)
        collected          = sess.get("children_collected", [])
        collected.append(raw_text.strip())

        if len(collected) < count:
            set_session(tenant.id, customer_num, {
                "state":              "awaiting_children_names",
                "pending_queue_date": pending_queue_date,
                "date_options":       date_options,
                "include_parent":     include_parent,
                "children_count":     count,
                "children_collected": collected,
            })
            send_text(tenant, customer_num,
                f"Name of child {len(collected) + 1} of {count}:"
            )
        else:
            # All names collected — now go to service selection
            with Session(engine) as s:
                svc_objs = s.exec(
                    select(Service).where(Service.tenant_id == tenant.id, Service.is_active == True)
                ).all()
                svc_list = [{"id": sv.id, "name": sv.name} for sv in svc_objs]
            if not svc_list:
                send_text(tenant, customer_num, f"No services configured. Contact {tenant.business_name} directly.")
                clear_session(tenant.id, customer_num)
            else:
                set_session(tenant.id, customer_num, {
                    "state":              "awaiting_service",
                    "pending_queue_date": pending_queue_date,
                    "date_options":       date_options,
                    "service_ids":        [sv["id"] for sv in svc_list],
                    "include_parent":     include_parent,
                    "children_collected": collected,
                })
                with Session(engine) as s:
                    svc_objs = s.exec(select(Service).where(Service.id.in_([sv["id"] for sv in svc_list]))).all()
                send_service_menu(tenant, customer_num, svc_objs)
        return {"status": "success"}

    # ── ARRIVAL TIME ──────────────────────────────────────────────────────
    if state == "awaiting_arrival_time":
        pending_agent_id   = sess.get("pending_agent_id")
        pending_service_id = sess.get("pending_service_id")
        pending_queue_date = sess.get("pending_queue_date", core.today_str())
        include_parent     = sess.get("include_parent", True)
        collected          = sess.get("children_collected", [])
        # Sessions written before this field existed default to True — treat an
        # unknown pick as the customer's and leave it alone.
        agent_explicit     = sess.get("pending_agent_explicit", True)

        arrival = parse_arrival_time(text, pending_queue_date)
        if arrival is None:
            send_text(tenant, customer_num,
                "Sorry, I didn't understand that time. "
                "Please reply with a time like *12:30* or *now*."
            )
            return {"status": "success"}

        _do_assign(tenant, customer_num, customer_name,
                   pending_agent_id, pending_service_id, pending_queue_date, sess,
                   include_parent=include_parent, children_names=collected,
                   earliest_arrival=arrival,
                   customer_chose_agent=agent_explicit)
        return {"status": "success"}

    # ── FALLBACK ──────────────────────────────────────────────────────────
    clear_session(tenant.id, customer_num)
    send_main_menu(tenant, customer_num)
    return {"status": "success"}

