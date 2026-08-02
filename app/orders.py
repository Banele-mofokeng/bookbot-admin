"""
The ordering engine: what a cart costs, when the kitchen will be done, and the
rules for moving an order through its statuses.

No I/O beyond the database, and no message text — the same split the queue
engine keeps from the conversation flow.
"""
import math
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlmodel import Session, select

from app import core
from app.db import engine
from app.models import MenuItem, Order, OrderItem, Tenant

# Statuses an order can be in while the kitchen still owes the customer food.
OPEN_STATUSES = ["Placed", "Preparing"]
# Everything an order can be. Ordered as the kitchen works through them.
STATUSES = ["Placed", "Preparing", "Ready", "Collected", "Cancelled"]
TERMINAL_STATUSES = ["Collected", "Cancelled"]

# A customer may pull their own order back only before the kitchen starts it.
# After that the food exists and cancelling it is a staff decision at the
# counter, not a WhatsApp one.
CUSTOMER_CANCELLABLE = ["Placed"]


def sort_menu(items: List[MenuItem]) -> List[MenuItem]:
    """
    Menu order as staff mean it, not as the alphabet falls.

    Categories are ordered by the lowest sort_order any of their items carries,
    so setting Kotas to 0 and Drinks to 10 puts kotas first without anyone
    having to name the categories alphabetically. Items inside a category then
    go by their own sort_order, with the name as a stable tiebreak.
    Uncategorised items always land at the bottom.
    """
    first_in_category: Dict[str, int] = {}
    for item in items:
        key = item.category or ""
        first_in_category[key] = min(
            first_in_category.get(key, item.sort_order), item.sort_order)

    def position(item: MenuItem):
        category = item.category or ""
        return (
            1 if not category else 0,          # uncategorised last
            first_in_category[category],
            category,
            item.sort_order,
            item.name,
        )

    return sorted(items, key=position)


def get_menu(tenant_id: int, include_inactive: bool = False) -> List[MenuItem]:
    """The menu as the customer is shown it. Inactive items are sold out and
    must never be offered."""
    with Session(engine) as s:
        q = select(MenuItem).where(MenuItem.tenant_id == tenant_id)
        if not include_inactive:
            q = q.where(MenuItem.is_active == True)
        items = s.exec(q).all()
    return sort_menu(items)


