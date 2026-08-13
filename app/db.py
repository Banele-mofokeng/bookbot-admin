"""Engine, indexes, and the additive migrations create_all cannot do."""
from sqlmodel import SQLModel, create_engine

from app import config, core
from app.models import (Tenant, User, Service, Agent, AgentService,
                        AgentSchedule, AgentBlock, QueueEntry, OutboxMessage,
                        NotificationLog, MenuItem, Order, OrderItem)

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
    # ── Ordering ──
    # The kitchen board, and the backlog read behind every quoted ready time.
    ("ix_order_tenant_date_status", Order.__tablename__,
     "tenant_id, order_date, status"),
    # "What did I order?" — runs on every customer status or cancel request.
    ("ix_order_tenant_customer_date", Order.__tablename__,
     "tenant_id, customer_number, order_date"),
    # Lines are always fetched by their order, never on their own.
    ("ix_orderitem_order", OrderItem.__tablename__, "order_id"),
    ("ix_menuitem_tenant", MenuItem.__tablename__, "tenant_id"),
]


# Constraint-bearing indexes, kept apart from INDEXES because these are UNIQUE
# and partial rather than plain.
#
# The one below is not a performance index at all. It is the last line of
# defence against booking one agent twice for one moment: booking_lock degrades
# open when Redis is unreachable (see app/sessions.py), which for a queue costs
# a mis-assignment staff can fix, and for an appointment costs two customers
# sent to the same chair at the same time. Scoped to fixed, still-active rows,
# so cancelled and finished appointments never block rebooking the slot.
#
# It catches identical starts, which is the collision the grid actually
# produces. Partial overlap — 10:00 for 45 minutes against 10:30 — is caught by
# the explicit check in reserve_appointment instead; expressing that as a
# constraint needs PostgreSQL range exclusions, which SQLite cannot run and the
# test suite therefore could not exercise.
UNIQUE_INDEXES = [
    # name, table, columns, predicate
    ("uq_queueentry_agent_slot", QueueEntry.__tablename__,
     "agent_id, estimated_start",
     "is_fixed AND status IN ('Waiting', 'InService')"),
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
        for name, table, columns, predicate in UNIQUE_INDEXES:
            conn.execute(sql_text(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {name} "
                f"ON {table} ({columns}) WHERE {predicate}"
            ))
        conn.commit()


# Columns added to a table after it first shipped. create_all() never alters an
# existing table, so an already-deployed database only gets these from here.
# Order is irrelevant — each is added only if absent.
QUEUEENTRY_COLUMNS = [
    ("parent_entry_id",  "INTEGER"),
    ("earliest_arrival", "TIMESTAMP"),
    # Analytics provenance. Existing rows keep closed_by = '' and are reported
    # as "unknown" rather than being folded into a real no-show count.
    ("closed_by",        "VARCHAR DEFAULT ''"),
    ("started_at",       "TIMESTAMP"),
    ("finished_at",      "TIMESTAMP"),
    # Appointments. Every row already deployed is a queue entry whose start is
    # derived, so the default has to be false — flipping it would freeze every
    # live queue's ETAs where they stand.
    ("is_fixed",         "BOOLEAN DEFAULT false"),
    ("slot_end",         "TIMESTAMP"),
]

# Ordering config on an existing tenant row. The defaults matter: every tenant
# already deployed is a queue tenant, and must stay one until someone says
# otherwise on the dashboard.
TENANT_COLUMNS = [
    ("mode",                   "VARCHAR DEFAULT 'queue'"),
    ("currency_symbol",        "VARCHAR DEFAULT 'R'"),
    ("kitchen_parallel_items", "INTEGER DEFAULT 4"),
    ("slot_granularity_minutes", "INTEGER DEFAULT 30"),
    ("reminder_offsets_minutes", "VARCHAR DEFAULT '1440,120'"),
]

ADDED_COLUMNS = {
    "queueentry": QUEUEENTRY_COLUMNS,
    "tenant":     TENANT_COLUMNS,
}


# The boolean columns notification_log replaced, and the rule each one became.
# Kept here rather than imported from jobs so this module stays free of any
# dependency on the job layer.
_NOTIFICATION_BACKFILL = [
    ("notified_two_away", "two_away"),
    ("notified_next",     "youre_next"),
]


def backfill_notification_log():
    """
    Seed notification_log from the boolean columns it replaced.

    Without this, every entry already warned before this shipped looks unclaimed
    to the new code, and gets warned a second time on the first tick after
    deploy — across every live queue at once. Customers would be told they are
    next when they are not.

    Runs on every startup and is a no-op once seeded: the NOT EXISTS clause and
    the unique constraint behind it both make a second insert impossible.
    """
    from sqlalchemy import text as sql_text
    with engine.connect() as conn:
        for column, rule in _NOTIFICATION_BACKFILL:
            # A bare column reference rather than "= TRUE": SQLite stores these
            # as 0/1 and PostgreSQL as a real boolean, and both accept the
            # column on its own in a WHERE. The column name is one of ours, not
            # input. The rule is bound.
            # "rule" is quoted: it is non-reserved in PostgreSQL today, but it
            # was a keyword once and quoting costs nothing to be sure of.
            conn.execute(sql_text(
                f'INSERT INTO {NotificationLog.__tablename__} '
                f'    (entry_id, "rule", sent_at) '
                f'SELECT q.id, :rule, :seeded_at '
                f'  FROM {QueueEntry.__tablename__} q '
                f' WHERE q.{column} '
                f'   AND NOT EXISTS ('
                f'       SELECT 1 FROM {NotificationLog.__tablename__} n '
                f'        WHERE n.entry_id = q.id AND n."rule" = :rule)'
            ), {"rule": rule, "seeded_at": core.now()})
        conn.commit()


def create_db_and_tables():
    from sqlalchemy import text as sql_text
    SQLModel.metadata.create_all(engine)
    # Add new columns to existing tables if they don't exist yet (PostgreSQL
    # migration). This has to happen before ensure_indexes, not after: an index
    # over a column that only arrives here cannot be built until it exists.
    # uq_queueentry_agent_slot is the first index to depend on a column from
    # this list, so the old order would simply have failed on any existing
    # database.
    with engine.connect() as conn:
        for table, columns in ADDED_COLUMNS.items():
            existing = {row[0] for row in conn.execute(sql_text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = :table"
            ), {"table": table}).fetchall()}
            for column, coltype in columns:
                if column not in existing:
                    conn.execute(sql_text(
                        f"ALTER TABLE {table} ADD COLUMN {column} {coltype}"
                    ))
                    conn.commit()
    # Both of these run against a schema that is now up to date.
    ensure_indexes()
    backfill_notification_log()

