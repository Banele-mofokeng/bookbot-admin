"""
Per-agent schedules and one-off blocks.

The queue engine used to assume every agent works the tenant's full opening
hours, every day. These tests cover the window layer that replaced that
assumption, and — just as importantly — that an agent with no schedule set
still behaves exactly as before, because every existing deployment is in that
state on the day this ships.

DATE (2026-06-20) is a Saturday, weekday 5. WEEKDAY constants below are used
rather than bare integers so the off-by-one is visible.
"""
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

import main
from main import (Tenant, Service, Agent, AgentService, QueueEntry,
                  AgentSchedule, AgentBlock)


DATE = "2026-06-20"          # Saturday
SATURDAY, SUNDAY, MONDAY = 5, 6, 0
NOW = datetime(2026, 6, 20, 10, 0)


def at(hour, minute=0, day=20):
    return datetime(2026, 6, day, hour, minute)


@pytest.fixture
def frozen_now(monkeypatch):
    monkeypatch.setattr(main, "now", lambda: NOW)
    return NOW


def test_date_really_is_a_saturday():
    """Guards every weekday number in this file."""
    assert datetime.strptime(DATE, "%Y-%m-%d").weekday() == SATURDAY


# ── fixtures ─────────────────────────────────────────────────────────────────
def _tenant(opens=8, closes=17):
    with Session(main.engine) as s:
        t = Tenant(business_name="Sched Co", whatsapp_number="27810000000",
                   evolution_instance="i", evolution_api_key="k",
                   evolution_api_url="http://x",
                   queue_opens=opens, queue_closes=closes, advance_days=1)
        s.add(t); s.commit(); s.refresh(t)
        return t


def _service(tenant_id, minutes):
    with Session(main.engine) as s:
        sv = Service(tenant_id=tenant_id, name=f"S{minutes}", duration_minutes=minutes)
        s.add(sv); s.commit(); s.refresh(sv)
        return sv.id


def _agent(tenant_id, service_ids=(), name="A"):
    with Session(main.engine) as s:
        a = Agent(tenant_id=tenant_id, name=name)
        s.add(a); s.commit(); s.refresh(a)
        for sid in service_ids:
            s.add(AgentService(agent_id=a.id, service_id=sid))
        s.commit()
        return a.id


def _schedule(tenant_id, agent_id, weekday, start_min, end_min):
    with Session(main.engine) as s:
        s.add(AgentSchedule(tenant_id=tenant_id, agent_id=agent_id,
                            weekday=weekday, start_minute=start_min,
                            end_minute=end_min))
        s.commit()


def _block(tenant_id, agent_id, starts_at, ends_at, reason=""):
    with Session(main.engine) as s:
        b = AgentBlock(tenant_id=tenant_id, agent_id=agent_id,
                       starts_at=starts_at, ends_at=ends_at, reason=reason)
        s.add(b); s.commit(); s.refresh(b)
        return b.id


_seq = [0]


@pytest.fixture(autouse=True)
def _reset_seq():
    _seq[0] = 0
    yield


def _entry(tenant_id, service_id, agent_id, *, status="Waiting",
           estimated_start=None, earliest_arrival=None, joined_at=None,
           date=DATE, name="C"):
    _seq[0] += 1
    with Session(main.engine) as s:
        e = QueueEntry(tenant_id=tenant_id, service_id=service_id,
                       agent_id=agent_id, customer_number="2781000",
                       customer_name=name, status=status, queue_date=date,
                       estimated_start=estimated_start,
                       earliest_arrival=earliest_arrival,
                       joined_at=joined_at or (at(7) + timedelta(seconds=_seq[0])))
        s.add(e); s.commit(); s.refresh(e)
        return e.id


def _read(entry_id):
    with Session(main.engine) as s:
        return s.get(QueueEntry, entry_id)


# =============================================================================
# Window resolution
# =============================================================================
def test_agent_with_no_schedule_falls_back_to_tenant_hours():
    """The state every existing agent is in. Nothing may move for them."""
    t = _tenant(opens=8, closes=17); a = _agent(t.id)
    assert main.get_working_windows(t, a, DATE) == [(at(8), at(17))]


