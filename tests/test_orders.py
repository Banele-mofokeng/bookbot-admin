"""
The ordering vertical: the WhatsApp cart flow, pricing, ready-time quotes, and
the kitchen board.

The conversation is driven through the real /webhook endpoint so the mode
dispatch is exercised too, with Redis replaced by an in-process fake — the same
approach the concurrency tests take.
"""
import itertools
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

import main
from app import config, core, db, jobs, orders
from app.models import MenuItem, Order, OutboxMessage, Tenant

TODAY = "2026-08-02"


# ── fakes and helpers ────────────────────────────────────────────────────────
class FakeRedis:
    """Just the operations the session store and the webhook idempotency
    guard use."""

    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.store[key] = value
        return True

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def delete(self, key):
        self.store.pop(key, None)
        return 1

    def ping(self):
        return True


@pytest.fixture
def fake_redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(config, "redis_client", fake)
    return fake


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    """Midday on TODAY — inside the shop's trading hours, so the flow is not
    at the mercy of when the suite happens to run."""
    at = datetime(2026, 8, 2, 12, 0)
    monkeypatch.setattr(core, "now", lambda: at)
    return at


def _seed_shop(mode="orders", parallel=4, opens=8, closes=20, number="27820000000"):
    with Session(db.engine) as s:
        t = Tenant(
            business_name="Kasi Kotas", whatsapp_number=number,
            evolution_instance="i", evolution_api_key="k",
            evolution_api_url="http://x",
            queue_opens=opens, queue_closes=closes,
            mode=mode, currency_symbol="R", kitchen_parallel_items=parallel,
        )
        s.add(t); s.commit(); s.refresh(t)
        return t.id


def _add_item(tenant_id, name, price_cents, prep_minutes=10,
              category="Kotas", is_active=True, sort_order=0):
    with Session(db.engine) as s:
        item = MenuItem(
            tenant_id=tenant_id, name=name, price_cents=price_cents,
            prep_minutes=prep_minutes, category=category,
            is_active=is_active, sort_order=sort_order,
        )
        s.add(item); s.commit(); s.refresh(item)
        return item.id


_msg_ids = itertools.count(1)


def _say(client, text, number="27760000001", sender="27820000000"):
    """One inbound WhatsApp message. Each gets a fresh id so the handler's
    duplicate guard doesn't swallow it."""
    return client.post("/webhook", json={
        "event": "messages.upsert",
        "sender": sender,
        "data": {
            "key": {"id": f"m{next(_msg_ids)}",
                    "remoteJid": f"{number}@s.whatsapp.net"},
            "pushName": "Thabo",
            "message": {"conversation": text},
        },
    })


def _sent_bodies(tenant_id=None):
    with Session(db.engine) as s:
        q = select(OutboxMessage).order_by(OutboxMessage.id)
        if tenant_id:
            q = q.where(OutboxMessage.tenant_id == tenant_id)
        return [m.body for m in s.exec(q).all()]


def _last_body():
    bodies = _sent_bodies()
    assert bodies, "the bot said nothing"
    return bodies[-1]


def _orders_for(tenant_id):
    with Session(db.engine) as s:
        return s.exec(
            select(Order).where(Order.tenant_id == tenant_id).order_by(Order.id)
        ).all()


# ── money formatting ─────────────────────────────────────────────────────────
def test_whole_amounts_lose_the_cents():
    assert core.format_money(4500) == "R45"
    assert core.format_money(4550, "R") == "R45.50"
    assert core.format_money(0, "R") == "R0"


