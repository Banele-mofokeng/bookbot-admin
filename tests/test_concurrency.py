"""
Query counts and the booking lock.

Two separate defects, both invisible to a correctness-only test suite:

1. N+1 queries. Every queue-engine walk fetched each entry's Service with its
   own round trip, and assign_agent opened a fresh Session per candidate agent.
   The results were right, just paid for per row. So these tests assert on
   *query count* and specifically that it does not grow with the number of
   rows or agents — an assertion that fails the moment an N+1 comes back.

2. Read-then-write assignment. assign_agent measures backlogs, the caller then
   inserts a row that changes them. Concurrent bookings inside that window both
   land on the same agent. Tests here drive booking_lock directly with a fake
   Redis, because a real thread race is not reproducible on demand.
"""
import threading
import time as real_time
from contextlib import contextmanager
from datetime import datetime, timedelta

import pytest
from sqlalchemy import event
from sqlmodel import Session, select

import main
from app import config, core, db, queue_engine, sessions
from app.models import Tenant, Service, Agent, AgentService, QueueEntry


DATE = "2026-06-20"


# ── helpers ──────────────────────────────────────────────────────────────────
@contextmanager
def count_queries():
    """Count statements issued on the app engine inside the block."""
    box = {"n": 0, "sql": []}

    def before(conn, cursor, statement, params, context, executemany):
        box["n"] += 1
        box["sql"].append(statement)

    event.listen(db.engine, "before_cursor_execute", before)
    try:
        yield box
    finally:
        event.remove(db.engine, "before_cursor_execute", before)


def _seed(n_agents=1, n_services=1):
    """Tenant + agents, every agent able to do every service."""
    with Session(db.engine) as s:
        t = Tenant(
            business_name="Test Co", whatsapp_number="27810000000",
            evolution_instance="i", evolution_api_key="k",
            evolution_api_url="http://x",
            queue_opens=8, queue_closes=17, advance_days=0,
        )
        s.add(t); s.commit(); s.refresh(t)

        svc_ids = []
        for i in range(n_services):
            sv = Service(tenant_id=t.id, name=f"S{i}", duration_minutes=30 + i)
            s.add(sv); s.commit(); s.refresh(sv)
            svc_ids.append(sv.id)

        agent_ids = []
        for i in range(n_agents):
            a = Agent(tenant_id=t.id, name=f"A{i}")
            s.add(a); s.commit(); s.refresh(a)
            for sid in svc_ids:
                s.add(AgentService(agent_id=a.id, service_id=sid))
            s.commit()
            agent_ids.append(a.id)

        return t.id, svc_ids, agent_ids


def _tenant(tenant_id):
    with Session(db.engine) as s:
        return s.get(Tenant, tenant_id)


def _fill(tenant_id, svc_ids, agent_ids, per_agent):
    """per_agent Waiting entries for each agent, cycling through services."""
    with Session(db.engine) as s:
        n = 0
        for aid in agent_ids:
            for i in range(per_agent):
                s.add(QueueEntry(
                    tenant_id=tenant_id, service_id=svc_ids[i % len(svc_ids)],
                    agent_id=aid, customer_number=f"2781{n}",
                    customer_name=f"C{n}", status="Waiting", queue_date=DATE,
                    estimated_start=datetime(2026, 6, 20, 9, 0),
                    joined_at=datetime(2026, 6, 20, 8, 0) + timedelta(minutes=n),
                ))
                n += 1
        s.commit()


# =============================================================================
# N+1 — query count must not scale with rows
# =============================================================================
def test_backlog_query_count_is_flat_in_entries():
    tid, svcs, [aid] = _seed(n_agents=1, n_services=3)

    _fill(tid, svcs, [aid], per_agent=2)
    with count_queries() as small:
        queue_engine.get_agent_backlog_minutes(aid, tid, DATE)

    _fill(tid, svcs, [aid], per_agent=10)
    with count_queries() as big:
        queue_engine.get_agent_backlog_minutes(aid, tid, DATE)

    assert small["n"] == big["n"], (
        f"query count grew with rows: {small['n']} -> {big['n']}\n"
        + "\n".join(big["sql"])
    )
    assert big["n"] <= 2, f"expected entries + services, got {big['sql']}"