def test_agent_with_a_schedule_uses_it_instead_of_tenant_hours():
    t = _tenant(opens=8, closes=17); a = _agent(t.id)
    _schedule(t.id, a, SATURDAY, 9 * 60, 13 * 60)
    assert main.get_working_windows(t, a, DATE) == [(at(9), at(13))]


def test_agent_scheduled_on_other_days_only_is_off_today():
    """A schedule that exists but doesn't cover today means off, not fallback."""
    t = _tenant(); a = _agent(t.id)
    _schedule(t.id, a, MONDAY, 8 * 60, 17 * 60)
    assert main.get_working_windows(t, a, DATE) == []


def test_split_shift_is_two_windows_with_a_real_gap():
    t = _tenant(); a = _agent(t.id)
    _schedule(t.id, a, SATURDAY, 8 * 60, 12 * 60)
    _schedule(t.id, a, SATURDAY, 13 * 60, 17 * 60)
    assert main.get_working_windows(t, a, DATE) == [(at(8), at(12)), (at(13), at(17))]


def test_overlapping_windows_coalesce():
    t = _tenant(); a = _agent(t.id)
    _schedule(t.id, a, SATURDAY, 8 * 60, 12 * 60)
    _schedule(t.id, a, SATURDAY, 11 * 60, 15 * 60)
    assert main.get_working_windows(t, a, DATE) == [(at(8), at(15))]


def test_touching_windows_coalesce():
    t = _tenant(); a = _agent(t.id)
    _schedule(t.id, a, SATURDAY, 8 * 60, 12 * 60)
    _schedule(t.id, a, SATURDAY, 12 * 60, 17 * 60)
    assert main.get_working_windows(t, a, DATE) == [(at(8), at(17))]


def test_a_window_can_run_to_midnight():
    t = _tenant(); a = _agent(t.id)
    _schedule(t.id, a, SATURDAY, 18 * 60, 24 * 60)
    assert main.get_working_windows(t, a, DATE) == [(at(18), at(0, 0, day=21))]


# ── blocks ───────────────────────────────────────────────────────────────────
def test_a_midday_block_splits_the_shift():
    """The lunch-break case."""
    t = _tenant(opens=8, closes=17); a = _agent(t.id)
    _block(t.id, a, at(13), at(14), "Lunch")
    assert main.get_working_windows(t, a, DATE) == [(at(8), at(13)), (at(14), at(17))]


def test_a_block_covering_the_whole_day_leaves_no_windows():
    t = _tenant(); a = _agent(t.id)
    _block(t.id, a, at(0), at(0, 0, day=21), "Leave")
    assert main.get_working_windows(t, a, DATE) == []


def test_a_block_clips_the_start_of_the_day():
    t = _tenant(opens=8, closes=17); a = _agent(t.id)
    _block(t.id, a, at(7), at(10), "Late in")
    assert main.get_working_windows(t, a, DATE) == [(at(10), at(17))]


def test_a_block_clips_the_end_of_the_day():
    t = _tenant(opens=8, closes=17); a = _agent(t.id)
    _block(t.id, a, at(15), at(23), "Early out")
    assert main.get_working_windows(t, a, DATE) == [(at(8), at(15))]


def test_an_overnight_block_only_removes_todays_part():
    t = _tenant(opens=8, closes=17); a = _agent(t.id)
    _block(t.id, a, at(16, 0, day=19), at(10, 0, day=20), "Overnight")
    assert main.get_working_windows(t, a, DATE) == [(at(10), at(17))]


def test_a_block_on_another_day_is_ignored():
    t = _tenant(opens=8, closes=17); a = _agent(t.id)
    _block(t.id, a, at(9, 0, day=21), at(12, 0, day=21), "Tomorrow")
    assert main.get_working_windows(t, a, DATE) == [(at(8), at(17))]


def test_blocks_apply_on_top_of_a_schedule_not_just_the_fallback():
    t = _tenant(); a = _agent(t.id)
    _schedule(t.id, a, SATURDAY, 9 * 60, 17 * 60)
    _block(t.id, a, at(12), at(13))
    assert main.get_working_windows(t, a, DATE) == [(at(9), at(12)), (at(13), at(17))]