# ── the cart flow, end to end ────────────────────────────────────────────────
def test_a_customer_can_order_over_whatsapp(fake_redis):
    tid = _seed_shop()
    _add_item(tid, "Full House", 4500, prep_minutes=12, sort_order=0)
    _add_item(tid, "Coke 500ml", 1500, prep_minutes=1, category="Drinks",
              sort_order=10)
    client = TestClient(main.app)

    _say(client, "hi")
    assert "Place an order" in _last_body()

    _say(client, "1")                      # place an order
    assert "Our menu" in _last_body()
    assert "Full House" in _last_body()

    _say(client, "1")                      # first menu item
    assert "How many" in _last_body()

    _say(client, "2")                      # two of them
    cart = _last_body()
    assert "2 × Full House" in cart
    assert "R90" in cart, cart

    _say(client, "2")                      # checkout
    assert "special request" in _last_body()

    _say(client, "no")
    confirm = _last_body()
    assert "Confirm your order" in confirm
    assert "Ready around" in confirm

    _say(client, "yes")
    placed = _last_body()
    assert "placed" in placed.lower()

    rows = _orders_for(tid)
    assert len(rows) == 1
    assert rows[0].status == "Placed"
    assert rows[0].total_cents == 9000
    assert rows[0].placed_via == "whatsapp"

    lines = orders.get_order_lines(rows[0].id)
    assert len(lines) == 1
    assert lines[0].qty == 2
    assert lines[0].unit_price_cents == 4500


def test_ordering_the_same_item_twice_adds_up(fake_redis):
    tid = _seed_shop()
    _add_item(tid, "Chips", 2000)
    client = TestClient(main.app)

    _say(client, "hi"); _say(client, "1")
    _say(client, "1"); _say(client, "2")   # 2 chips
    _say(client, "1")                      # add another item
    _say(client, "1"); _say(client, "3")   # 3 more chips

    assert "5 × Chips" in _last_body()


def test_quantity_is_capped(fake_redis):
    tid = _seed_shop()
    _add_item(tid, "Chips", 2000)
    client = TestClient(main.app)

    _say(client, "hi"); _say(client, "1"); _say(client, "1")
    _say(client, "500")

    assert "between" in _last_body()
    _say(client, "1")
    assert "1 × Chips" in _last_body()


def test_starting_over_empties_the_cart(fake_redis):
    tid = _seed_shop()
    _add_item(tid, "Chips", 2000)
    client = TestClient(main.app)

    _say(client, "hi"); _say(client, "1"); _say(client, "1"); _say(client, "2")
    _say(client, "3")                      # start over

    assert "Place an order" in _last_body()
    _say(client, "1"); _say(client, "1"); _say(client, "1")
    assert "1 × Chips" in _last_body(), "the cleared cart came back"


def test_a_greeting_mid_order_keeps_the_cart(fake_redis):
    """Someone typing 'hi' halfway through has not asked to lose their food."""
    tid = _seed_shop()
    _add_item(tid, "Chips", 2000)
    client = TestClient(main.app)

    _say(client, "hi"); _say(client, "1"); _say(client, "1"); _say(client, "2")
    _say(client, "hello")                  # back to the top
    _say(client, "1")                      # place an order again
    _say(client, "1"); _say(client, "1")   # one more chips

    assert "3 × Chips" in _last_body()


def test_a_special_request_is_not_mistaken_for_a_greeting(fake_redis):
    """'no chilli' contains 'hi'. Substring matching would throw the customer
    back to the main menu holding a half-built cart."""
    tid = _seed_shop()
    _add_item(tid, "Kota", 4500)
    client = TestClient(main.app)

    _say(client, "hi"); _say(client, "1"); _say(client, "1"); _say(client, "1")
    _say(client, "2")                       # checkout
    _say(client, "no chilli")               # the note

    assert "Confirm your order" in _last_body()
    _say(client, "yes")
    assert _orders_for(tid)[0].note == "no chilli"


def test_the_shop_refuses_orders_when_closed(fake_redis):
    tid = _seed_shop(opens=8, closes=11)   # frozen clock is 12:00
    _add_item(tid, "Chips", 2000)
    client = TestClient(main.app)

    _say(client, "hi")
    _say(client, "1")

    assert "closed" in _last_body().lower()
    assert _orders_for(tid) == []


def test_an_empty_menu_says_so_rather_than_dead_ending(fake_redis):
    _seed_shop()
    client = TestClient(main.app)

    _say(client, "hi")
    _say(client, "1")

    assert "menu isn't up yet" in _last_body()


# ── pricing ──────────────────────────────────────────────────────────────────
def test_sort_order_decides_which_category_leads():
    """Alphabetical categories would put Drinks above Kotas at a kota shop."""
    tid = _seed_shop()
    _add_item(tid, "Coke", 1500, category="Drinks", sort_order=10)
    _add_item(tid, "Full House", 4500, category="Kotas", sort_order=0)
    _add_item(tid, "Serviette", 100, category="", sort_order=0)

    assert [i.name for i in orders.get_menu(tid)] == ["Full House", "Coke", "Serviette"]


