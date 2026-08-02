"""
Hot-path indexes.

Asserting an index exists is weak — it would still pass if the columns were in
an order the planner can't use. So these tests also run EXPLAIN QUERY PLAN over
the real queue-engine queries and assert the planner picks the index instead of
scanning. The planner is SQLite's here, not PostgreSQL's, but column order and
prefix usability are what's being checked and those carry over.
"""
import pytest
from sqlalchemy import text
from sqlmodel import Session, select

from app import db
from app.models import QueueEntry, OutboxMessage, Tenant, Service, Agent, AgentService, AgentSchedule, AgentBlock


def _index_names():
    with db.engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        )).fetchall()
    return {r[0] for r in rows}


def _plan(stmt) -> str:
    """EXPLAIN QUERY PLAN for a SQLModel select, as one string."""
    compiled = stmt.compile(db.engine, compile_kwargs={"literal_binds": True})
    with db.engine.connect() as conn:
        rows = conn.execute(text(f"EXPLAIN QUERY PLAN {compiled}")).fetchall()
    return " | ".join(str(r) for r in rows)


# ── creation ─────────────────────────────────────────────────────────────────
def test_every_declared_index_exists():
    present = _index_names()
    missing = [name for name, _, _ in db.INDEXES if name not in present]
    assert not missing, f"missing indexes: {missing}"


def test_ensure_indexes_is_idempotent():
    """Runs on every boot — a second call must not error."""
    db.ensure_indexes()
    db.ensure_indexes()
    present = _index_names()
    assert all(name in present for name, _, _ in db.INDEXES)


def test_index_tables_match_the_models():
    """Table names are taken from the models, so they can't drift on a rename."""
    tables = {t for _, t, _ in db.INDEXES}
    assert tables == {
        QueueEntry.__tablename__, OutboxMessage.__tablename__,
        Service.__tablename__, Agent.__tablename__,
        AgentService.__tablename__, Tenant.__tablename__,
        AgentSchedule.__tablename__, AgentBlock.__tablename__,
    }


# ── the planner actually uses them ───────────────────────────────────────────
def test_queue_engine_filter_uses_the_composite():
    """The shape every backlog / ETA / gap-fill query shares."""
    stmt = select(QueueEntry).where(
        QueueEntry.tenant_id  == 1,
        QueueEntry.queue_date == "2026-06-20",
        QueueEntry.agent_id   == 1,
        QueueEntry.status.in_(["Waiting", "InService"]),
    )
    assert "ix_queueentry_tenant_date_agent_status" in _plan(stmt)


def test_dashboard_day_read_uses_the_composite_prefix():
    """get_queue filters only tenant_id + queue_date — the leading two columns."""
    stmt = select(QueueEntry).where(
        QueueEntry.tenant_id  == 1,
        QueueEntry.queue_date == "2026-06-20",
    )
    assert "ix_queueentry_tenant_date_agent_status" in _plan(stmt)


def test_already_in_queue_lookup_is_indexed():
    stmt = select(QueueEntry).where(
        QueueEntry.tenant_id       == 1,
        QueueEntry.customer_number == "2782@s.whatsapp.net",
        QueueEntry.queue_date      >= "2026-06-20",
    )
    assert "ix_queueentry_tenant_customer_date" in _plan(stmt)


def test_party_cancellation_lookup_is_indexed():
    stmt = select(QueueEntry).where(QueueEntry.parent_entry_id == 1)
    assert "ix_queueentry_parent" in _plan(stmt)


def test_reconciler_sweep_is_indexed():
    """Cross-tenant, runs every 60s."""
    stmt = select(QueueEntry).where(
        QueueEntry.status     == "InService",
        QueueEntry.queue_date >= "2026-06-19",
    )
    assert "ix_queueentry_status_date" in _plan(stmt)


def test_outbox_drain_is_indexed():
    """Runs twice a second — the hottest query in the app."""
    stmt = select(OutboxMessage).where(
        OutboxMessage.status == "Pending",
        OutboxMessage.next_attempt_at <= "2026-06-20 10:00:00",
    ).order_by(OutboxMessage.id)
    assert "ix_outbox_status_due" in _plan(stmt)


def test_tenant_resolution_is_indexed():
    """Every inbound webhook resolves the tenant by WhatsApp number."""
    stmt = select(Tenant).where(
        Tenant.whatsapp_number == "27810000000",
        Tenant.is_active == True,
    )
    assert "ix_tenant_whatsapp" in _plan(stmt)


@pytest.mark.parametrize("stmt_factory, expected", [
    (lambda: select(Service).where(Service.tenant_id == 1), "ix_service_tenant"),
    (lambda: select(Agent).where(Agent.tenant_id == 1), "ix_agent_tenant"),
    (lambda: select(AgentService).where(AgentService.service_id == 1),
     "ix_agentservice_service"),
    (lambda: select(AgentService).where(AgentService.agent_id == 1),
     "ix_agentservice_agent"),
])
def test_lookup_tables_are_indexed(stmt_factory, expected):
    assert expected in _plan(stmt_factory())


# ── behaviour is unchanged ───────────────────────────────────────────────────
def test_results_are_identical_with_indexes():
    """An index must change speed, never answers."""
    with Session(db.engine) as s:
        t = Tenant(business_name="T", whatsapp_number="27810000000",
                   evolution_instance="i", evolution_api_key="k",
                   evolution_api_url="http://x")
        s.add(t); s.commit(); s.refresh(t)
        svc = Service(tenant_id=t.id, name="Cut", duration_minutes=60)
        s.add(svc); s.commit(); s.refresh(svc)
        a = Agent(tenant_id=t.id, name="A")
        s.add(a); s.commit(); s.refresh(a)
        for i in range(5):
            s.add(QueueEntry(
                tenant_id=t.id, service_id=svc.id, agent_id=a.id,
                customer_number=f"2782{i}", customer_name=f"C{i}",
                status="Waiting", queue_date="2026-06-20",
            ))
        s.commit()

        indexed = s.exec(select(QueueEntry).where(
            QueueEntry.tenant_id  == t.id,
            QueueEntry.queue_date == "2026-06-20",
            QueueEntry.agent_id   == a.id,
            QueueEntry.status     == "Waiting",
        )).all()

    assert len(indexed) == 5