def test_two_blocks_cut_two_holes():
    t = _tenant(opens=8, closes=17); a = _agent(t.id)
    _block(t.id, a, at(10), at(11))
    _block(t.id, a, at(14), at(15))
    assert main.get_working_windows(t, a, DATE) == [
        (at(8), at(10)), (at(11), at(14)), (at(15), at(17))]


def test_one_agents_block_does_not_affect_another():
    t = _tenant(opens=8, closes=17)
    a, b = _agent(t.id, name="A"), _agent(t.id, name="B")
    _block(t.id, a, at(12), at(13))

    windows = main.get_working_windows_for([a, b], t, DATE)
    assert windows[a] == [(at(8), at(12)), (at(13), at(17))]
    assert windows[b] == [(at(8), at(17))]


def test_windows_are_batched_flat_in_agent_count():
    """assign_agent resolves these for every candidate on every booking."""
    from tests.test_concurrency import count_queries
    t = _tenant()
    agents = [_agent(t.id, name=f"A{i}") for i in range(6)]

    with count_queries() as one:
        main.get_working_windows_for(agents[:1], t, DATE)
    with count_queries() as many:
        main.get_working_windows_for(agents, t, DATE)

    assert one["n"] == many["n"] <= 2, "\n".join(many["sql"])


# =============================================================================
# place_in_windows
# =============================================================================
def test_place_returns_the_floor_when_it_already_sits_in_a_window():
    assert main.place_in_windows([(at(8), at(17))], at(10), 60) == at(10)


def test_place_jumps_forward_to_the_window_start():
    assert main.place_in_windows([(at(9), at(17))], at(7), 60) == at(9)


def test_place_skips_a_window_too_short_for_the_service():
    """60 minutes won't fit in the 30 left before lunch — start after it."""
    windows = [(at(8), at(12, 30)), (at(13), at(17))]
    assert main.place_in_windows(windows, at(12), 60) == at(13)


def test_place_uses_the_tail_of_a_window_when_it_still_fits():
    windows = [(at(8), at(13)), (at(14), at(17))]
    assert main.place_in_windows(windows, at(12), 60) == at(12)


def test_place_returns_none_when_the_day_is_over():
    assert main.place_in_windows([(at(8), at(17))], at(16, 45), 60) is None


def test_place_returns_none_with_no_windows():
    assert main.place_in_windows([], at(10), 30) is None


def test_place_will_not_run_a_service_through_a_block():
    """The whole point: a 60-min cut cannot start 30 min before lunch."""
    windows = [(at(8), at(13)), (at(14), at(17))]
    assert main.place_in_windows(windows, at(12, 30), 60) == at(14)


# =============================================================================
# assign_agent
# =============================================================================
def test_assign_skips_an_agent_who_is_off_today(frozen_now):
    """Without this an agent on leave has an empty queue, so wins every time."""
    t = _tenant(); svc = _service(t.id, 60)
    off = _agent(t.id, [svc], "Off")
    on  = _agent(t.id, [svc], "On")
    _schedule(t.id, off, MONDAY, 8 * 60, 17 * 60)     # never Saturday
    _entry(t.id, svc, on)                             # 'on' is the busy one

    assert main.assign_agent(t, svc, None, DATE) == on


def test_assign_skips_an_agent_blocked_out_for_the_whole_day(frozen_now):
    t = _tenant(); svc = _service(t.id, 60)
    away = _agent(t.id, [svc], "Away")
    here = _agent(t.id, [svc], "Here")
    _block(t.id, away, at(0), at(0, 0, day=21), "Leave")
    _entry(t.id, svc, here)

    assert main.assign_agent(t, svc, None, DATE) == here


def test_assign_returns_none_when_nobody_is_working(frozen_now):
    t = _tenant(); svc = _service(t.id, 60)
    a = _agent(t.id, [svc])
    _schedule(t.id, a, SUNDAY, 8 * 60, 17 * 60)
    assert main.assign_agent(t, svc, None, DATE) is None


def test_assign_ignores_a_preference_for_an_agent_who_is_off(frozen_now):
    """A named request can't conjure a working day."""
    t = _tenant(); svc = _service(t.id, 60)
    off  = _agent(t.id, [svc], "Off")
    open_ = _agent(t.id, [svc], "Open")
    _schedule(t.id, off, MONDAY, 8 * 60, 17 * 60)

    assert main.assign_agent(t, svc, off, DATE) == open_