# ── pricing ──────────────────────────────────────────────────────────────────
def test_price_cart_totals_by_line():
    tid = _seed_shop()
    a = _add_item(tid, "Kota", 4500)
    b = _add_item(tid, "Coke", 1500)

    priced = orders.price_cart(tid, [{"menu_item_id": a, "qty": 2},
                                     {"menu_item_id": b, "qty": 1}])

    assert priced["total_cents"] == 4500 * 2 + 1500
    assert priced["dropped"] == []


def test_a_sold_out_item_is_dropped_and_named():
    tid = _seed_shop()
    a = _add_item(tid, "Kota", 4500)
    b = _add_item(tid, "Coke", 1500, is_active=False)

    priced = orders.price_cart(tid, [{"menu_item_id": a, "qty": 1},
                                     {"menu_item_id": b, "qty": 1}])

    assert priced["total_cents"] == 4500
    assert priced["dropped"] == ["Coke"]


def test_a_cart_of_only_sold_out_items_ends_the_order(fake_redis):
    tid = _seed_shop()
    item = _add_item(tid, "Kota", 4500)
    client = TestClient(main.app)

    _say(client, "hi"); _say(client, "1"); _say(client, "1"); _say(client, "1")

    with Session(db.engine) as s:                 # kitchen runs out mid-order
        row = s.get(MenuItem, item)
        row.is_active = False
        s.add(row); s.commit()

    _say(client, "2")                             # checkout

    assert "sold out" in _last_body()
    assert _orders_for(tid) == []


def test_another_tenants_item_cannot_be_priced_onto_this_order():
    """Cart ids come off a customer's session — they must be re-checked against
    the tenant, not trusted."""
    mine  = _seed_shop(number="27820000001")
    yours = _seed_shop(number="27820000002")
    theirs = _add_item(yours, "Their Kota", 4500)

    priced = orders.price_cart(mine, [{"menu_item_id": theirs, "qty": 1}])

    assert priced["lines"] == []
    assert priced["total_cents"] == 0


def test_a_price_change_does_not_rewrite_a_placed_order():
    tid = _seed_shop()
    item = _add_item(tid, "Kota", 4500)
    with Session(db.engine) as s:
        tenant = s.get(Tenant, tid)

    order = orders.place_order(tenant, "2776@s.whatsapp.net", "Thabo",
                               [{"menu_item_id": item, "qty": 2}],
                               order_date=TODAY)

    with Session(db.engine) as s:
        row = s.get(MenuItem, item)
        row.price_cents = 9900          # tomorrow's price
        s.add(row); s.commit()

    with Session(db.engine) as s:
        saved = s.get(Order, order.id)
    assert saved.total_cents == 9000
    assert orders.get_order_lines(order.id)[0].unit_price_cents == 4500


def test_deleting_a_menu_item_leaves_history_intact(fake_redis):
    tid = _seed_shop()
    item = _add_item(tid, "Kota", 4500)
    with Session(db.engine) as s:
        tenant = s.get(Tenant, tid)
    order = orders.place_order(tenant, "2776@s.whatsapp.net", "Thabo",
                               [{"menu_item_id": item, "qty": 1}],
                               order_date=TODAY)

    with Session(db.engine) as s:
        s.delete(s.get(MenuItem, item))
        s.commit()

    lines = orders.get_order_lines(order.id)
    assert lines[0].name == "Kota"
    assert lines[0].unit_price_cents == 4500


# ── ready-time quotes ────────────────────────────────────────────────────────
def test_an_empty_kitchen_quotes_only_the_slowest_item(frozen_clock):
    tid = _seed_shop()
    with Session(db.engine) as s:
        tenant = s.get(Tenant, tid)

    lines = [{"prep_minutes": 12}, {"prep_minutes": 3}]
    ready = orders.estimate_ready_at(tenant, lines, TODAY)

    assert ready == frozen_clock + timedelta(minutes=12)