def test_batched_backlog_query_count_is_flat_in_agents():
    tid, svcs, agents = _seed(n_agents=6, n_services=3)
    _fill(tid, svcs, agents, per_agent=4)

    with count_queries() as one:
        queue_engine.get_agent_backlogs_minutes(agents[:1], tid, DATE)
    with count_queries() as many:
        queue_engine.get_agent_backlogs_minutes(agents, tid, DATE)

    assert one["n"] == many["n"] <= 2


def test_batched_backlog_matches_the_single_agent_version():
    """The fast path must not be a different answer, only a cheaper one."""
    tid, svcs, agents = _seed(n_agents=4, n_services=3)
    _fill(tid, svcs, agents, per_agent=3)

    batched = queue_engine.get_agent_backlogs_minutes(agents, tid, DATE)
    for aid in agents:
        assert batched[aid] == queue_engine.get_agent_backlog_minutes(aid, tid, DATE)


def test_assign_agent_query_count_is_flat_in_agents():
    """Was one Session + one query per candidate agent, plus one per entry."""
    tid_a, svcs_a, agents_a = _seed(n_agents=1, n_services=2)
    _fill(tid_a, svcs_a, agents_a, per_agent=5)
    with count_queries() as one_agent:
        queue_engine.assign_agent(_tenant(tid_a), svcs_a[0], None, DATE)

    tid_b, svcs_b, agents_b = _seed(n_agents=8, n_services=2)
    _fill(tid_b, svcs_b, agents_b, per_agent=5)
    with count_queries() as many_agents:
        queue_engine.assign_agent(_tenant(tid_b), svcs_b[0], None, DATE)

    assert one_agent["n"] == many_agents["n"], (
        f"{one_agent['n']} -> {many_agents['n']} for 1 -> 8 agents\n"
        + "\n".join(many_agents["sql"])
    )


def test_assign_agent_still_picks_the_shortest_backlog():
    tid, [svc], agents = _seed(n_agents=3, n_services=1)
    busy, medium, free = agents
    _fill(tid, [svc], [busy], per_agent=5)
    _fill(tid, [svc], [medium], per_agent=2)

    assert queue_engine.assign_agent(_tenant(tid), svc, None, DATE) == free


def test_assign_agent_still_honours_an_explicit_preference():
    tid, [svc], agents = _seed(n_agents=3, n_services=1)
    busy = agents[0]
    _fill(tid, [svc], [busy], per_agent=5)

    assert queue_engine.assign_agent(_tenant(tid), svc, busy, DATE) == busy


def test_recalculate_queue_query_count_is_flat_in_entries():
    tid, svcs, [aid] = _seed(n_agents=1, n_services=3)

    _fill(tid, svcs, [aid], per_agent=2)
    with count_queries() as small:
        queue_engine.recalculate_queue(tid, aid, DATE)

    _fill(tid, svcs, [aid], per_agent=10)
    with count_queries() as big:
        queue_engine.recalculate_queue(tid, aid, DATE)

    # Row UPDATEs are per-entry by nature; only SELECTs should be flat.
    small_reads = [q for q in small["sql"] if q.lstrip().upper().startswith("SELECT")]
    big_reads   = [q for q in big["sql"] if q.lstrip().upper().startswith("SELECT")]
    assert len(small_reads) == len(big_reads), "\n".join(big_reads)


def test_gap_fill_query_count_is_flat_in_entries():
    tid, svcs, [aid] = _seed(n_agents=1, n_services=3)
    tenant = _tenant(tid)

    _fill(tid, svcs, [aid], per_agent=2)
    with count_queries() as small:
        queue_engine.find_walkin_insert_joined_at(aid, tid, tenant, DATE, svcs[0])

    _fill(tid, svcs, [aid], per_agent=10)
    with count_queries() as big:
        queue_engine.find_walkin_insert_joined_at(aid, tid, tenant, DATE, svcs[0])

    assert small["n"] == big["n"], "\n".join(big["sql"])


def test_gap_fill_still_slots_in_before_a_later_appointment(monkeypatch):
    """
    Behaviour guard on the batched service lookup. The clock is frozen onto
    DATE — gap-fill now schedules inside the agent's working windows for that
    day, so a real now() months away from DATE has no window to sit in.
    """
    frozen = datetime(2026, 6, 20, 10, 0)
    monkeypatch.setattr(core, "now", lambda: frozen)

    tid, [svc], [aid] = _seed(n_agents=1, n_services=1)
    with Session(db.engine) as s:
        s.add(QueueEntry(
            tenant_id=tid, service_id=svc, agent_id=aid,
            customer_number="2781x", customer_name="Later", status="Waiting",
            queue_date=DATE, earliest_arrival=frozen + timedelta(hours=4),
            joined_at=frozen,
        ))
        s.commit()

    tenant = _tenant(tid)
    assert queue_engine.find_walkin_insert_joined_at(aid, tid, tenant, DATE, svc) is not None


