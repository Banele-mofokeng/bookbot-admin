"""
The takeaway conversation: browse a menu, build a cart, place an order, chase
it, cancel it.

Runs for tenants with mode == "orders". The queue flow in webhook.py is
untouched by anything here — the two share the tenant, the session store and
the outbox, and nothing else.

Every state name is prefixed "ord_" so a tenant that switches modes mid-session
can never land in a queue state holding a cart, or the reverse.
"""
from typing import Any, Dict, List, Optional

from sqlmodel import Session

from app import core, orders
from app.core import format_money
from app.db import engine
from app.messaging import queue_is_open_today as shop_is_open, send_text
from app.models import MenuItem, Tenant
from app.sessions import clear_session, set_session

# A typo shouldn't be able to order fifty kotas. Anyone genuinely feeding a
# street can send the order twice, or phone.
MAX_QTY = 20

DIGITS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

# Words that take a customer back to the top from wherever they are.
GREETINGS = {"menu", "start", "hi", "hello", "hey", "order", "orders", "howzit"}

STATUS_LABELS = {
    "Placed":    "👍 Received — waiting for the kitchen",
    "Preparing": "🍳 Being made now",
    "Ready":     "✅ Ready for collection",
    "Collected": "🙏 Collected",
    "Cancelled": "❌ Cancelled",
}


def _digit(i: int) -> str:
    """Numbered bullet for a list position (0-indexed)."""
    return DIGITS[i] if i < len(DIGITS) else f"*{i + 1}.*"


def _parse_choice(text: str, count: int) -> Optional[int]:
    """A reply that is a valid 1-based position in a list of `count`, as a
    0-based index. Anything else is None — including 0, which is 'back'."""
    if not text.isdigit():
        return None
    n = int(text)
    return n - 1 if 1 <= n <= count else None


# =============================================================================
# MESSAGES
# =============================================================================

def send_order_main_menu(tenant: Tenant, number: str):
    send_text(tenant, number,
        f"*Welcome to {tenant.business_name}* 👋\n\n"
        f"What would you like to do?\n\n"
        f"1️⃣ Place an order\n"
        f"2️⃣ My order\n"
        f"3️⃣ Cancel my order\n\n"
        f"Reply with *1*, *2*, or *3*"
    )


def send_menu(tenant: Tenant, number: str, items: List[MenuItem]):
    """The food menu, grouped by category in display order."""
    lines, current = [], None
    for i, item in enumerate(items):
        category = item.category or "More"
        if category != current:
            lines.append(f"\n_{category}_")
            current = category
        lines.append(f"{_digit(i)} {item.name} — "
                     f"{format_money(item.price_cents, tenant.currency_symbol)}")

    send_text(tenant, number,
        "*Our menu* 🍔"
        + "\n".join(lines)
        + "\n\nReply with an item *number* to add it.\n*0* to go back."
    )


def _cart_summary(tenant: Tenant, priced: Dict[str, Any]) -> str:
    lines = [
        f"{l['qty']} × {l['name']} — "
        f"{format_money(l['line_total_cents'], tenant.currency_symbol)}"
        for l in priced["lines"]
    ]
    return ("\n".join(lines)
            + f"\n\n*Total: {format_money(priced['total_cents'], tenant.currency_symbol)}*")


def send_cart(tenant: Tenant, number: str, priced: Dict[str, Any]):
    sold_out = ""
    if priced["dropped"]:
        # Said plainly rather than silently dropped — a customer who thinks
        # they ordered chips and gets none blames the shop, not the bot.
        sold_out = ("\n\n⚠️ Sorry, we've run out of "
                    + ", ".join(priced["dropped"]) + ".")
    send_text(tenant, number,
        "*Your order* 🧾\n\n"
        + _cart_summary(tenant, priced)
        + sold_out
        + "\n\n1️⃣ Add another item\n2️⃣ Checkout\n3️⃣ Start over\n\n"
        + "Reply with *1*, *2*, or *3*"
    )


# =============================================================================
# FLOW
# =============================================================================

