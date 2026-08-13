"""
The midnight sweep, over a date range rather than yesterday alone.

The job closes what staff left open. It used to look at exactly one date, so a
process down across two midnights left the older day open forever — entries
still Waiting, still occupying their agent in every backlog calculation, and
never reaching reporting as closed.

The tests that matter most here are the ones about what the sweep must NOT
touch: today's live queue, and anything booked for a future date.
"""
from datetime import datetime, timedelta

import pytest
from sqlmodel import Session

import main  # noqa: F401 — binds the test engine before app modules load
from app import config, core, db, jobs
from app.models import Tenant, Service, Agent, QueueEntry, Order


def _days_ago(n: int) -> str:
    return (core.now().date() - timedelta(days=n)).isoformat()


def _days_ahead(n: int) -> str:
    return (core.now().date() + timedelta(days=n)).isoformat()


def _seed_tenant():
    with Session(db.engine) as s:
        t = Tenant(
            business_name="Test Co", whatsapp_number="27810000000",
            evolution_instance="i", evolution_api_key="k",
            evolution_api_url="http://x",
        )
        s.add(t); s.commit(); s.refresh(t)
        svc = Service(tenant_id=t.id, name="Cut", duration_minutes=60)
        s.add(svc); s.commit(); s.refresh(svc)
        a = Agent(tenant_id=t.id, name="A0")
        s.add(a); s.commit(); s.refresh(a)
        return t.id, svc.id, a.id


def _add_entry(tenant_id, svc_id, agent_id, date, status="Waiting", name="C"):
    with Session(db.engine) as s:
        e = QueueEntry(
            tenant_id=tenant_id, service_id=svc_id, agent_id=agent_id,
            customer_number="2781@s.whatsapp.net", customer_name=name,
            status=status, queue_date=date,
            estimated_start=datetime(2026, 6, 20, 9, 0),
        )
        s.add(e); s.commit(); s.refresh(e)
        return e.id


def _add_order(tenant_id, date, status="Placed"):
    with Session(db.engine) as s:
        o = Order(tenant_id=tenant_id, customer_number="2781@s.whatsapp.net",
                  customer_name="C", status=status, order_date=date,
                  total_cents=4500)
        s.add(o); s.commit(); s.refresh(o)
        return o.id


def _status(model, row_id):
    with Session(db.engine) as s:
        return s.get(model, row_id).status


# ── the range, not just yesterday ────────────────────────────────────────────
@pytest.mark.asyncio
async def test_the_sweep_closes_entries_older_than_yesterday():
    """A process down across two midnights used to strand the older day."""
    tid, svc, aid = _seed_tenant()
    old       = _add_entry(tid, svc, aid, _days_ago(3), name="Three days ago")
    yesterday = _add_entry(tid, svc, aid, _days_ago(1), name="Yesterday")

    await jobs.midnight_reset_job()

    assert _status(QueueEntry, old) == "NoShow", \
        "an entry from three days ago stayed Waiting forever"
    assert _status(QueueEntry, yesterday) == "NoShow"


@pytest.mark.asyncio
async def test_the_sweep_closes_orders_older_than_yesterday():
    tid, _, _ = _seed_tenant()
    old = _add_order(tid, _days_ago(4))

    await jobs.midnight_reset_job()

    assert _status(Order, old) == "Cancelled"


@pytest.mark.asyncio
async def test_swept_rows_are_still_tagged_as_ours():
    """Provenance is what keeps these out of the real no-show rate."""
    tid, svc, aid = _seed_tenant()
    entry = _add_entry(tid, svc, aid, _days_ago(3))
    order = _add_order(tid, _days_ago(3))

    await jobs.midnight_reset_job()

    with Session(db.engine) as s:
        swept_entry = s.get(QueueEntry, entry)
        swept_order = s.get(Order, order)
    assert swept_entry.closed_by == "system"
    assert swept_entry.finished_at is not None
    assert swept_order.closed_by == "system"
    assert swept_order.finished_at is not None


# ── what the sweep must not touch ────────────────────────────────────────────
@pytest.mark.asyncio
async def test_the_sweep_leaves_today_alone():
    tid, svc, aid = _seed_tenant()
    today_entry = _add_entry(tid, svc, aid, core.today_str())
    today_order = _add_order(tid, core.today_str())

    await jobs.midnight_reset_job()

    assert _status(QueueEntry, today_entry) == "Waiting", \
        "the shop is still working today"
    assert _status(Order, today_order) == "Placed"


@pytest.mark.asyncio
async def test_the_sweep_never_touches_a_future_booking():
    """
    The reason the range is `< today` and not merely "everything old". Once a
    business books further ahead than today, a sweep that reached forward would
    quietly cancel appointments nobody has attended yet.
    """
    tid, svc, aid = _seed_tenant()
    tomorrow  = _add_entry(tid, svc, aid, _days_ahead(1), name="Tomorrow")
    next_week = _add_entry(tid, svc, aid, _days_ahead(7), name="Next week")

    await jobs.midnight_reset_job()

    assert _status(QueueEntry, tomorrow) == "Waiting"
    assert _status(QueueEntry, next_week) == "Waiting"


@pytest.mark.asyncio
async def test_a_blank_date_is_left_alone():
    """
    A blank date sorts before every real one, so a range sweep would close rows
    whose day is simply unknown. The old equality sweep never touched them and
    neither should this one.
    """
    tid, _, _ = _seed_tenant()
    undated = _add_order(tid, "")

    await jobs.midnight_reset_job()

    assert _status(Order, undated) == "Placed"


@pytest.mark.asyncio
async def test_already_closed_rows_are_not_reopened_or_retagged():
    tid, svc, aid = _seed_tenant()
    done = _add_entry(tid, svc, aid, _days_ago(3), status="Done")

    await jobs.midnight_reset_job()

    with Session(db.engine) as s:
        row = s.get(QueueEntry, done)
    assert row.status == "Done"
    assert row.closed_by == "", "real completed work must not be tagged system"


# ── batching ─────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_the_sweep_drains_more_rows_than_one_batch(monkeypatch):
    """
    The first pass after an outage can face far more than one night's worth, so
    the sweep batches. Batching that stopped after one pass would leave the
    remainder open until the next night — and the night after that, forever.
    """
    monkeypatch.setattr(config, "SWEEP_BATCH", 2)
    tid, svc, aid = _seed_tenant()
    ids = [_add_entry(tid, svc, aid, _days_ago(2), name=f"C{i}") for i in range(5)]

    await jobs.midnight_reset_job()

    assert all(_status(QueueEntry, i) == "NoShow" for i in ids), \
        "a batched sweep must loop until the table is drained"
