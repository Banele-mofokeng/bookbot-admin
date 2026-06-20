"""
Unit tests for QueueBot pure helpers — no DB or Redis required.

Run:  pip install -r requirements-dev.txt && pytest
"""
import os
from datetime import datetime

# Point at a throwaway URL so importing main never touches a real DB.
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")

import main  # noqa: E402


# ── normalize_number ─────────────────────────────────────────────
def test_normalize_local_to_country_code():
    assert main.normalize_number("0812345678") == "27812345678"

def test_normalize_strips_plus_and_spaces():
    assert main.normalize_number("+27 81 234 5678") == "27812345678"

def test_normalize_already_normalized():
    assert main.normalize_number("27812345678") == "27812345678"


# ── parse_arrival_time ───────────────────────────────────────────
def test_parse_now_returns_a_datetime():
    assert isinstance(main.parse_arrival_time("now", "2026-06-20"), datetime)

def test_parse_hh_colon_mm():
    dt = main.parse_arrival_time("14:30", "2026-06-20")
    assert (dt.year, dt.month, dt.day, dt.hour, dt.minute) == (2026, 6, 20, 14, 30)

def test_parse_hhmm_no_colon():
    dt = main.parse_arrival_time("0900", "2026-06-20")
    assert (dt.hour, dt.minute) == (9, 0)

def test_parse_garbage_returns_none():
    assert main.parse_arrival_time("banana", "2026-06-20") is None


# ── format_duration ──────────────────────────────────────────────
def test_format_duration_minutes():
    assert main.format_duration(45) == "~45 min"

def test_format_duration_exact_hours():
    assert main.format_duration(120) == "~2hr"

def test_format_duration_hours_and_minutes():
    assert main.format_duration(90) == "~1hr 30min"


# ── format_eta ───────────────────────────────────────────────────
def test_format_eta_none():
    assert main.format_eta(None) == "TBD"

def test_format_eta_time():
    assert main.format_eta(datetime(2026, 6, 20, 9, 5)) == "09:05"


# ── queue_is_open_today ──────────────────────────────────────────
class _FakeTenant:
    queue_opens = 8
    queue_closes = 17

def test_queue_open_window(monkeypatch):
    monkeypatch.setattr(main, "now", lambda: datetime(2026, 6, 20, 10, 0))
    assert main.queue_is_open_today(_FakeTenant()) is True

def test_queue_closed_before_open(monkeypatch):
    monkeypatch.setattr(main, "now", lambda: datetime(2026, 6, 20, 7, 0))
    assert main.queue_is_open_today(_FakeTenant()) is False

def test_queue_closed_after_close(monkeypatch):
    monkeypatch.setattr(main, "now", lambda: datetime(2026, 6, 20, 17, 0))
    assert main.queue_is_open_today(_FakeTenant()) is False
