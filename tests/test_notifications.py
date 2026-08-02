"""
Durable outbound delivery: the outbox, and notifications derived from queue
state rather than from scheduled timers.

Nothing here touches the network — the outbox is drained against a fake httpx
client so retry and give-up behaviour can be driven deterministically.
"""
import asyncio
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

import main
from app import config, core, db, jobs, messaging
from app.models import Tenant, Service, Agent, AgentService, QueueEntry, OutboxMessage

QUEUE_DATE = "2026-06-20"


# ── fakes ────────────────────────────────────────────────────────────────────
class _FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _AsyncCtx:
    """Wraps a FakeClient so it works with `async with httpx.AsyncClient()`."""

    def __init__(self, client):
        self.client = client

    async def __aenter__(self):
        return self.client

    async def __aexit__(self, *exc):
        return False


class FakeClient:
    """Stands in for httpx.AsyncClient. Records calls, fails on demand."""

    def __init__(self, fail_times=0, status_code=200):
        self.calls = []
        self.fail_times = fail_times
        self.status_code = status_code

    async def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("connection refused")
        return _FakeResponse(self.status_code)


# ── helpers ──────────────────────────────────────────────────────────────────
def _seed(duration_minutes=60):
    with Session(db.engine) as s:
        t = Tenant(
            business_name="Test Co", whatsapp_number="27810000000",
            evolution_instance="inst", evolution_api_key="key",
            evolution_api_url="http://evo.local/",
            queue_opens=8, queue_closes=17, advance_days=0,
        )
        s.add(t); s.commit(); s.refresh(t)
        svc = Service(tenant_id=t.id, name="Cut", duration_minutes=duration_minutes)
        s.add(svc); s.commit(); s.refresh(svc)
        a = Agent(tenant_id=t.id, name="Nomsa")
        s.add(a); s.commit(); s.refresh(a)
        s.add(AgentService(agent_id=a.id, service_id=svc.id)); s.commit()
        return t.id, svc.id, a.id


def _tenant(tenant_id):
    with Session(db.engine) as s:
        return s.get(Tenant, tenant_id)


def _entry(tenant_id, svc_id, agent_id, name, status="Waiting",
           estimated_start=None, number="27820000001@s.whatsapp.net"):
    with Session(db.engine) as s:
        e = QueueEntry(
            tenant_id=tenant_id, service_id=svc_id, agent_id=agent_id,
            customer_number=number, customer_name=name, status=status,
            queue_date=QUEUE_DATE, estimated_start=estimated_start,
        )
        s.add(e); s.commit(); s.refresh(e)
        return e.id


def _outbox():
    with Session(db.engine) as s:
        return s.exec(select(OutboxMessage).order_by(OutboxMessage.id)).all()


def _freeze(monkeypatch, when: datetime):
    monkeypatch.setattr(core, "now", lambda: when)


# ── send_text enqueues ───────────────────────────────────────────────────────
def test_send_text_queues_instead_of_sending():
    tid, _, _ = _seed()
    messaging.send_text(_tenant(tid), "27820000001", "hello")
    rows = _outbox()
    assert len(rows) == 1
    assert rows[0].status == "Pending"
    assert rows[0].body == "hello"
    assert rows[0].attempts == 0


def test_send_text_ignores_empty_number():
    tid, _, _ = _seed()
    messaging.send_text(_tenant(tid), "", "hello")
    assert _outbox() == []


def test_dedupe_key_suppresses_second_enqueue():
    tid, _, _ = _seed()
    t = _tenant(tid)
    messaging.send_text(t, "27820000001", "you're next", dedupe_key="youre_next:1")
    messaging.send_text(t, "27820000001", "you're next", dedupe_key="youre_next:1")
    assert len(_outbox()) == 1


def test_no_dedupe_key_allows_repeats():
    """The same menu text legitimately repeats in a conversation."""
    tid, _, _ = _seed()
    t = _tenant(tid)
    messaging.send_text(t, "27820000001", "main menu")
    messaging.send_text(t, "27820000001", "main menu")
    assert len(_outbox()) == 2


# ── draining ─────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_drain_sends_and_marks_sent():
    tid, _, _ = _seed()
    messaging.send_text(_tenant(tid), "27820000001", "hello")
    client = FakeClient()
    sent = await messaging.drain_outbox_once(client)

    assert sent == 1
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["url"] == "http://evo.local/message/sendText/inst"
    assert call["json"] == {"number": "27820000001", "text": "hello"}
    assert call["headers"]["apikey"] == "key"

    row = _outbox()[0]
    assert row.status == "Sent"
    assert row.sent_at is not None


@pytest.mark.asyncio
async def test_drain_preserves_queue_order():
    """Consecutive bot replies must arrive in the order they were queued."""
    tid, _, _ = _seed()
    t = _tenant(tid)
    for body in ("first", "second", "third"):
        messaging.send_text(t, "27820000001", body)
    client = FakeClient()
    await messaging.drain_outbox_once(client)
    assert [c["json"]["text"] for c in client.calls] == ["first", "second", "third"]


