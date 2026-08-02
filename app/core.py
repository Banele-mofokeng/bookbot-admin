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


# Indexed by datetime.weekday() — Monday is 0, matching AgentSchedule.weekday.
WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday",
                 "Friday", "Saturday", "Sunday"]
