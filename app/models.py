"""SQLModel tables. No behaviour lives here."""
from datetime import datetime
from typing import Optional
from sqlalchemy import UniqueConstraint
from sqlmodel import SQLModel, Field

from app.core import now

# =============================================================================
# 2. DATABASE MODELS
# =============================================================================

class Tenant(SQLModel, table=True):
    """One business using the platform."""
    id: Optional[int]          = Field(default=None, primary_key=True)
    business_name: str                               # "Porsche Hair Salon"
    business_type: str         = "General"           # "Hair Salon", "Clinic", etc.
    whatsapp_number: str                             # the business WhatsApp number
    owner_number: str          = ""                  # owner's personal number for notifications
    evolution_instance: str                          # Evolution API instance name
    evolution_api_key: str                           # Evolution API key
    evolution_api_url: str                           # Evolution API base URL
    # ── Labels (makes the bot generic) ──
    agent_label: str           = "Agent"             # "Stylist", "Doctor", "Bay"
    service_label: str         = "Service"           # "Hair Service", "Procedure"
    # ── Queue config ──
    queue_opens: int           = 8                   # 8 = 08:00
    queue_closes: int          = 17                  # 17 = 17:00
    advance_days: int          = 1                   # how many days ahead allowed (0 = today only)
    is_active: bool            = True
    # ── Which conversation the bot runs ──
    # "queue"  — book a slot with an agent (salon, clinic, workshop)
    # "orders" — order items off a menu (takeaway, kasi fastfood)
    # One tenant runs one of them. Existing tenants default to queue, so this
    # column changes nothing for anyone already deployed.
    mode: str                  = "queue"
    # ── Ordering config (ignored in queue mode) ──
    currency_symbol: str       = "R"
    # How many items the kitchen genuinely works on at once. Prep time for a
    # backlog is divided by this, so a two-pan kitchen quotes twice the wait of
    # a four-pan one for the same queue of orders.
    kitchen_parallel_items: int = 4
    # ── Appointment config (ignored outside appointment booking) ──
    # The grid offered times land on. 30 means 09:00, 09:30, 10:00 — predictable
    # for a customer to read and for a calendar to draw. A service longer than
    # one step simply consumes several.
    slot_granularity_minutes: int = 30


class User(SQLModel, table=True):
    """A dashboard login. Either a super-admin (platform operator, tenant_id=None)
    or a tenant user who can only see/manage their own business."""
    __tablename__ = "app_user"  # avoid the reserved word "user" in Postgres
    id: Optional[int]          = Field(default=None, primary_key=True)
    email: str                 = Field(index=True)       # unique login email (stored lowercased)
    password_hash: str                                   # pbkdf2 "iterations$salt$hash"
    tenant_id: Optional[int]   = Field(default=None, foreign_key="tenant.id")  # None = super-admin
    is_super: bool             = False
    is_active: bool            = True


class Service(SQLModel, table=True):
    """A service offered by a tenant e.g. Box Braids, Wheel Alignment."""
    id: Optional[int]          = Field(default=None, primary_key=True)
    tenant_id: int             = Field(foreign_key="tenant.id")
    name: str                                        # "Box Braids"
    duration_minutes: int      = 60                  # how long this service takes
    is_active: bool            = True


class Agent(SQLModel, table=True):
    """A person or station that serves customers e.g. Nomsa, Bay 1, Dr Dlamini."""
    id: Optional[int]          = Field(default=None, primary_key=True)
    tenant_id: int             = Field(foreign_key="tenant.id")
    name: str                                        # "Nomsa"
    is_active: bool            = True


class AgentService(SQLModel, table=True):
    """Which services each agent can perform."""
    id: Optional[int]          = Field(default=None, primary_key=True)
    agent_id: int              = Field(foreign_key="agent.id")
    service_id: int            = Field(foreign_key="service.id")


class AgentSchedule(SQLModel, table=True):
    """
    One recurring working window for an agent on one weekday. Several rows for
    the same weekday make a split shift — 08:00–12:00 and 13:00–17:00 is two
    rows, and the hour between them is simply not worked.

    Times are minutes from midnight rather than a time column: it keeps the
    arithmetic in one unit, sidesteps per-dialect time handling, and lets a
    window run to 1440 (midnight) without wrapping.

    An agent with NO rows at all falls back to the tenant's opening hours, so
    every existing agent keeps working exactly as before until someone sets a
    schedule. An agent WITH rows but none for today is off today.
    """
    __tablename__ = "agent_schedule"
    id: Optional[int]          = Field(default=None, primary_key=True)
    tenant_id: int             = Field(foreign_key="tenant.id")
    agent_id: int              = Field(foreign_key="agent.id")
    weekday: int                                     # 0 = Monday … 6 = Sunday
    start_minute: int          = 8 * 60              # 480 = 08:00
    end_minute: int            = 17 * 60             # 1020 = 17:00, exclusive