@pytest.mark.asyncio
async def test_failed_send_is_retried_with_backoff(monkeypatch):
    base = datetime(2026, 6, 20, 10, 0)
    _freeze(monkeypatch, base)
    tid, _, _ = _seed()
    messaging.send_text(_tenant(tid), "27820000001", "hello")

    await messaging.drain_outbox_once(FakeClient(fail_times=1))
    row = _outbox()[0]
    assert row.status == "Pending"          # still queued, not lost
    assert row.attempts == 1
    assert row.next_attempt_at > base       # backed off
    assert "connection refused" in row.last_error


@pytest.mark.asyncio
async def test_backed_off_message_is_skipped_until_due(monkeypatch):
    base = datetime(2026, 6, 20, 10, 0)
    _freeze(monkeypatch, base)
    tid, _, _ = _seed()
    messaging.send_text(_tenant(tid), "27820000001", "hello")
    await messaging.drain_outbox_once(FakeClient(fail_times=1))

    # Immediately after, it is not yet due.
    client = FakeClient()
    assert await messaging.drain_outbox_once(client) == 0
    assert client.calls == []

    # Once the backoff elapses it goes out.
    _freeze(monkeypatch, base + timedelta(minutes=10))
    client = FakeClient()
    assert await messaging.drain_outbox_once(client) == 1
    assert _outbox()[0].status == "Sent"


@pytest.mark.asyncio
async def test_gives_up_after_max_attempts(monkeypatch):
    base = datetime(2026, 6, 20, 10, 0)
    _freeze(monkeypatch, base)
    tid, _, _ = _seed()
    messaging.send_text(_tenant(tid), "27820000001", "hello")

    for i in range(config.OUTBOX_MAX_ATTEMPTS):
        _freeze(monkeypatch, base + timedelta(hours=i))
        await messaging.drain_outbox_once(FakeClient(fail_times=1))

    row = _outbox()[0]
    assert row.status == "Failed"
    assert row.attempts == config.OUTBOX_MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_one_bad_message_does_not_block_the_rest():
    """A tenant whose Evolution is down must not stall everyone else's queue."""
    tid, _, _ = _seed()
    t = _tenant(tid)
    messaging.send_text(t, "27820000001", "will fail")
    messaging.send_text(t, "27820000002", "must still go")

    client = FakeClient(fail_times=1)   # only the first call raises
    sent = await messaging.drain_outbox_once(client)

    assert sent == 1
    rows = _outbox()
    assert rows[0].status == "Pending"   # retried later
    assert rows[1].status == "Sent"      # not head-of-line blocked


@pytest.mark.asyncio
async def test_http_error_status_counts_as_failure():
    tid, _, _ = _seed()
    messaging.send_text(_tenant(tid), "27820000001", "hello")
    await messaging.drain_outbox_once(FakeClient(status_code=500))
    row = _outbox()[0]
    assert row.status == "Pending"
    assert row.attempts == 1


# ── atomic claim ─────────────────────────────────────────────────────────────
def test_claim_notification_succeeds_once_then_fails():
    tid, svc, aid = _seed()
    eid = _entry(tid, svc, aid, "Thabo")
    assert jobs._claim_notification(eid, "notified_next") is True
    assert jobs._claim_notification(eid, "notified_next") is False


def test_claim_notification_rejects_unknown_flag():
    """The flag name is interpolated into SQL — only the two known ones pass."""
    tid, svc, aid = _seed()
    eid = _entry(tid, svc, aid, "Thabo")
    with pytest.raises(ValueError):
        jobs._claim_notification(eid, "status = 'Done' --")


# ── reconciler ───────────────────────────────────────────────────────────────
def test_reconciler_fires_warning_when_service_nearly_done(monkeypatch):
    tid, svc, aid = _seed(duration_minutes=60)
    start = datetime(2026, 6, 20, 9, 0)
    _entry(tid, svc, aid, "Being served", status="InService", estimated_start=start)
    waiter = _entry(tid, svc, aid, "Next up", estimated_start=start + timedelta(hours=1))

    # 50 minutes in: 10 minutes left, inside the 15-minute window.
    _freeze(monkeypatch, start + timedelta(minutes=50))
    assert jobs.reconcile_notifications() == 1

    rows = _outbox()
    assert len(rows) == 1
    assert "Almost your turn" in rows[0].body
    assert rows[0].dedupe_key == f"two_away:{waiter}"
    with Session(db.engine) as s:
        assert s.get(QueueEntry, waiter).notified_two_away is True


def test_reconciler_silent_when_not_yet_due(monkeypatch):
    tid, svc, aid = _seed(duration_minutes=60)
    start = datetime(2026, 6, 20, 9, 0)
    _entry(tid, svc, aid, "Being served", status="InService", estimated_start=start)
    _entry(tid, svc, aid, "Next up")

    _freeze(monkeypatch, start + timedelta(minutes=20))   # 40 min left
    assert jobs.reconcile_notifications() == 0
    assert _outbox() == []


