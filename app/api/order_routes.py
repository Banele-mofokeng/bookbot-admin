"""The kitchen board: today's orders, their statuses, and counter orders."""
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, SQLModel, select

from app import core, orders
from app.auth import ensure_tenant_access, get_current_user
from app.core import format_money
from app.db import engine
from app.messaging import send_text
from app.models import Order, OrderItem, Tenant, User

router = APIRouter(dependencies=[Depends(get_current_user)])


def _order_public(order: Order, lines: List[OrderItem]) -> Dict[str, Any]:
    """One board row. `code` is a property, so it has to be spelled out here —
    it would not survive plain model serialisation."""
    return {
        "id":              order.id,
        "code":            order.code,
        "tenant_id":       order.tenant_id,
        "customer_name":   order.customer_name,
        "customer_number": order.customer_number,
        "customer_phone":  order.customer_phone,
        "status":          order.status,
        "total_cents":     order.total_cents,
        "order_date":      order.order_date,
        "placed_via":      order.placed_via,
        "ready_at":        order.ready_at,
        "note":            order.note,
        "created_at":      order.created_at,
        "started_at":      order.started_at,
        "finished_at":     order.finished_at,
        "closed_by":       order.closed_by,
        "items": [
            {"id": l.id, "name": l.name, "qty": l.qty,
             "unit_price_cents": l.unit_price_cents,
             "line_total_cents": l.unit_price_cents * l.qty}
            for l in lines
        ],
    }


@router.get("/admin/orders/{tenant_id}")
def get_orders(tenant_id: int, order_date: Optional[str] = None,
               user: User = Depends(get_current_user)):
    """
    Every order for one day, oldest first — the order the kitchen works in.

    Lines for the whole day are fetched in one query rather than per order;
    a busy lunch is hundreds of orders and the board polls.
    """
    ensure_tenant_access(user, tenant_id)
    target = order_date or core.today_str()
    with Session(engine) as s:
        rows = s.exec(
            select(Order).where(
                Order.tenant_id  == tenant_id,
                Order.order_date == target,
            ).order_by(Order.id)
        ).all()
        lines = s.exec(
            select(OrderItem).where(
                OrderItem.order_id.in_([o.id for o in rows] or [-1])
            ).order_by(OrderItem.id)
        ).all()

    by_order: Dict[int, List[OrderItem]] = {}
    for line in lines:
        by_order.setdefault(line.order_id, []).append(line)

    open_orders = [o for o in rows if o.status in orders.OPEN_STATUSES]
    return {
        "order_date": target,
        "orders":     [_order_public(o, by_order.get(o.id, [])) for o in rows],
        "summary": {
            "open":      len(open_orders),
            "ready":     len([o for o in rows if o.status == "Ready"]),
            "collected": len([o for o in rows if o.status == "Collected"]),
            "cancelled": len([o for o in rows if o.status == "Cancelled"]),
            # Money actually taken, not money ordered — cancelled food was
            # never sold and an open order has not been paid for yet.
            "takings_cents": sum(o.total_cents for o in rows
                                 if o.status == "Collected"),
        },
    }


def _notify_customer(tenant: Tenant, order: Order):
    """
    Tell the customer their food is up, or that staff cancelled the order.

    The dedupe key makes this safe to reach more than once — a double-tap on
    "Ready", or a bounce back through Preparing and forward again, must not
    send the same shout twice.
    """
    if not order.customer_number:
        return
    if order.status == "Ready":
        send_text(tenant, order.customer_number,
            f"🛍️ *Order #{order.code} is ready!*\n\n"
            f"Come collect it at the counter.\n"
            f"Total to pay: *{format_money(order.total_cents, tenant.currency_symbol)}*",
            dedupe_key=f"order_ready:{order.id}",
        )
    elif order.status == "Cancelled" and order.closed_by == "staff":
        send_text(tenant, order.customer_number,
            f"❌ Sorry — we've had to cancel order *#{order.code}*.\n\n"
            f"Please speak to us at the counter or order again.",
            dedupe_key=f"order_cancelled:{order.id}",
        )


@router.patch("/admin/orders/{order_id}/status")
def update_order_status(order_id: int, body: Dict[str, Any],
                        user: User = Depends(get_current_user)):
    status = body.get("status")
    if status not in orders.STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"status must be one of {', '.join(orders.STATUSES)}")

    with Session(engine) as s:
        order = s.get(Order, order_id)
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")
        ensure_tenant_access(user, order.tenant_id)
        tenant = s.get(Tenant, order.tenant_id)

    updated = orders.set_status(order_id, status, closed_by="staff")
    _notify_customer(tenant, updated)

    if status == "Ready" and not updated.notified_ready:
        with Session(engine) as s:
            row = s.get(Order, order_id)
            row.notified_ready = True
            s.add(row)
            s.commit()
            s.refresh(row)
            updated = row

    return _order_public(updated, orders.get_order_lines(order_id))


class CounterOrderLine(SQLModel):
    menu_item_id: int
    qty:          int = 1


class CounterOrderCreate(SQLModel):
    tenant_id:      int
    customer_name:  str = "Walk-in"
    customer_phone: str = ""
    note:           str = ""
    items:          List[CounterOrderLine] = []


@router.post("/admin/orders")
def create_counter_order(data: CounterOrderCreate,
                         user: User = Depends(get_current_user)):
    """
    Ring up an order taken at the counter.

    Priced and queued exactly like a WhatsApp order, so the kitchen board stays
    one list and the ready-time quote accounts for walk-in food too. A phone
    number is optional; given one, the customer gets the same "ready" message.
    """
    ensure_tenant_access(user, data.tenant_id)
    if not data.items:
        raise HTTPException(status_code=400, detail="An order needs at least one item")

    with Session(engine) as s:
        tenant = s.get(Tenant, data.tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Business not found")

    phone = core.normalize_number(data.customer_phone) if data.customer_phone else ""
    order = orders.place_order(
        tenant,
        # Counter customers are reachable only if they left a number. The
        # WhatsApp JID format is what send_text expects.
        customer_number = f"{phone}@s.whatsapp.net" if phone else "",
        customer_name   = data.customer_name or "Walk-in",
        cart            = [{"menu_item_id": i.menu_item_id, "qty": i.qty}
                           for i in data.items],
        placed_via      = "counter",
        customer_phone  = phone,
        note            = data.note,
    )
    if not order:
        raise HTTPException(status_code=400,
                            detail="None of those items are on the menu right now")
    return _order_public(order, orders.get_order_lines(order.id))