def test_get_queue_query_count_is_flat_in_rows(super_token):
    """The dashboard polls this every 30s per open tab."""
    from fastapi.testclient import TestClient
    c = TestClient(main.app)
    hdr = {"Authorization": f"Bearer {super_token}"}

    tid, svcs, agents = _seed(n_agents=4, n_services=3)

    _fill(tid, svcs, agents, per_agent=1)     # 4 rows
    with count_queries() as small:
        r = c.get(f"/admin/queue/{tid}?queue_date={DATE}", headers=hdr)
    assert r.status_code == 200 and len(r.json()) == 4

    _fill(tid, svcs, agents, per_agent=9)     # 40 rows
    with count_queries() as big:
        r = c.get(f"/admin/queue/{tid}?queue_date={DATE}", headers=hdr)
    assert r.status_code == 200 and len(r.json()) == 40

    assert small["n"] == big["n"], (
        f"query count grew with queue size: {small['n']} -> {big['n']}\n"
        + "\n".join(big["sql"])
    )


def test_get_queue_still_resolves_agent_and_service_names(super_token):
    from fastapi.testclient import TestClient
    c = TestClient(main.app)
    tid, [svc], [aid] = _seed(n_agents=1, n_services=1)
    _fill(tid, [svc], [aid], per_agent=1)

    rows = c.get(f"/admin/queue/{tid}?queue_date={DATE}",
                 headers={"Authorization": f"Bearer {super_token}"}).json()
    assert rows[0]["agent"] == "A0"
    assert rows[0]["service"] == "S0"


def test_get_queue_tolerates_a_deleted_service(super_token):
    """Batched lookup must fall back the same way s.get(None) did."""
    from fastapi.testclient import TestClient
    c = TestClient(main.app)
    tid, [svc], [aid] = _seed(n_agents=1, n_services=1)
    _fill(tid, [svc], [aid], per_agent=1)
    with Session(db.engine) as s:
        s.delete(s.get(Service, svc)); s.commit()

    rows = c.get(f"/admin/queue/{tid}?queue_date={DATE}",
                 headers={"Authorization": f"Bearer {super_token}"}).json()
    assert rows[0]["service"] == "—"


# =============================================================================
# Booking lock
# =============================================================================
class FakeRedis:
    """SET NX EX + the compare-and-delete EVAL, enough for booking_lock."""

    def __init__(self):
        self.store = {}
        self._lock = threading.Lock()
        self.set_calls = 0

    def set(self, key, value, nx=False, ex=None):
        with self._lock:
            self.set_calls += 1
            if nx and key in self.store:
                return None
            self.store[key] = value
            return True

    def eval(self, script, numkeys, key, arg):
        with self._lock:
            if self.store.get(key) == arg:
                del self.store[key]
                return 1
            return 0


class BrokenRedis:
    def set(self, *a, **k):
        raise ConnectionError("redis down")

    def eval(self, *a, **k):
        raise ConnectionError("redis down")


@pytest.fixture
def fake_redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(config, "redis_client", fake)
    return fake


def test_lock_is_acquired_and_released(fake_redis):
    key = f"lock:booking:1:{DATE}"
    with sessions.booking_lock(1, DATE) as held:
        assert held is True
        assert list(fake_redis.store) == [key]
        assert fake_redis.store[key], "lock must carry an owner token"
    assert fake_redis.store == {}, "lock outlived the block"


def test_lock_is_released_even_when_the_body_raises(fake_redis):
    with pytest.raises(RuntimeError):
        with sessions.booking_lock(1, DATE):
            raise RuntimeError("boom")
    assert fake_redis.store == {}, "a failed booking would wedge the tenant"


def test_lock_is_scoped_per_tenant_and_date(fake_redis):
    """Two businesses booking at once must not wait on each other."""
    with sessions.booking_lock(1, DATE) as a:
        with sessions.booking_lock(2, DATE) as b:
            with sessions.booking_lock(1, "2026-06-21") as c:
                assert (a, b, c) == (True, True, True)