def handle_message(tenant: Tenant, customer_num: str, customer_name: str,
                   raw_text: str, text: str, sess: Dict[str, Any],
                   state: str) -> Dict[str, str]:
    """
    One inbound WhatsApp message for an ordering tenant.

    Mirrors the queue handler's contract: the caller has already resolved the
    tenant, loaded the session and lowercased the text, and expects a small
    JSON-able dict back.
    """
    cart: List[Dict[str, Any]] = sess.get("cart", [])

    # ── GLOBAL TRIGGERS ───────────────────────────────────────────────────
    # Matched on whole words, never as substrings: "no chilli" contains "hi",
    # and a customer typing their special request must not be thrown back to
    # the main menu with their cart half-built. The note step opts out entirely,
    # since anything typed there is meant as free text.
    words = text.split()
    if state != "ord_note" and words and (
            text in GREETINGS or words[0] in GREETINGS):
        # The cart survives a greeting. Someone who types "hi" halfway through
        # ordering hasn't asked to throw away what they picked.
        set_session(tenant.id, customer_num, {"state": "ord_menu", "cart": cart})
        send_order_main_menu(tenant, customer_num)
        return {"status": "success"}

    # ── BACK ──────────────────────────────────────────────────────────────
    if text == "0":
        if state in ("ord_qty", "ord_browsing"):
            return _show_menu(tenant, customer_num, cart)
        if state in ("ord_note", "ord_confirm") and cart:
            return _show_cart(tenant, customer_num, cart)
        set_session(tenant.id, customer_num, {"state": "ord_menu", "cart": cart})
        send_order_main_menu(tenant, customer_num)
        return {"status": "success"}

    # ── MAIN MENU ─────────────────────────────────────────────────────────
    if state in ("idle", "ord_menu"):
        if text == "1":
            return _start_order(tenant, customer_num, cart)
        if text == "2":
            return _report_status(tenant, customer_num)
        if text == "3":
            return _cancel_order(tenant, customer_num)
        send_order_main_menu(tenant, customer_num)
        return {"status": "success"}

    # ── PICKING AN ITEM ───────────────────────────────────────────────────
    if state == "ord_browsing":
        menu_ids = sess.get("menu_ids", [])
        idx = _parse_choice(text, len(menu_ids))
        if idx is None:
            send_text(tenant, customer_num,
                "Please reply with an item *number* from the menu, or *0* to go back.")
            return {"status": "success"}

        item_id = menu_ids[idx]
        with Session(engine) as s:
            item = s.get(MenuItem, item_id)
        if not item or not item.is_active:
            send_text(tenant, customer_num,
                "Sorry, that one just sold out. Please pick something else.")
            return _show_menu(tenant, customer_num, cart)

        set_session(tenant.id, customer_num, {
            "state":     "ord_qty",
            "cart":      cart,
            "menu_ids":  menu_ids,
            "pending_item_id": item.id,
        })
        send_text(tenant, customer_num,
            f"How many *{item.name}*?\n\n"
            f"Reply with a number (1–{MAX_QTY}), or *0* to go back."
        )
        return {"status": "success"}

    # ── QUANTITY ──────────────────────────────────────────────────────────
    if state == "ord_qty":
        if not text.isdigit() or not (1 <= int(text) <= MAX_QTY):
            send_text(tenant, customer_num,
                f"Please reply with a number between *1* and *{MAX_QTY}*, "
                f"or *0* to go back.")
            return {"status": "success"}

        item_id = sess.get("pending_item_id")
        qty     = int(text)
        # Ordering the same thing twice adds up rather than replacing — two
        # separate "1 × chips" replies mean two portions of chips.
        for line in cart:
            if line["menu_item_id"] == item_id:
                line["qty"] = min(MAX_QTY, line["qty"] + qty)
                break
        else:
            cart.append({"menu_item_id": item_id, "qty": qty})
        return _show_cart(tenant, customer_num, cart)

    # ── CART ──────────────────────────────────────────────────────────────
    if state == "ord_cart":
        if text == "1":
            return _show_menu(tenant, customer_num, cart)
        if text == "2":
            if not cart:
                return _show_menu(tenant, customer_num, cart)
            # Re-price before taking the order further. Something can sell out
            # between building the cart and checking out, and the customer must
            # see that on the cart screen rather than discovering a shorter
            # order at the counter.
            priced = orders.price_cart(tenant.id, cart)
            if not priced["lines"]:
                return _sold_out_everything(tenant, customer_num)
            if priced["dropped"]:
                return _show_cart(tenant, customer_num, cart)
            set_session(tenant.id, customer_num, {"state": "ord_note", "cart": cart})
            send_text(tenant, customer_num,
                "Any special request? (e.g. _no chilli_)\n\n"
                "Reply *no* if there isn't one."
            )
            return {"status": "success"}
        if text == "3":
            clear_session(tenant.id, customer_num)
            set_session(tenant.id, customer_num, {"state": "ord_menu", "cart": []})
            send_text(tenant, customer_num, "🗑️ Order cleared.")
            send_order_main_menu(tenant, customer_num)
            return {"status": "success"}
        return _show_cart(tenant, customer_num, cart)

    # ── SPECIAL REQUEST ───────────────────────────────────────────────────
    if state == "ord_note":
        note = "" if text in ("no", "none", "nothing", "n") else raw_text[:200]
        priced = orders.price_cart(tenant.id, cart)
        if not priced["lines"]:
            return _sold_out_everything(tenant, customer_num)

        ready_at = orders.estimate_ready_at(
            tenant, priced["lines"], core.today_str())
        set_session(tenant.id, customer_num, {
            "state": "ord_confirm", "cart": cart, "note": note,
        })
        send_text(tenant, customer_num,
            "*Confirm your order* 🧾\n\n"
            + _cart_summary(tenant, priced)
            + (f"\n\n_Note: {note}_" if note else "")
            + f"\n\nReady around *{core.format_eta(ready_at)}* ⏰"
            + "\n\nReply *yes* to place it, or *0* to go back."
        )
        return {"status": "success"}

    # ── CONFIRM ───────────────────────────────────────────────────────────
    if state == "ord_confirm":
        if text not in ("yes", "y", "confirm", "ok"):
            send_text(tenant, customer_num,
                "Reply *yes* to place your order, or *0* to go back.")
            return {"status": "success"}

        order = orders.place_order(
            tenant, customer_num, customer_name, cart,
            note=sess.get("note", ""),
        )
        if not order:
            return _sold_out_everything(tenant, customer_num)

        lines = orders.get_order_lines(order.id)
        clear_session(tenant.id, customer_num)
        send_text(tenant, customer_num,
            f"✅ *Order #{order.code} placed!*\n\n"
            + "\n".join(f"{l.qty} × {l.name}" for l in lines)
            + f"\n\n*Total: {format_money(order.total_cents, tenant.currency_symbol)}*"
            + (f"\n_Note: {order.note}_" if order.note else "")
            + f"\n\nReady for collection around *{core.format_eta(order.ready_at)}* ⏰\n"
            + "We'll message you the moment it's ready.\n\n"
            + "_Pay at the counter when you collect._"
        )
        return {"status": "success"}

    # ── FALLBACK ──────────────────────────────────────────────────────────
    set_session(tenant.id, customer_num, {"state": "ord_menu", "cart": cart})
    send_order_main_menu(tenant, customer_num)
    return {"status": "success"}