def price_cart(tenant_id: int, cart: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Turn a session cart — [{"menu_item_id": 3, "qty": 2}, …] — into priced
    lines and a total, reading today's menu.

    Lines whose item has since been deleted or deactivated are dropped and
    reported separately: a customer must never be charged for, or handed, an
    item the shop has stopped selling mid-conversation.
    """
    ids = [line["menu_item_id"] for line in cart]
    with Session(engine) as s:
        found = s.exec(
            select(MenuItem).where(
                MenuItem.tenant_id == tenant_id,
                MenuItem.id.in_(ids or [-1]),
            )
        ).all()
    by_id = {m.id: m for m in found}

    lines, dropped = [], []
    for entry in cart:
        item = by_id.get(entry["menu_item_id"])
        qty  = max(1, int(entry.get("qty", 1)))
        if not item or not item.is_active:
            dropped.append(item.name if item else "an item")
            continue
        lines.append({
            "menu_item_id":     item.id,
            "name":             item.name,
            "qty":              qty,
            "unit_price_cents": item.price_cents,
            "prep_minutes":     item.prep_minutes,
            "line_total_cents": item.price_cents * qty,
        })

    return {
        "lines":       lines,
        "dropped":     dropped,
        "total_cents": sum(l["line_total_cents"] for l in lines),
    }


def kitchen_backlog_minutes(tenant: Tenant, order_date: str,
                            before_order_id: Optional[int] = None) -> int:
    """
    How long the kitchen is already committed for, in minutes.

    Every open order's prep time is summed and divided by how many items the
    kitchen genuinely works at once. A single fryer quoting the sum would be
    right; four pans quoting the sum tells every customer to come back an hour
    late, and they stop believing the quote.

    before_order_id limits the count to orders placed ahead of a given one, so
    an existing order can be re-quoted without counting itself or the orders
    behind it.
    """
    with Session(engine) as s:
        q = select(Order).where(
            Order.tenant_id  == tenant.id,
            Order.order_date == order_date,
            Order.status.in_(OPEN_STATUSES),
        )
        if before_order_id is not None:
            q = q.where(Order.id < before_order_id)
        open_orders = s.exec(q).all()
        if not open_orders:
            return 0
        lines = s.exec(
            select(OrderItem).where(
                OrderItem.order_id.in_([o.id for o in open_orders])
            )
        ).all()

    committed = sum(l.prep_minutes * l.qty for l in lines)
    parallel  = max(1, tenant.kitchen_parallel_items)
    return math.ceil(committed / parallel)


def estimate_ready_at(tenant: Tenant, lines: List[Dict[str, Any]],
                      order_date: str, before_order_id: Optional[int] = None,
                      floor: Optional[datetime] = None) -> datetime:
    """
    When this order will be ready.

    The kitchen has to clear what it already owes other customers before this
    order is done, and then this order's own slowest item has to cook. Items
    within one order are assumed to come off together — a burger and a Coke are
    handed over as one bag, so the Coke doesn't finish sooner in any way the
    customer experiences.
    """
    start   = floor or core.now()
    backlog = kitchen_backlog_minutes(tenant, order_date, before_order_id)
    own     = max((l["prep_minutes"] for l in lines), default=0)
    return start + timedelta(minutes=backlog + own)


def place_order(tenant: Tenant, customer_number: str, customer_name: str,
                cart: List[Dict[str, Any]], order_date: Optional[str] = None,
                placed_via: str = "whatsapp", customer_phone: str = "",
                note: str = "") -> Optional[Order]:
    """
    Write a priced order and its lines. Returns the saved Order, or None if
    nothing on the cart is still orderable.

    Prices, names and prep times are copied onto the lines here. From this
    point the order is independent of the menu: repricing a kota tonight leaves
    this morning's takings alone.
    """
    order_date = order_date or core.today_str()
    priced     = price_cart(tenant.id, cart)
    if not priced["lines"]:
        return None

    ready_at = estimate_ready_at(tenant, priced["lines"], order_date)

    with Session(engine) as s:
        order = Order(
            tenant_id       = tenant.id,
            customer_number = customer_number,
            customer_name   = customer_name,
            customer_phone  = customer_phone,
            status          = "Placed",
            total_cents     = priced["total_cents"],
            order_date      = order_date,
            placed_via      = placed_via,
            ready_at        = ready_at,
            note            = note,
            # Set explicitly rather than leaning on the field default, which
            # binds core.now() at class-definition time.
            created_at      = core.now(),
        )
        s.add(order)
        s.commit()
        s.refresh(order)

        for line in priced["lines"]:
            s.add(OrderItem(
                order_id         = order.id,
                menu_item_id     = line["menu_item_id"],
                name             = line["name"],
                qty              = line["qty"],
                unit_price_cents = line["unit_price_cents"],
                prep_minutes     = line["prep_minutes"],
            ))
        s.commit()
        s.refresh(order)
    return order


def get_open_order(tenant_id: int, customer_number: str,
                   order_date: Optional[str] = None) -> Optional[Order]:
    """The customer's order that the kitchen still owes them, newest first.
    Ready counts as open — the food is waiting on the counter for them."""
    order_date = order_date or core.today_str()
    with Session(engine) as s:
        return s.exec(
            select(Order).where(
                Order.tenant_id       == tenant_id,
                Order.customer_number == customer_number,
                Order.order_date      == order_date,
                Order.status.in_(OPEN_STATUSES + ["Ready"]),
            ).order_by(Order.id.desc())
        ).first()


def get_order_lines(order_id: int) -> List[OrderItem]:
    with Session(engine) as s:
        return s.exec(
            select(OrderItem).where(OrderItem.order_id == order_id)
                             .order_by(OrderItem.id)
        ).all()


def set_status(order_id: int, status: str, closed_by: str = "staff") -> Optional[Order]:
    """
    Move an order along and stamp the timings reporting depends on.

    started_at and finished_at are set once each. Bouncing an order back to
    Preparing to re-fire an item must not erase when the kitchen first picked
    it up, or the prep-time numbers measure the last mistake instead of the
    work.
    """
    if status not in STATUSES:
        return None
    with Session(engine) as s:
        order = s.get(Order, order_id)
        if not order:
            return None
        order.status = status
        if status == "Preparing" and order.started_at is None:
            order.started_at = core.now()
        if status in TERMINAL_STATUSES:
            if order.finished_at is None:
                order.finished_at = core.now()
            order.closed_by = closed_by
        s.add(order)
        s.commit()
        s.refresh(order)
        return order