def test_backlog_is_divided_by_how_many_items_the_kitchen_runs_at_once(frozen_clock):
    """Four pans quoting the raw sum would tell everyone to come back an hour
    late, and they stop believing the quote."""
    tid = _seed_shop(parallel=4)
    item = _add_item(tid, "Kota", 4500, prep_minutes=10)
    with Session(db.engine) as s:
        tenant = s.get(Tenant, tid)

    for _ in range(4):                      # 4 orders × 10 min = 40 min of prep
        orders.place_order(tenant, "2776@s.whatsapp.net", "T",
                           [{"menu_item_id": item, "qty": 1}], order_date=TODAY)

    assert orders.kitchen_backlog_minutes(tenant, TODAY) == 10
    ready = orders.estimate_ready_at(tenant, [{"prep_minutes": 10}], TODAY)
    assert ready == frozen_clock + timedelta(minutes=20)


def test_a_one_pan_kitchen_quotes_the_whole_queue(frozen_clock):
    tid = _seed_shop(parallel=1)
    item = _add_item(tid, "Kota", 4500, prep_minutes=10)
    with Session(db.engine) as s:
        tenant = s.get(Tenant, tid)

    for _ in range(3):
        orders.place_order(tenant, "2776@s.whatsapp.net", "T",
                           [{"menu_item_id": item, "qty": 1}], order_date=TODAY)

    assert orders.kitchen_backlog_minutes(tenant, TODAY) == 30


def test_closed_orders_stop_counting_against_the_kitchen():
    tid = _seed_shop(parallel=1)
    item = _add_item(tid, "Kota", 4500, prep_minutes=10)
    with Session(db.engine) as s:
        tenant = s.get(Tenant, tid)
    placed = orders.place_order(tenant, "2776@s.whatsapp.net", "T",
                                [{"menu_item_id": item, "qty": 1}],
                                order_date=TODAY)

    assert orders.kitchen_backlog_minutes(tenant, TODAY) == 10
    orders.set_status(placed.id, "Collected")
    assert orders.kitchen_backlog_minutes(tenant, TODAY) == 0


def test_quantities_count_towards_the_backlog():
    tid = _seed_shop(parallel=1)
    item = _add_item(tid, "Kota", 4500, prep_minutes=10)
    with Session(db.engine) as s:
        tenant = s.get(Tenant, tid)
    orders.place_order(tenant, "2776@s.whatsapp.net", "T",
                       [{"menu_item_id": item, "qty": 3}], order_date=TODAY)

    assert orders.kitchen_backlog_minutes(tenant, TODAY) == 30


# ── status and cancellation ──────────────────────────────────────────────────
def test_timings_are_stamped_once_each(frozen_clock):
    tid = _seed_shop()
    item = _add_item(tid, "Kota", 4500)
    with Session(db.engine) as s:
        tenant = s.get(Tenant, tid)
    order = orders.place_order(tenant, "2776@s.whatsapp.net", "T",
                               [{"menu_item_id": item, "qty": 1}], order_date=TODAY)

    started = orders.set_status(order.id, "Preparing").started_at
    assert started is not None
    orders.set_status(order.id, "Ready")
    # Bounced back to re-fire an item, then forward again.
    orders.set_status(order.id, "Preparing")
    assert orders.set_status(order.id, "Ready").started_at == started, \
        "prep-time reporting would measure the last mistake, not the work"


def test_a_customer_can_cancel_before_the_kitchen_starts(fake_redis):
    tid = _seed_shop()
    _add_item(tid, "Kota", 4500)
    client = TestClient(main.app)
    _say(client, "hi"); _say(client, "1"); _say(client, "1"); _say(client, "1")
    _say(client, "2"); _say(client, "no"); _say(client, "yes")

    _say(client, "menu")
    _say(client, "3")                       # cancel my order

    assert "cancelled" in _last_body().lower()
    row = _orders_for(tid)[0]
    assert row.status == "Cancelled"
    assert row.closed_by == "customer"


def test_a_customer_cannot_cancel_food_already_being_made(fake_redis):
    tid = _seed_shop()
    _add_item(tid, "Kota", 4500)
    client = TestClient(main.app)
    _say(client, "hi"); _say(client, "1"); _say(client, "1"); _say(client, "1")
    _say(client, "2"); _say(client, "no"); _say(client, "yes")

    order_id = _orders_for(tid)[0].id
    orders.set_status(order_id, "Preparing")

    _say(client, "menu")
    _say(client, "3")

    assert "counter" in _last_body()
    assert _orders_for(tid)[0].status == "Preparing"