def test_assign_still_honours_a_preference_for_a_working_agent(frozen_now):
    t = _tenant(); svc = _service(t.id, 60)
    busy = _agent(t.id, [svc], "Busy")
    _agent(t.id, [svc], "Free")
    _entry(t.id, svc, busy)
    assert main.assign_agent(t, svc, busy, DATE) == busy


def test_assign_is_unchanged_when_nobody_has_a_schedule(frozen_now):
    t = _tenant(); svc = _service(t.id, 60)
    busy, free = _agent(t.id, [svc], "Busy"), _agent(t.id, [svc], "Free")
    _entry(t.id, svc, busy)
    assert main.assign_agent(t, svc, None, DATE) == free


# =============================================================================
# recalculate_queue
# =============================================================================
def test_recalc_pushes_a_booking_past_a_lunch_block(frozen_now):
    """60-min service at 12:30 can't run through 13:00–14:00, so it waits."""
    t = _tenant(opens=8, closes=17); svc = _service(t.id, 60)
    a = _agent(t.id, [svc])
    _block(t.id, a, at(13), at(14), "Lunch")
    e1 = _entry(t.id, svc, a)                       # 10:00–11:00
    e2 = _entry(t.id, svc, a)                       # 11:00–12:00
    e3 = _entry(t.id, svc, a)                       # 12:00–13:00
    e4 = _entry(t.id, svc, a)                       # would be 13:00 -> 14:00

    main.recalculate_queue(t.id, a, DATE)

    assert _read(e1).estimated_start == at(10)
    assert _read(e2).estimated_start == at(11)
    assert _read(e3).estimated_start == at(12)
    assert _read(e4).estimated_start == at(14), "booked straight through lunch"


def test_recalc_starts_at_the_agents_shift_not_the_shop_opening(frozen_now):
    t = _tenant(opens=8, closes=17); svc = _service(t.id, 60)
    a = _agent(t.id, [svc])
    _schedule(t.id, a, SATURDAY, 14 * 60, 18 * 60)   # afternoon shift
    e1 = _entry(t.id, svc, a)

    main.recalculate_queue(t.id, a, DATE)
    assert _read(e1).estimated_start == at(14)


def test_recalc_short_service_fills_the_slot_before_a_block(frozen_now):
    """A 30-min service DOES fit in the half hour before lunch."""
    t = _tenant(opens=8, closes=17); svc = _service(t.id, 30)
    a = _agent(t.id, [svc])
    _block(t.id, a, at(13), at(14))
    for _ in range(5):                               # 10:00,10:30,11:00,11:30,12:00
        _entry(t.id, svc, a)
    last = _entry(t.id, svc, a)                      # 12:30–13:00, exactly fits

    main.recalculate_queue(t.id, a, DATE)
    assert _read(last).estimated_start == at(12, 30)


def test_recalc_overflows_past_the_last_window_rather_than_dropping_anyone(frozen_now):
    """
    An oversubscribed day still gives everyone a time. It reads after hours,
    which is honest — nobody can serve them inside the schedule.
    """
    t = _tenant(opens=8, closes=17); svc = _service(t.id, 60)
    a = _agent(t.id, [svc])
    _schedule(t.id, a, SATURDAY, 10 * 60, 12 * 60)   # two hours only
    e1 = _entry(t.id, svc, a)
    e2 = _entry(t.id, svc, a)
    e3 = _entry(t.id, svc, a)                        # no room left

    main.recalculate_queue(t.id, a, DATE)

    assert _read(e1).estimated_start == at(10)
    assert _read(e2).estimated_start == at(11)
    assert _read(e3).estimated_start == at(12), "customer lost their ETA"


def test_recalc_is_unchanged_for_an_agent_with_no_schedule(frozen_now):
    t = _tenant(opens=8, closes=17); svc = _service(t.id, 60)
    a = _agent(t.id, [svc])
    e1, e2 = _entry(t.id, svc, a), _entry(t.id, svc, a)

    main.recalculate_queue(t.id, a, DATE)
    assert (_read(e1).estimated_start, _read(e2).estimated_start) == (at(10), at(11))