def test_second_holder_waits_then_proceeds_unlocked(fake_redis, monkeypatch):
    """
    Cap the wait — handle_webhook is async and calls in synchronously, so a
    long spin stalls the event loop. Losing the booking is worse than a rare
    unlocked one, so it proceeds rather than raising.
    """
    monkeypatch.setattr(config, "BOOKING_LOCK_WAIT", 0.05)
    with sessions.booking_lock(1, DATE) as first:
        assert first is True
        started = real_time.monotonic()
        with sessions.booking_lock(1, DATE) as second:
            waited = real_time.monotonic() - started
            assert second is False, "should not claim a lock it never got"
        assert waited >= 0.05, "must actually wait before giving up"
    assert fake_redis.store == {}, "inner degraded-open exit stole the outer lock"


def test_release_is_compare_and_delete(fake_redis, monkeypatch):
    """
    If our TTL lapses and someone else takes the key, a blind DEL on exit
    would release their lock. Simulate by overwriting the token mid-block.
    """
    key = f"lock:booking:1:{DATE}"
    with sessions.booking_lock(1, DATE):
        fake_redis.store[key] = "somebody-elses-token"
    assert fake_redis.store[key] == "somebody-elses-token"


def test_lock_degrades_open_when_redis_is_down(monkeypatch):
    monkeypatch.setattr(config, "redis_client", BrokenRedis())
    ran = False
    with sessions.booking_lock(1, DATE) as held:
        assert held is False
        ran = True
    assert ran, "an unreachable Redis must not block bookings"


def test_lock_serialises_two_threads(fake_redis, monkeypatch):
    """
    The point of the lock: critical sections must not overlap. Each thread
    marks itself in, sleeps, and asserts nobody else got in meanwhile.
    """
    monkeypatch.setattr(config, "BOOKING_LOCK_WAIT", 5.0)
    inside = []
    overlaps = []

    def book():
        with sessions.booking_lock(1, DATE) as held:
            assert held, "5s wait should be ample"
            inside.append(1)
            if len(inside) > 1:
                overlaps.append(1)
            real_time.sleep(0.05)
            inside.pop()

    threads = [threading.Thread(target=book) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not overlaps, "two bookings were inside the lock at once"
    assert fake_redis.store == {}


# ── the race the lock exists for ─────────────────────────────────────────────
def test_walkins_get_distinct_positions(super_token, fake_redis):
    from fastapi.testclient import TestClient
    c = TestClient(main.app)
    hdr = {"Authorization": f"Bearer {super_token}"}
    tid, [svc], [aid] = _seed(n_agents=1, n_services=1)

    positions = []
    for i in range(4):
        r = c.post("/admin/queue/walkin", headers=hdr, json={
            "tenant_id": tid, "service_id": svc, "agent_id": aid,
            "customer_name": f"W{i}", "queue_date": DATE,
        })
        assert r.status_code == 200, r.text
        positions.append(r.json()["position"])

    assert sorted(positions) == [1, 2, 3, 4], positions


def test_walkin_takes_the_booking_lock(super_token, fake_redis):
    """Regression guard — the admin path must not bypass the lock."""
    from fastapi.testclient import TestClient
    c = TestClient(main.app)
    tid, [svc], [aid] = _seed(n_agents=1, n_services=1)

    before = fake_redis.set_calls
    r = c.post("/admin/queue/walkin",
               headers={"Authorization": f"Bearer {super_token}"},
               json={"tenant_id": tid, "service_id": svc, "agent_id": aid,
                     "customer_name": "W", "queue_date": DATE})
    assert r.status_code == 200, r.text
    assert fake_redis.set_calls > before
    assert fake_redis.store == {}, "walk-in left the lock held"


def test_walkin_releases_the_lock_on_a_rejected_booking(super_token, fake_redis):
    """Closed queue raises HTTPException inside the lock."""
    from fastapi.testclient import TestClient
    c = TestClient(main.app)
    tid, [svc], [aid] = _seed(n_agents=1, n_services=1)
    with Session(db.engine) as s:
        t = s.get(Tenant, tid)
        t.queue_opens, t.queue_closes = 23, 23   # shut all day
        s.add(t); s.commit()

    r = c.post("/admin/queue/walkin",
               headers={"Authorization": f"Bearer {super_token}"},
               json={"tenant_id": tid, "service_id": svc, "agent_id": aid,
                     "customer_name": "W", "queue_date": core.today_str()})
    assert r.status_code == 400
    assert fake_redis.store == {}, "a rejected walk-in wedged the tenant"