def test_status_request_reports_the_order(fake_redis):
    tid = _seed_shop()
    _add_item(tid, "Kota", 4500)
    client = TestClient(main.app)
    _say(client, "hi"); _say(client, "1"); _say(client, "1"); _say(client, "1")
    _say(client, "2"); _say(client, "no"); _say(client, "yes")

    _say(client, "menu")
    _say(client, "2")                       # my order

    body = _last_body()
    assert "1 × Kota" in body
    assert "R45" in body


def test_status_request_without_an_order_points_back_at_the_menu(fake_redis):
    _seed_shop()
    client = TestClient(main.app)
    _say(client, "hi")
    _say(client, "2")

    assert "don't have an order" in _last_body()


# ── the queue bot is untouched ───────────────────────────────────────────────
def test_a_queue_tenant_still_gets_the_queue_bot(fake_redis):
    _seed_shop(mode="queue", number="27820000009")
    client = TestClient(main.app)

    _say(client, "hi", sender="27820000009")

    body = _last_body()
    assert "Join the queue" in body
    assert "Place an order" not in body


# ── admin: menu ──────────────────────────────────────────────────────────────
def _headers(token):
    return {"Authorization": f"Bearer {token}"}


def test_menu_crud(super_token):
    tid = _seed_shop()
    client = TestClient(main.app)
    h = _headers(super_token)

    created = client.post("/admin/menu", headers=h, json={
        "tenant_id": tid, "name": "Kota", "price_cents": 4500,
        "prep_minutes": 12, "category": "Kotas"})
    assert created.status_code == 200, created.text
    item_id = created.json()["id"]

    listed = client.get(f"/admin/menu/{tid}", headers=h).json()
    assert [i["name"] for i in listed] == ["Kota"]

    off = client.patch(f"/admin/menu/{item_id}", headers=h,
                       json={"is_active": False})
    assert off.json()["is_active"] is False

    assert client.delete(f"/admin/menu/{item_id}", headers=h).status_code == 200
    assert client.get(f"/admin/menu/{tid}", headers=h).json() == []


def test_a_negative_price_is_refused(super_token):
    tid = _seed_shop()
    client = TestClient(main.app)
    r = client.post("/admin/menu", headers=_headers(super_token), json={
        "tenant_id": tid, "name": "Free lunch", "price_cents": -100})
    assert r.status_code == 400


def test_inactive_items_still_show_on_the_dashboard(super_token):
    """Staff need to see what's sold out in order to switch it back on."""
    tid = _seed_shop()
    _add_item(tid, "Kota", 4500, is_active=False)
    client = TestClient(main.app)

    listed = client.get(f"/admin/menu/{tid}", headers=_headers(super_token)).json()

    assert [i["name"] for i in listed] == ["Kota"]
    assert orders.get_menu(tid) == [], "the bot must not offer a sold-out item"


# ── admin: the kitchen board ─────────────────────────────────────────────────
def test_the_board_lists_todays_orders_with_their_lines(super_token):
    tid = _seed_shop()
    item = _add_item(tid, "Kota", 4500)
    with Session(db.engine) as s:
        tenant = s.get(Tenant, tid)
    orders.place_order(tenant, "2776@s.whatsapp.net", "Thabo",
                       [{"menu_item_id": item, "qty": 2}], order_date=TODAY)
    client = TestClient(main.app)

    board = client.get(f"/admin/orders/{tid}?order_date={TODAY}",
                       headers=_headers(super_token)).json()

    assert board["summary"]["open"] == 1
    assert len(board["orders"]) == 1
    row = board["orders"][0]
    assert row["code"] and len(row["code"]) == 3
    assert row["items"][0]["qty"] == 2
    assert row["items"][0]["line_total_cents"] == 9000