class AgentBlock(SQLModel, table=True):
    """
    A one-off window an agent is unavailable — leave, training, a dentist
    appointment. Subtracted from whatever AgentSchedule says, so it wins.
    Stored as absolute datetimes because a block is a specific occasion, not a
    recurring pattern.
    """
    __tablename__ = "agent_block"
    id: Optional[int]          = Field(default=None, primary_key=True)
    tenant_id: int             = Field(foreign_key="tenant.id")
    agent_id: int              = Field(foreign_key="agent.id")
    starts_at: datetime
    ends_at: datetime
    reason: str                = ""


class QueueEntry(SQLModel, table=True):
    """A single customer in a queue."""
    id: Optional[int]          = Field(default=None, primary_key=True)
    tenant_id: int             = Field(foreign_key="tenant.id")
    service_id: int            = Field(foreign_key="service.id")
    agent_id: int              = Field(foreign_key="agent.id")
    preferred_agent_id: Optional[int] = Field(default=None, foreign_key="agent.id")
    customer_number: str                             # "27764519653@s.whatsapp.net"
    customer_name: str
    additional_names: str      = ""                  # kept for legacy walk-in use; children now get own entries
    parent_entry_id: Optional[int] = Field(default=None)  # set on child entries to link back to parent
    customer_phone: str        = ""                  # walk-in phone for notifications e.g. "27812345678"
    status: str                = "Waiting"           # Waiting|InService|Done|NoShow|Cancelled
    booked_via: str            = "whatsapp"          # whatsapp | walkin
    queue_date: str                                  # "2026-03-18" — ISO date string
    estimated_start: Optional[datetime] = None       # calculated ETA
    earliest_arrival: Optional[datetime] = None      # customer's declared arrival time
    # ── Appointments ──
    # A queue entry's estimated_start is *derived*: recalculate_queue rewrites
    # it whenever anything ahead of it changes, which is right for a queue and
    # wrong for a booked time. is_fixed says this start is a promise. Fixed
    # entries are never rewritten; they become obstacles the flexible queue is
    # scheduled around, which is also what lets walk-ins fill the gaps between
    # them.
    is_fixed: bool             = False
    # When the promise ends. Written once at booking rather than derived from
    # Service.duration_minutes, so retiming a service tomorrow cannot silently
    # move the end of an appointment booked today.
    slot_end: Optional[datetime] = None
    position: int              = 0                   # display position in full queue
    # Superseded by NotificationLog, which is now what decides whether a
    # notification has been claimed. Still written, so a rollout where old and
    # new processes overlap cannot re-send — the old code reads these and knows
    # nothing about the log. Drop both once no such process is left running.
    notified_two_away: bool    = False
    notified_next: bool        = False
    joined_at: datetime        = Field(default_factory=now)
    # ── Provenance, for reporting ──
    # Who closed this entry: "staff" (someone tapped it on the dashboard),
    # "customer" (cancelled over WhatsApp), "system" (the midnight sweep closed
    # it because nobody ever did), or "" on rows written before this existed.
    # Reporting must never merge these. A system-closed NoShow means the shop
    # forgot to tap Done — not that the customer failed to turn up — and
    # counting it as a no-show measures staff habits while claiming to measure
    # customers.
    closed_by: str             = ""
    started_at: Optional[datetime]  = None   # first moved to InService
    finished_at: Optional[datetime] = None   # reached a terminal status


class OutboxMessage(SQLModel, table=True):
    """
    A WhatsApp message waiting to go out. Writing here instead of calling
    Evolution inline means a webhook reply never blocks on the network, and a
    send that fails is retried rather than silently dropped.
    """
    __tablename__ = "outbox_message"
    id: Optional[int]          = Field(default=None, primary_key=True)
    tenant_id: int             = Field(foreign_key="tenant.id")
    to_number: str
    body: str
    # Set for notifications that must never go out twice (e.g. "you're next"
    # for a given entry). Empty for ordinary conversational replies, which are
    # always sent — the same menu text legitimately repeats.
    dedupe_key: str            = Field(default="", index=True)
    status: str                = "Pending"   # Pending | Sent | Failed
    attempts: int              = 0
    # Indexed jointly with status — see INDEXES; alone it serves no query.
    next_attempt_at: datetime  = Field(default_factory=now)
    last_error: str            = ""
    created_at: datetime       = Field(default_factory=now)
    sent_at: Optional[datetime] = None


