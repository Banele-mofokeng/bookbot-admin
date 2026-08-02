"""SQLModel tables. No behaviour lives here."""
from datetime import datetime
from typing import Optional
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
    position: int              = 0                   # display position in full queue
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