def test_takings_count_collected_orders_only(super_token):
    tid = _seed_shop()
    item = _add_item(tid, "Kota", 4500)
    with Session(db.engine) as s:
        tenant = s.get(Tenant, tid)
    kept      = orders.place_order(tenant, "a@s.whatsapp.net", "A",
                                   [{"menu_item_id": item, "qty": 1}], order_date=TODAY)
    scrapped  = orders.place_order(tenant, "b@s.whatsapp.net", "B",
                                   [{"menu_item_id": item, "qty": 1}], order_date=TODAY)
    orders.place_order(tenant, "c@s.whatsapp.net", "C",
                       [{"menu_item_id": item, "qty": 1}], order_date=TODAY)
    orders.set_status(kept.id, "Collected")
    orders.set_status(scrapped.id, "Cancelled")
    client = TestClient(main.app)

    summary = client.get(f"/admin/orders/{tid}?order_date={TODAY}",
                         headers=_headers(super_token)).json()["summary"]

    assert summary["takings_cents"] == 4500, "unpaid or cancelled food was booked as money"
    assert summary["open"] == 1
    assert summary["cancelled"] == 1


def test_marking_ready_messages_the_customer_once(super_token):
    tid = _seed_shop()
    item = _add_item(tid, "Kota", 4500)
    with Session(db.engine) as s:
        tenant = s.get(Tenant, tid)
    order = orders.place_order(tenant, "2776@s.whatsapp.net", "Thabo",
                               [{"menu_item_id": item, "qty": 1}], order_date=TODAY)
    client = TestClient(main.app)
    h = _headers(super_token)

    client.patch(f"/admin/orders/{order.id}/status", headers=h, json={"status": "Ready"})
    client.patch(f"/admin/orders/{order.id}/status", headers=h, json={"status": "Preparing"})
    client.patch(f"/admin/orders/{order.id}/status", headers=h, json={"status": "Ready"})

    ready_shouts = [b for b in _sent_bodies(tid) if "is ready" in b]
    assert len(ready_shouts) == 1, "the customer was shouted at twice"


def test_a_counter_order_needs_no_whatsapp_number(super_token):
    tid = _seed_shop()
    item = _add_item(tid, "Kota", 4500)
    client = TestClient(main.app)

    r = client.post("/admin/orders", headers=_headers(super_token), json={
        "tenant_id": tid, "customer_name": "Sipho",
        "items": [{"menu_item_id": item, "qty": 3}]})

    assert r.status_code == 200, r.text
    assert r.json()["total_cents"] == 13500
    assert r.json()["placed_via"] == "counter"
    assert _sent_bodies(tid) == [], "there is nobody to message"


def test_a_counter_order_with_a_phone_number_gets_the_ready_shout(super_token):
    tid = _seed_shop()
    item = _add_item(tid, "Kota", 4500)
    client = TestClient(main.app)
    h = _headers(super_token)

    order = client.post("/admin/orders", headers=h, json={
        "tenant_id": tid, "customer_name": "Sipho", "customer_phone": "0764519653",
        "items": [{"menu_item_id": item, "qty": 1}]}).json()
    client.patch(f"/admin/orders/{order['id']}/status", headers=h,
                 json={"status": "Ready"})

    assert any("is ready" in b for b in _sent_bodies(tid))


def test_an_empty_counter_order_is_refused(super_token):
    tid = _seed_shop()
    client = TestClient(main.app)
    r = client.post("/admin/orders", headers=_headers(super_token),
                    json={"tenant_id": tid, "items": []})
    assert r.status_code == 400


def test_an_unknown_status_is_refused(super_token):
    tid = _seed_shop()
    item = _add_item(tid, "Kota", 4500)
    with Session(db.engine) as s:
        tenant = s.get(Tenant, tid)
    order = orders.place_order(tenant, "2776@s.whatsapp.net", "T",
                               [{"menu_item_id": item, "qty": 1}], order_date=TODAY)
    client = TestClient(main.app)

    r = client.patch(f"/admin/orders/{order.id}/status",
                     headers=_headers(super_token), json={"status": "Burnt"})

    assert r.status_code == 400


