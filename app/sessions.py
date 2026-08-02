"""Per-customer conversation state in Redis, plus the per-tenant booking
lock that serialises agent assignment.
"""
import json
import secrets
import time as _time          # aliased — "time" is taken by datetime.time
from contextlib import contextmanager

from app import config

# =============================================================================
# 5. REDIS SESSION HELPERS
# =============================================================================

def get_session(tenant_id: int, customer_num: str) -> dict:
    raw = config.redis_client.get(f"s:{tenant_id}:{customer_num}")
    return json.loads(raw) if raw else {"state": "idle"}


def set_session(tenant_id: int, customer_num: str, data: dict):
    config.redis_client.setex(f"s:{tenant_id}:{customer_num}", config.SESSION_TTL, json.dumps(data))


def clear_session(tenant_id: int, customer_num: str):
    config.redis_client.delete(f"s:{tenant_id}:{customer_num}")


_RELEASE_LOCK_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""


@contextmanager
def booking_lock(tenant_id: int, queue_date: str):
    """
    Serialise agent assignment + queue insert for one tenant on one day.

    Assignment is read-then-write: assign_agent measures every agent's backlog,
    then the caller inserts a row that changes those backlogs. Two bookings
    landing inside that window both see the pre-insert state, both pick the
    same shortest-backlog agent, and both compute the same position. Positions
    self-heal on the next recalculate_queue; the agent choice does not.

    Scoped per tenant per date, so two businesses — or the same business on two
    different dates — never wait on each other.

    Degrades open. If Redis is unreachable, or the lock is still held after
    config.BOOKING_LOCK_WAIT, the booking goes ahead unlocked. That is exactly the
    behaviour before this lock existed, and dropping a real booking is worse
    than a rare double-assignment that staff can see and fix on the dashboard.

    The wait is capped short because handle_webhook is async and calls into
    this synchronously — a long spin here stalls the event loop, and with it
    the outbox drain. Normal hold time is a few writes, tens of milliseconds.

    Yields True if the lock was actually held, False if it degraded open.
    """
    key   = f"lock:booking:{tenant_id}:{queue_date}"
    token = secrets.token_hex(16)
    held  = False
    try:
        deadline = _time.monotonic() + config.BOOKING_LOCK_WAIT
        while True:
            if config.redis_client.set(key, token, nx=True, ex=config.BOOKING_LOCK_TTL):
                held = True
                break
            if _time.monotonic() >= deadline:
                print(f"⚠️  booking lock busy | {key} — proceeding unlocked")
                break
            _time.sleep(config.BOOKING_LOCK_RETRY)
    except Exception as exc:
        print(f"⚠️  booking lock unavailable ({exc}) — proceeding unlocked")

    try:
        yield held
    finally:
        if held:
            try:
                # Compare-and-delete. If our TTL already expired and another
                # booking took the lock, a blind DEL would release theirs.
                config.redis_client.eval(_RELEASE_LOCK_LUA, 1, key, token)
            except Exception as exc:
                print(f"⚠️  booking lock release failed ({exc}) — "
                      f"expires on its own within {config.BOOKING_LOCK_TTL}s")