class NotificationLog(SQLModel, table=True):
    """
    One row per notification claimed for one queue entry.

    Replaces a boolean column per notification kind. A column per kind stops
    scaling the moment there are more than a couple — a reminder ladder wants
    T-24h, T-2h and T-30m — and each new one costs a migration on the hottest
    table in the app. A row per (entry, rule) costs none.

    The unique constraint *is* the claim. Two callers racing to send the same
    notification both INSERT, exactly one commits, and the loser is told it
    lost. Stricter than a read-then-write, and unlike the conditional UPDATE
    this replaced, the rule arrives as a bound parameter rather than as SQL
    text — so a rule name can be anything stable without being a hazard.
    """
    __tablename__ = "notification_log"
    __table_args__ = (
        UniqueConstraint("entry_id", "rule", name="uq_notification_entry_rule"),
    )
    id: Optional[int]          = Field(default=None, primary_key=True)
    entry_id: int              = Field(foreign_key="queueentry.id", index=True)
    rule: str                                        # "two_away", "youre_next", …
    # When the claim was made, not when the message reached the customer — the
    # outbox owns delivery. Rows seeded by the backfill carry the time they were
    # seeded, because the original send time was never recorded anywhere.
    sent_at: datetime          = Field(default_factory=now)


# =============================================================================
# 2a. ORDERING TABLES
# =============================================================================
# Deliberately separate from Service/QueueEntry rather than bent onto them. A
# queue entry is one service on one agent at one time; an order is several
# items, in quantities, with money attached and nobody assigned. Sharing the
# tables would mean every queue query growing an "is this actually an order?"
# branch.

class MenuItem(SQLModel, table=True):
    """Something a customer can order e.g. Kota, Chips, 500ml Coke."""
    __tablename__ = "menu_item"
    id: Optional[int]          = Field(default=None, primary_key=True)
    tenant_id: int             = Field(foreign_key="tenant.id")
    name: str                                        # "Full House Kota"
    category: str              = ""                  # "Kotas", "Drinks" — blank groups last
    # Money is in cents, always. Floats lose rands over a day of orders.
    price_cents: int           = 0
    prep_minutes: int          = 10                  # how long the kitchen needs
    is_active: bool            = True                # sold out = deactivate
    sort_order: int            = 0                   # display order within a category


class Order(SQLModel, table=True):
    """One customer's order, placed over WhatsApp or rung up at the counter."""
    __tablename__ = "customer_order"  # "order" is a reserved word in SQL
    id: Optional[int]          = Field(default=None, primary_key=True)
    tenant_id: int             = Field(foreign_key="tenant.id")
    customer_number: str       = ""                  # "27764519653@s.whatsapp.net"
    customer_name: str         = ""
    customer_phone: str        = ""                  # counter orders: a number to notify
    status: str                = "Placed"            # Placed|Preparing|Ready|Collected|Cancelled
    # Written once at placement from the items on the order. Never recomputed
    # from the menu — a price change tonight must not rewrite this morning's
    # takings.
    total_cents: int           = 0
    order_date: str            = ""                  # "2026-08-02" — ISO date string
    placed_via: str            = "whatsapp"          # whatsapp | counter
    ready_at: Optional[datetime] = None              # quoted collection time
    notified_ready: bool       = False
    note: str                  = ""                  # "no chilli"
    created_at: datetime       = Field(default_factory=now)
    started_at: Optional[datetime]  = None           # first moved to Preparing
    finished_at: Optional[datetime] = None           # reached a terminal status
    # Same provenance rule as QueueEntry: "staff", "customer", "system", or ""
    # on rows written before this existed. Reporting must not merge them — an
    # order the midnight sweep cancelled means staff never closed it, not that
    # the customer walked away.
    closed_by: str             = ""

    @property
    def code(self) -> str:
        """Short counter callout code. Unique within a day well past any
        realistic daily volume; the board is per-day, so wrap-around across
        days is not a collision anyone sees."""
        return f"{(self.id or 0) % 1000:03d}"


class OrderItem(SQLModel, table=True):
    """One line on an order. Name and price are snapshots, not lookups."""
    __tablename__ = "order_item"
    id: Optional[int]          = Field(default=None, primary_key=True)
    order_id: int              = Field(foreign_key="customer_order.id")
    menu_item_id: Optional[int] = Field(default=None, foreign_key="menu_item.id")
    # Copied at placement so renaming or repricing an item — or deleting it —
    # leaves historical orders and their totals intact.
    name: str                  = ""
    qty: int                   = 1
    unit_price_cents: int      = 0
    prep_minutes: int          = 10