def test_reconciler_does_not_repeat_itself(monkeypatch):
    """Ticking every minute must not send the warning every minute."""
    tid, svc, aid = _seed(duration_minutes=60)
    start = datetime(2026, 6, 20, 9, 0)
    _entry(tid, svc, aid, "Being served", status="InService", estimated_start=start)
    _entry(tid, svc, aid, "Next up")

    _freeze(monkeypatch, start + timedelta(minutes=50))
    jobs.reconcile_notifications()
    jobs.reconcile_notifications()
    jobs.reconcile_notifications()
    assert len(_outbox()) == 1


def test_reconciler_survives_a_restart(monkeypatch):
    """
    The point of deriving from state: nothing is scheduled, so a process that
    was down through the moment the warning was due still sends it on the next
    tick — one tick late instead of never.
    """
    tid, svc, aid = _seed(duration_minutes=60)
    start = datetime(2026, 6, 20, 9, 0)
    _entry(tid, svc, aid, "Being served", status="InService", estimated_start=start)
    _entry(tid, svc, aid, "Next up")

    # No tick happened at minute 45. First tick after "restart" is at minute 58.
    _freeze(monkeypatch, start + timedelta(minutes=58))
    assert jobs.reconcile_notifications() == 1
    assert "Almost your turn" in _outbox()[0].body


def test_reconciler_skips_walkin_without_phone(monkeypatch):
    tid, svc, aid = _seed(duration_minutes=60)
    start = datetime(2026, 6, 20, 9, 0)
    _entry(tid, svc, aid, "Being served", status="InService", estimated_start=start)
    with Session(db.engine) as s:
        e = QueueEntry(
            tenant_id=tid, service_id=svc, agent_id=aid,
            customer_number="walkin", customer_name="No phone",
            booked_via="walkin", customer_phone="", status="Waiting",
            queue_date=QUEUE_DATE,
        )
        s.add(e); s.commit()

    _freeze(monkeypatch, start + timedelta(minutes=50))
    jobs.reconcile_notifications()
    assert _outbox() == []


# ── you're next ──────────────────────────────────────────────────────────────
def test_youre_next_queues_once_and_suppresses_the_warning(monkeypatch):
    tid, svc, aid = _seed()
    waiter = _entry(tid, svc, aid, "Next up")

    jobs._fire_youre_next(tid, aid, QUEUE_DATE)
    jobs._fire_youre_next(tid, aid, QUEUE_DATE)   # second call is a no-op

    rows = _outbox()
    assert len(rows) == 1
    assert "up next" in rows[0].body
    with Session(db.engine) as s:
        e = s.get(QueueEntry, waiter)
        # Both flags claimed, so the reconciler won't follow up with a warning.
        assert e.notified_next is True
        assert e.notified_two_away is True

    _freeze(monkeypatch, datetime(2026, 6, 20, 23, 0))
    assert jobs.reconcile_notifications() == 0


def test_youre_next_noop_when_queue_empty():
    tid, svc, aid = _seed()
    jobs._fire_youre_next(tid, aid, QUEUE_DATE)
    assert _outbox() == []


# ── worker loop ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_worker_drains_queued_messages(monkeypatch):
    tid, _, _ = _seed()
    messaging.send_text(_tenant(tid), "27820000001", "hello")

    client = FakeClient()
    monkeypatch.setattr(messaging.httpx, "AsyncClient", lambda *a, **k: _AsyncCtx(client))
    monkeypatch.setattr(config, "OUTBOX_POLL_SECONDS", 0.01)

    task = asyncio.create_task(messaging.outbox_worker())
    for _ in range(50):                      # give it a few ticks
        await asyncio.sleep(0.01)
        if _outbox()[0].status == "Sent":
            break
    task.cancel()

    assert _outbox()[0].status == "Sent"
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_worker_survives_a_failing_pass(monkeypatch):
    """A bad pass must not kill the loop — that would stop every message."""
    calls = {"n": 0}

    async def flaky(_client):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return 0

    monkeypatch.setattr(messaging, "drain_outbox_once", flaky)
    monkeypatch.setattr(messaging.httpx, "AsyncClient", lambda *a, **k: _AsyncCtx(FakeClient()))
    monkeypatch.setattr(config, "OUTBOX_POLL_SECONDS", 0.01)

    task = asyncio.create_task(messaging.outbox_worker())
    for _ in range(50):
        await asyncio.sleep(0.01)
        if calls["n"] >= 3:
            break
    task.cancel()

    assert calls["n"] >= 3   # kept ticking after the exception


# ── health ───────────────────────────────────────────────────────────────────
def test_health_reports_outbox_depth():
    tid, _, _ = _seed()
    messaging.send_text(_tenant(tid), "27820000001", "hello")
    body = TestClient(main.app).get("/health").json()
    assert body["outbox_pending"] == 1
    assert body["outbox_failed"] == 0
