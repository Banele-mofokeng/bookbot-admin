"""Engine, indexes, and the additive migrations create_all cannot do."""
from sqlmodel import SQLModel, create_engine

from app import config
from app.models import (Tenant, User, Service, Agent, AgentService,
                        AgentSchedule, AgentBlock, QueueEntry, OutboxMessage)

engine = create_engine(config.DATABASE_URL)

# Indexes for the queries that actually run hot. Table names come from the
# models so a rename can't leave a stale string behind.
#
# Every lookup in the queue engine — backlog, ETA recalculation, gap-fill,
# next-waiter — filters the same four columns, so one composite index serves all
# of them, and its (tenant_id, queue_date) prefix also serves the dashboard's
# whole-day read.
INDEXES = [
    # name, table, columns
    ("ix_queueentry_tenant_date_agent_status", QueueEntry.__tablename__,
     "tenant_id, queue_date, agent_id, status"),
    # "Are you already in the queue?" — runs on every WhatsApp booking attempt.
    ("ix_queueentry_tenant_customer_date", QueueEntry.__tablename__,
     "tenant_id, customer_number, queue_date"),
    # Whole-party cancellation walks children back to their parent.
    ("ix_queueentry_parent", QueueEntry.__tablename__, "parent_entry_id"),
    # Cross-tenant sweeps: the 60s reconciler and the midnight reset.
    ("ix_queueentry_status_date", QueueEntry.__tablename__, "status, queue_date"),
    # The outbox drain runs twice a second — this is the hottest query here.
    ("ix_outbox_status_due", OutboxMessage.__tablename__, "status, next_attempt_at"),
    ("ix_service_tenant", Service.__tablename__, "tenant_id"),
    ("ix_agent_tenant", Agent.__tablename__, "tenant_id"),
    ("ix_agentservice_service", AgentService.__tablename__, "service_id"),
    ("ix_agentservice_agent", AgentService.__tablename__, "agent_id"),
    # Working windows are resolved for every candidate agent on every booking.
    ("ix_agentschedule_agent_weekday", AgentSchedule.__tablename__,
     "agent_id, weekday"),
    ("ix_agentblock_agent_starts", AgentBlock.__tablename__,
     "agent_id, starts_at"),
    # Every inbound webhook resolves the tenant by its WhatsApp number.
    ("ix_tenant_whatsapp", Tenant.__tablename__, "whatsapp_number"),
]


def ensure_indexes():
    """
    Create the hot-path indexes if they're missing.

    create_all() only builds indexes for tables it creates, so an existing
    deployment never gets them from the model definitions alone. CREATE INDEX
    IF NOT EXISTS is understood by both PostgreSQL and SQLite, so this is the
    one place indexes are declared for fresh and existing databases alike.

    These are small tables; the brief write lock PostgreSQL takes while building
    is not worth the extra machinery of CONCURRENTLY. Revisit if queueentry ever
    grows to millions of rows.
    """
    from sqlalchemy import text as sql_text
    with engine.connect() as conn:
        for name, table, columns in INDEXES:
            conn.execute(sql_text(
                f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({columns})"
            ))
        conn.commit()


# Columns added to queueentry after the table first shipped. create_all() never
# alters an existing table, so an already-deployed database only gets these from
# here. Order is irrelevant — each is added only if absent.
QUEUEENTRY_COLUMNS = [
    ("parent_entry_id",  "INTEGER"),
    ("earliest_arrival", "TIMESTAMP"),
    # Analytics provenance. Existing rows keep closed_by = '' and are reported
    # as "unknown" rather than being folded into a real no-show count.
    ("closed_by",        "VARCHAR DEFAULT ''"),
    ("started_at",       "TIMESTAMP"),
    ("finished_at",      "TIMESTAMP"),
]


def create_db_and_tables():
    from sqlalchemy import text as sql_text
    SQLModel.metadata.create_all(engine)
    ensure_indexes()
    # Add new columns to existing tables if they don't exist yet (PostgreSQL migration)
    with engine.connect() as conn:
        existing = {row[0] for row in conn.execute(sql_text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'queueentry'"
        )).fetchall()}
        for column, coltype in QUEUEENTRY_COLUMNS:
            if column not in existing:
                conn.execute(sql_text(
                    f"ALTER TABLE queueentry ADD COLUMN {column} {coltype}"
                ))
                conn.commit()