# =============================================================================
# STEPS
# =============================================================================

def _start_order(tenant: Tenant, customer_num: str,
                 cart: List[Dict[str, Any]]) -> Dict[str, str]:
    if not shop_is_open(tenant):
        send_text(tenant, customer_num,
            f"Sorry, *{tenant.business_name}* is closed right now. 😴\n\n"
            f"We open at {tenant.queue_opens:02d}:00 — see you then!"
        )
        return {"status": "success"}
    return _show_menu(tenant, customer_num, cart)


def _show_menu(tenant: Tenant, customer_num: str,
               cart: List[Dict[str, Any]]) -> Dict[str, str]:
    items = orders.get_menu(tenant.id)
    if not items:
        send_text(tenant, customer_num,
            "Our menu isn't up yet — please check back shortly. 🙏")
        return {"status": "success"}

    set_session(tenant.id, customer_num, {
        "state":    "ord_browsing",
        "cart":     cart,
        "menu_ids": [i.id for i in items],
    })
    send_menu(tenant, customer_num, items)
    return {"status": "success"}


def _show_cart(tenant: Tenant, customer_num: str,
               cart: List[Dict[str, Any]]) -> Dict[str, str]:
    priced = orders.price_cart(tenant.id, cart)
    if not priced["lines"]:
        return _sold_out_everything(tenant, customer_num)

    # Drop sold-out lines from the session too, so the next screen doesn't
    # re-announce them.
    kept = {l["menu_item_id"] for l in priced["lines"]}
    cart = [c for c in cart if c["menu_item_id"] in kept]

    set_session(tenant.id, customer_num, {"state": "ord_cart", "cart": cart})
    send_cart(tenant, customer_num, priced)
    return {"status": "success"}


def _sold_out_everything(tenant: Tenant, customer_num: str) -> Dict[str, str]:
    clear_session(tenant.id, customer_num)
    send_text(tenant, customer_num,
        "😞 Sorry — everything on your order has sold out.\n\n"
        "Reply *menu* to start again."
    )
    return {"status": "success"}


def _report_status(tenant: Tenant, customer_num: str) -> Dict[str, str]:
    order = orders.get_open_order(tenant.id, customer_num)
    if not order:
        send_text(tenant, customer_num,
            "You don't have an order with us today.\n\n"
            "Reply *1* to place one. 🍔"
        )
        return {"status": "success"}

    lines = orders.get_order_lines(order.id)
    when  = ""
    if order.status in orders.OPEN_STATUSES and order.ready_at:
        when = f"\n\nReady around *{core.format_eta(order.ready_at)}* ⏰"
    elif order.status == "Ready":
        when = "\n\nCome collect it at the counter! 🛍️"

    send_text(tenant, customer_num,
        f"*Order #{order.code}*\n\n"
        + "\n".join(f"{l.qty} × {l.name}" for l in lines)
        + f"\n\n*Total: {format_money(order.total_cents, tenant.currency_symbol)}*"
        + f"\n\nStatus: {STATUS_LABELS.get(order.status, order.status)}"
        + when
    )
    return {"status": "success"}


def _cancel_order(tenant: Tenant, customer_num: str) -> Dict[str, str]:
    order = orders.get_open_order(tenant.id, customer_num)
    if not order:
        send_text(tenant, customer_num, "You have no order to cancel today.")
        return {"status": "success"}

    if order.status not in orders.CUSTOMER_CANCELLABLE:
        # The food already exists. Writing it off is a counter decision.
        send_text(tenant, customer_num,
            f"Order *#{order.code}* is already being made, so we can't cancel "
            f"it here. 🍳\n\nPlease speak to us at the counter."
        )
        return {"status": "success"}

    orders.set_status(order.id, "Cancelled", closed_by="customer")
    clear_session(tenant.id, customer_num)
    send_text(tenant, customer_num,
        f"❌ Order *#{order.code}* cancelled.\n\nReply *menu* to order again."
    )
    return {"status": "success"}
