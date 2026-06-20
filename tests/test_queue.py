"""
Integration tests for the queue engine and admin auth, on a SQLite engine.
Covers the production fixes: whole-party cancellation and guarded admin routes.
"""
from datetime import datetime

from sqlmodel import Session, select
from fastapi.testclient import TestClient

import main
from main import Tenant, Service, Agent, AgentService, QueueEntry


def _seed_tenant_with_agents(n_agents=1):
    with Session(main.engine) as s:
        t = Tenant(
            business_name="Test Co", whatsapp_number="27810000000",
            evolution_instance="i", evolution_api_key="k", evolution_api_url="http://x",
            queue_opens=8, queue_closes=17, advance_days=0,
        )
        s.add(t); s.commit(); s.refresh(t)
        svc = Service(tenant_id=t.id, name="Cut", duration_minutes=60)
        s.add(svc); s.commit(); s.refresh(svc)
        agent_ids = []
        for i in range(n_agents):
            a = Agent(tenant_id=t.id, name=f"A{i}")
            s.add(a); s.commit(); s.refresh(a)
            s.add(AgentService(agent_id=a.id, service_id=svc.id)); s.commit()
            agent_ids.append(a.id)
        return t.id, svc.id, agent_ids


def _add_entry(tenant_id, svc_id, agent_id, name, status="Waiting",
               parent_id=None, date="2026-06-20"):
    with Session(main.engine) as s:
        e = QueueEntry(
            tenant_id=tenant_id, service_id=svc_id, agent_id=agent_id,
            customer_number="2781@s.whatsapp.net", customer_name=name,
            status=status, queue_date=date, parent_entry_id=parent_id,
            estimated_start=datetime(2026, 6, 20, 9, 0),
        )
        s.add(e); s.commit(); s.refresh(e)
        return e.id


# ── cancel_party (the family-cancel fix) ─────────────────────────
def test_cancel_party_cancels_parent_and_children():
    tid, svc, [aid] = _seed_tenant_with_agents(1)
    parent = _add_entry(tid, svc, aid, "Mom")
    c1 = _add_entry(tid, svc, aid, "Kid1", parent_id=parent)
    c2 = _add_entry(tid, svc, aid, "Kid2", parent_id=parent)

    touched = main.cancel_party(tid, parent)

    assert aid in touched
    with Session(main.engine) as s:
        rows = s.exec(select(QueueEntry).where(QueueEntry.tenant_id == tid)).all()
    assert {r.status for r in rows} == {"Cancelled"}


def test_cancel_party_from_a_child_still_cancels_whole_family():
    tid, svc, [aid] = _seed_tenant_with_agents(1)
    parent = _add_entry(tid, svc, aid, "Mom")
    c1 = _add_entry(tid, svc, aid, "Kid1", parent_id=parent)

    main.cancel_party(tid, c1)  # leave triggered from a child entry

    with Session(main.engine) as s:
        rows = s.exec(select(QueueEntry).where(QueueEntry.tenant_id == tid)).all()
    assert all(r.status == "Cancelled" for r in rows)


def test_cancel_party_children_only_booking():
    """First child is the party root; siblings link to it via parent_entry_id."""
    tid, svc, [aid] = _seed_tenant_with_agents(1)
    root = _add_entry(tid, svc, aid, "Kid1")            # parent_entry_id None
    sib  = _add_entry(tid, svc, aid, "Kid2", parent_id=root)

    main.cancel_party(tid, sib)

    with Session(main.engine) as s:
        rows = s.exec(select(QueueEntry).where(QueueEntry.tenant_id == tid)).all()
    assert all(r.status == "Cancelled" for r in rows)


def test_cancel_party_wrong_tenant_noop():
    tid, svc, [aid] = _seed_tenant_with_agents(1)
    e = _add_entry(tid, svc, aid, "Solo")
    assert main.cancel_party(tid + 999, e) == []
    with Session(main.engine) as s:
        row = s.get(QueueEntry, e)
    assert row.status == "Waiting"


# ── admin auth (the open-API fix) ────────────────────────────────
def test_admin_requires_token():
    client = TestClient(main.app)
    assert client.get("/admin/tenants").status_code == 401
    assert client.get("/admin/tenants", headers={"x-admin-token": "wrong"}).status_code == 401
    ok = client.get("/admin/tenants", headers={"x-admin-token": "test-token"})
    assert ok.status_code == 200


def test_health_is_public():
    client = TestClient(main.app)
    # health hits redis; if redis is down it still returns 200 with an error string
    assert client.get("/health").status_code == 200