def test_recalc_respects_a_declared_arrival_that_lands_in_a_block(frozen_now):
    """Customer says 13:15, the agent is at lunch until 14:00 — so 14:00."""
    t = _tenant(opens=8, closes=17); svc = _service(t.id, 30)
    a = _agent(t.id, [svc])
    _block(t.id, a, at(13), at(14), "Lunch")
    e = _entry(t.id, svc, a, earliest_arrival=at(13, 15))

    main.recalculate_queue(t.id, a, DATE)
    assert _read(e).estimated_start == at(14)


# =============================================================================
# calculate_estimated_start
# =============================================================================
def test_ces_snaps_a_quote_out_of_a_block(frozen_now):
    t = _tenant(opens=8, closes=17); a = _agent(t.id)
    _block(t.id, a, at(13), at(14), "Lunch")
    # 180 minutes of backlog from 10:00 would land at 13:00, inside lunch.
    assert main.calculate_estimated_start(t, a, DATE, 180) == at(14)


def test_ces_snaps_forward_to_a_late_shift(frozen_now):
    t = _tenant(opens=8, closes=17); a = _agent(t.id)
    _schedule(t.id, a, SATURDAY, 15 * 60, 18 * 60)
    assert main.calculate_estimated_start(t, a, DATE, 0) == at(15)


def test_ces_leaves_an_oversubscribed_quote_alone(frozen_now):
    """Past the last window there is nothing to snap to — say so, don't lie."""
    t = _tenant(opens=8, closes=17); a = _agent(t.id)
    _schedule(t.id, a, SATURDAY, 8 * 60, 12 * 60)
    assert main.calculate_estimated_start(t, a, DATE, 600) == at(20)


def test_ces_unchanged_without_a_schedule(frozen_now):
    t = _tenant(opens=8, closes=17); a = _agent(t.id)
    assert main.calculate_estimated_start(t, a, DATE, 90) == at(11, 30)


# =============================================================================
# find_walkin_insert_joined_at
# =============================================================================
def test_gapfill_will_not_slot_a_walk_in_into_a_block(frozen_now):
    """
    The 15:00 booking leaves a gap, but the agent is at lunch 10:00–15:00, so
    a 30-min walk-in placed at 14:45 would overrun it. No slot.
    """
    t = _tenant(opens=8, closes=17); svc = _service(t.id, 30)
    a = _agent(t.id, [svc])
    _block(t.id, a, at(10), at(14, 45), "Out")
    _entry(t.id, svc, a, earliest_arrival=at(15), joined_at=at(9))

    assert main.find_walkin_insert_joined_at(a, t.id, t, DATE, svc) is None


def test_gapfill_still_works_when_the_block_is_after_the_gap(frozen_now):
    t = _tenant(opens=8, closes=17); svc = _service(t.id, 30)
    a = _agent(t.id, [svc])
    _block(t.id, a, at(15), at(16), "Later")
    _entry(t.id, svc, a, earliest_arrival=at(14), joined_at=at(9))

    assert main.find_walkin_insert_joined_at(a, t.id, t, DATE, svc) == \
        at(9) - timedelta(seconds=1)


def test_gapfill_returns_none_when_the_agent_is_off(frozen_now):
    t = _tenant(); svc = _service(t.id, 30); a = _agent(t.id, [svc])
    _schedule(t.id, a, MONDAY, 8 * 60, 17 * 60)
    _entry(t.id, svc, a, earliest_arrival=at(14), joined_at=at(9))

    assert main.find_walkin_insert_joined_at(a, t.id, t, DATE, svc) is None


# =============================================================================
# Admin API
# =============================================================================
@pytest.fixture
def client():
    return TestClient(main.app)


@pytest.fixture
def hdr(super_token):
    return {"Authorization": f"Bearer {super_token}"}


def test_schedule_defaults_to_tenant_hours(client, hdr):
    t = _tenant(); a = _agent(t.id)
    r = client.get(f"/admin/agents/{a}/schedule", headers=hdr)
    assert r.status_code == 200
    assert r.json() == {"agent_id": a, "uses_tenant_hours": True, "windows": []}


