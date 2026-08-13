"""Time helpers and formatting shared by every other module."""
import os
import re
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

# Timezone — set TZ env var to your local timezone e.g. "Africa/Johannesburg"
TZ = ZoneInfo(os.getenv("TZ", "Africa/Johannesburg"))

def now() -> datetime:
    """Current local time aware of the configured timezone."""
    return datetime.now(TZ).replace(tzinfo=None)

def today_str() -> str:
    """Today's date as ISO string in local timezone."""
    return now().date().isoformat()

def yesterday_str() -> str:
    return (now().date() - timedelta(days=1)).isoformat()

def normalize_number(number: str) -> str:
    """
    Ensures a SA number has the correct country code.
    0812345678   → 27812345678
    27812345678  → 27812345678
    +27812345678 → 27812345678
    """
    n = number.strip().replace("+", "").replace(" ", "")
    if n.startswith("0"):
        n = "27" + n[1:]
    return n


def format_duration(minutes: int) -> str:
    """Converts minutes to a human-friendly string."""
    if minutes < 60:
        return f"~{minutes} min"
    hours   = minutes // 60
    remainder = minutes % 60
    if remainder == 0:
        return f"~{hours}hr"
    return f"~{hours}hr {remainder}min"


def format_eta(dt: Optional[datetime]) -> str:
    if not dt:
        return "TBD"
    return dt.strftime("%H:%M")


def parse_reminder_offsets(raw: str) -> list:
    """
    "1440,120" → [1440, 120]. Minutes before an appointment, largest first.

    Duplicates, blanks, zero, negatives and anything unparseable are dropped
    rather than raising: this reads a tenant's configuration on a hot path, and
    one bad character in a settings field must not stop every other tenant's
    reminders. An empty result switches reminders off for that tenant, which is
    the honest reading of a setting nobody can make sense of — the dashboard
    rejects a bad value at the point somebody types it.
    """
    out = set()
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            minutes = int(part)
        except ValueError:
            continue
        if minutes > 0:
            out.add(minutes)
    return sorted(out, reverse=True)


def describe_gap(minutes: int) -> str:
    """
    How far off something is, in the words a person would use. Drives the
    reminder's opening line, so 1440 reads "tomorrow" and not "in 1440 minutes".
    """
    if minutes >= 2880:
        return f"in {round(minutes / 1440)} days"
    if minutes >= 1440:
        return "tomorrow"
    if minutes >= 120:
        return f"in about {round(minutes / 60)} hours"
    if minutes >= 60:
        return "in about an hour"
    return f"in {minutes} minutes"


def format_money(cents: int, symbol: str = "R") -> str:
    """Money for humans. Whole amounts lose the ".00" — a menu reads R45, not
    R45.00, and a till slip still needs the cents when there are any."""
    cents = int(cents or 0)
    if cents % 100 == 0:
        return f"{symbol}{cents // 100}"
    return f"{symbol}{cents / 100:.2f}"


# Indexed by datetime.weekday() — Monday is 0, matching AgentSchedule.weekday.
WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday",
                 "Friday", "Saturday", "Sunday"]