# ── tenant isolation ─────────────────────────────────────────────────────────
def _tenant_login(client, super_token, tenant_id, email):
    client.post("/admin/users", headers=_headers(super_token),
                json={"email": email, "password": "ownerpass1", "tenant_id": tenant_id})
    r = client.post("/auth/login", json={"email": email, "password": "ownerpass1"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def test_one_shop_cannot_read_anothers_menu_or_orders(super_token):
    mine  = _seed_shop(number="27820000011")
    yours = _seed_shop(number="27820000012")
    _add_item(yours, "Their Kota", 4500)
    client = TestClient(main.app)
    token = _tenant_login(client, super_token, mine, "owner@kasi.com")
    h = _headers(token)

    assert client.get(f"/admin/menu/{yours}", headers=h).status_code == 403
    assert client.get(f"/admin/orders/{yours}", headers=h).status_code == 403
    assert client.get(f"/admin/menu/{mine}", headers=h).status_code == 200


def test_one_shop_cannot_add_items_to_anothers_menu(super_token):
    mine  = _seed_shop(number="27820000013")
    yours = _seed_shop(number="27820000014")
    client = TestClient(main.app)
    token = _tenant_login(client, super_token, mine, "owner2@kasi.com")

    r = client.post("/admin/menu", headers=_headers(token), json={
        "tenant_id": yours, "name": "Sabotage", "price_cents": 1})

    assert r.status_code == 403


def test_one_shop_cannot_touch_anothers_order(super_token):
    mine  = _seed_shop(number="27820000015")
    yours = _seed_shop(number="27820000016")
    item  = _add_item(yours, "Their Kota", 4500)
    with Session(db.engine) as s:
        tenant = s.get(Tenant, yours)
    order = orders.place_order(tenant, "2776@s.whatsapp.net", "T",
                               [{"menu_item_id": item, "qty": 1}], order_date=TODAY)
    client = TestClient(main.app)
    token = _tenant_login(client, super_token, mine, "owner3@kasi.com")

    r = client.patch(f"/admin/orders/{order.id}/status",
                     headers=_headers(token), json={"status": "Cancelled"})

    assert r.status_code == 403
    assert _orders_for(yours)[0].status == "Placed"


def test_mode_must_be_one_the_bot_can_actually_run(super_token):
    client = TestClient(main.app)
    r = client.post("/admin/tenants", headers=_headers(super_token), json={
        "business_name": "Confused", "whatsapp_number": "27820000099",
        "evolution_instance": "i", "evolution_api_key": "k",
        "evolution_api_url": "http://x", "mode": "telepathy"})
    assert r.status_code == 400


def test_a_tenant_created_without_a_mode_is_a_queue_tenant(super_token):
    """Existing onboarding does not send `mode`, and must keep working."""
    client = TestClient(main.app)
    r = client.post("/admin/tenants", headers=_headers(super_token), json={
        "business_name": "Salon", "whatsapp_number": "27820000098",
        "evolution_instance": "i", "evolution_api_key": "k",
        "evolution_api_url": "http://x"})
    assert r.json()["mode"] == "queue"


# ── midnight sweep ───────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_the_midnight_sweep_closes_yesterdays_open_orders():
    tid = _seed_shop()
    item = _add_item(tid, "Kota", 4500)
    with Session(db.engine) as s:
        tenant = s.get(Tenant, tid)
    yesterday = core.yesterday_str()
    stale = orders.place_order(tenant, "2776@s.whatsapp.net", "T",
                               [{"menu_item_id": item, "qty": 1}],
                               order_date=yesterday)
    today = orders.place_order(tenant, "2776@s.whatsapp.net", "T",
                               [{"menu_item_id": item, "qty": 1}],
                               order_date=core.today_str())

    await jobs.midnight_reset_job()

    with Session(db.engine) as s:
        assert s.get(Order, stale.id).status == "Cancelled"
        assert s.get(Order, stale.id).closed_by == "system", \
            "reporting must not read this as a customer walking away"
        assert s.get(Order, today.id).status == "Placed"


@pytest.mark.asyncio
async def test_the_sweep_never_books_uncollected_food_as_money():
    tid = _seed_shop()
    item = _add_item(tid, "Kota", 4500)
    with Session(db.engine) as s:
        tenant = s.get(Tenant, tid)
    ready = orders.place_order(tenant, "2776@s.whatsapp.net", "T",
                               [{"menu_item_id": item, "qty": 1}],
                               order_date=core.yesterday_str())
    orders.set_status(ready.id, "Ready")

    await jobs.midnight_reset_job()

    with Session(db.engine) as s:
        assert s.get(Order, ready.id).status == "Cancelled"