def test_put_schedule_replaces_everything(client, hdr):
    t = _tenant(); a = _agent(t.id)
    client.put(f"/admin/agents/{a}/schedule", headers=hdr, json={"windows": [
        {"weekday": MONDAY, "start_minute": 8 * 60, "end_minute": 17 * 60},
    ]})
    r = client.put(f"/admin/agents/{a}/schedule", headers=hdr, json={"windows": [
        {"weekday": SATURDAY, "start_minute": 9 * 60, "end_minute": 13 * 60},
    ]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["uses_tenant_hours"] is False
    assert [(w["weekday"], w["start"], w["end"]) for w in body["windows"]] == \
        [(SATURDAY, "09:00", "13:00")]


def test_put_empty_schedule_restores_tenant_hours(client, hdr):
    t = _tenant(); a = _agent(t.id)
    client.put(f"/admin/agents/{a}/schedule", headers=hdr, json={"windows": [
        {"weekday": SATURDAY, "start_minute": 540, "end_minute": 780}]})
    r = client.put(f"/admin/agents/{a}/schedule", headers=hdr, json={"windows": []})

    assert r.json()["uses_tenant_hours"] is True
    assert main.get_working_windows(_tenant_row(t.id), a, DATE) == [(at(8), at(17))]


def _tenant_row(tenant_id):
    with Session(main.engine) as s:
        return s.get(Tenant, tenant_id)


@pytest.mark.parametrize("window, why", [
    ({"weekday": 7, "start_minute": 0, "end_minute": 60}, "weekday out of range"),
    ({"weekday": -1, "start_minute": 0, "end_minute": 60}, "negative weekday"),
    ({"weekday": 0, "start_minute": 600, "end_minute": 600}, "zero length"),
    ({"weekday": 0, "start_minute": 700, "end_minute": 600}, "end before start"),
    ({"weekday": 0, "start_minute": -1, "end_minute": 600}, "negative start"),
    ({"weekday": 0, "start_minute": 0, "end_minute": 1441}, "past midnight"),
    ({"weekday": 0, "start_minute": 0}, "missing end_minute"),
])
def test_put_schedule_rejects_bad_windows(client, hdr, window, why):
    t = _tenant(); a = _agent(t.id)
    r = client.put(f"/admin/agents/{a}/schedule", headers=hdr,
                   json={"windows": [window]})
    assert r.status_code == 400, f"{why} was accepted"


def test_put_schedule_rejects_a_non_list(client, hdr):
    t = _tenant(); a = _agent(t.id)
    r = client.put(f"/admin/agents/{a}/schedule", headers=hdr,
                   json={"windows": "all day"})
    assert r.status_code == 400


def test_put_schedule_is_atomic_on_a_bad_window(client, hdr):
    """One invalid window must not wipe the schedule that was already there."""
    t = _tenant(); a = _agent(t.id)
    client.put(f"/admin/agents/{a}/schedule", headers=hdr, json={"windows": [
        {"weekday": SATURDAY, "start_minute": 540, "end_minute": 780}]})

    client.put(f"/admin/agents/{a}/schedule", headers=hdr, json={"windows": [
        {"weekday": MONDAY, "start_minute": 480, "end_minute": 1020},
        {"weekday": 99, "start_minute": 0, "end_minute": 60},
    ]})

    assert len(client.get(f"/admin/agents/{a}/schedule",
                          headers=hdr).json()["windows"]) == 1


def test_setting_a_schedule_restates_todays_etas(client, hdr, monkeypatch):
    """A shift change has to move the customers already quoted the old one."""
    today = main.today_str()
    monkeypatch.setattr(main, "now",
                        lambda: datetime.strptime(today, "%Y-%m-%d").replace(hour=10))
    t = _tenant(opens=8, closes=17); svc = _service(t.id, 60)
    a = _agent(t.id, [svc])
    e = _entry(t.id, svc, a, date=today)
    main.recalculate_queue(t.id, a, today)
    assert _read(e).estimated_start.hour == 10

    weekday = datetime.strptime(today, "%Y-%m-%d").weekday()
    client.put(f"/admin/agents/{a}/schedule", headers=hdr, json={"windows": [
        {"weekday": weekday, "start_minute": 15 * 60, "end_minute": 18 * 60}]})

    assert _read(e).estimated_start.hour == 15


def test_block_crud_round_trip(client, hdr):
    t = _tenant(); a = _agent(t.id)
    r = client.post(f"/admin/agents/{a}/blocks", headers=hdr, json={
        "starts_at": at(13).isoformat(), "ends_at": at(14).isoformat(),
        "reason": "Lunch"})
    assert r.status_code == 200, r.text
    block_id = r.json()["id"]

    listed = client.get(f"/admin/agents/{a}/blocks?from_date={DATE}", headers=hdr)
    assert [b["id"] for b in listed.json()] == [block_id]
    assert main.get_working_windows(_tenant_row(t.id), a, DATE) == \
        [(at(8), at(13)), (at(14), at(17))]

    assert client.delete(f"/admin/blocks/{block_id}", headers=hdr).status_code == 200
    assert main.get_working_windows(_tenant_row(t.id), a, DATE) == [(at(8), at(17))]


@pytest.mark.parametrize("body", [
    {"starts_at": "2026-06-20T14:00:00", "ends_at": "2026-06-20T13:00:00"},
    {"starts_at": "2026-06-20T13:00:00", "ends_at": "2026-06-20T13:00:00"},
    {"starts_at": "not a date", "ends_at": "2026-06-20T13:00:00"},
    {"ends_at": "2026-06-20T13:00:00"},
])
def test_create_block_rejects_bad_input(client, hdr, body):
    t = _tenant(); a = _agent(t.id)
    assert client.post(f"/admin/agents/{a}/blocks",
                       headers=hdr, json=body).status_code == 400


def test_windows_endpoint_reports_what_the_engine_sees(client, hdr):
    t = _tenant(opens=8, closes=17); a = _agent(t.id)
    _block(t.id, a, at(13), at(14), "Lunch")

    body = client.get(f"/admin/agents/{a}/windows?queue_date={DATE}",
                      headers=hdr).json()
    assert body["working"] is True
    assert [(w["start"], w["minutes"]) for w in body["windows"]] == [
        (at(8).isoformat(), 300), (at(14).isoformat(), 180)]


def test_windows_endpoint_reports_a_day_off(client, hdr):
    t = _tenant(); a = _agent(t.id)
    _schedule(t.id, a, MONDAY, 8 * 60, 17 * 60)
    body = client.get(f"/admin/agents/{a}/windows?queue_date={DATE}",
                      headers=hdr).json()
    assert body == {"agent_id": a, "queue_date": DATE, "working": False, "windows": []}


def test_schedule_routes_require_auth(client):
    t = _tenant(); a = _agent(t.id)
    for method, path in [
        ("get", f"/admin/agents/{a}/schedule"),
        ("put", f"/admin/agents/{a}/schedule"),
        ("get", f"/admin/agents/{a}/blocks"),
        ("post", f"/admin/agents/{a}/blocks"),
        ("delete", "/admin/blocks/1"),
        ("get", f"/admin/agents/{a}/windows"),
    ]:
        kwargs = {"json": {}} if method in ("put", "post") else {}
        r = getattr(client, method)(path, **kwargs)
        assert r.status_code in (401, 403), f"{method} {path} was open"


def test_a_tenant_user_cannot_touch_another_tenants_agent(client, hdr):
    """Schedules are tenant-scoped like everything else under /admin."""
    mine = _tenant(); theirs = _tenant()
    their_agent = _agent(theirs.id)

    client.post("/admin/users", headers=hdr, json={
        "email": "client@test.com", "password": "clientpass123",
        "tenant_id": mine.id})
    token = client.post("/auth/login", json={
        "email": "client@test.com", "password": "clientpass123"}).json()["access_token"]
    theirs_hdr = {"Authorization": f"Bearer {token}"}

    assert client.get(f"/admin/agents/{their_agent}/schedule",
                      headers=theirs_hdr).status_code == 403
    assert client.put(f"/admin/agents/{their_agent}/schedule", headers=theirs_hdr,
                      json={"windows": []}).status_code == 403


def test_schedule_404s_for_an_unknown_agent(client, hdr):
    assert client.get("/admin/agents/999999/schedule", headers=hdr).status_code == 404
