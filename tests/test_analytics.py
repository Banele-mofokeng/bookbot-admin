"""
Owner analytics.

Most of these are about what the endpoint refuses to say. The queue has one
trap in it: `midnight_reset_job` closes anything staff left open as `NoShow`.
Counted naively, a shop that forgets to tap Done looks like a shop whose
customers don't turn up — the report would measure staff habits while claiming
to measure customers. So no-shows are split by who closed the entry, and the
rate is withheld outright for any range containing rows from before that was
tracked.

The rest guard the same instinct: rates over closed entries only (an open queue
must not drag them down as the day runs), and measured durations reported as
unavailable rather than zero when there are no samples.
"""
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

import main
from app import core, db, jobs, queue_engine
from app.models import Tenant, Service, Agent, AgentService, QueueEntry


DATE = "2026-06-20"


@pytest.fixture
def client():
    return TestClient(main.app)


@pytest.fixture
def hdr(super_token):
    return {"Authorization": f"Bearer {super_token}"}


def _seed(n_services=1):
    with Session(db.engine) as s:
        t = Tenant(business_name="Report Co", whatsapp_number="27810000000",
                   evolution_instance="i", evolution_api_key="k",
                   evolution_api_url="http://x", queue_opens=8, queue_closes=17)
        s.add(t); s.commit(); s.refresh(t)
        svc_ids = []
        for i in range(n_services):
            sv = Service(tenant_id=t.id, name=f"Svc{i}", duration_minutes=30 + i * 30)
            s.add(sv); s.commit(); s.refresh(sv)
            svc_ids.append(sv.id)
        a = Agent(tenant_id=t.id, name="Nomsa")
        s.add(a); s.commit(); s.refresh(a)
        for sid in svc_ids:
            s.add(AgentService(agent_id=a.id, service_id=sid))
        s.commit()
        return t.id, svc_ids, a.id


_seq = [0]


@pytest.fixture(autouse=True)
def _reset_seq():
    _seq[0] = 0
    yield


def _entry(tenant_id, svc, agent, *, status="Done", closed_by="staff",
           date=DATE, via="whatsapp", joined_at=None, started_at=None,
           finished_at=None):
    _seq[0] += 1
    with Session(db.engine) as s:
        e = QueueEntry(
            tenant_id=tenant_id, service_id=svc, agent_id=agent,
            customer_number=f"2781{_seq[0]}", customer_name=f"C{_seq[0]}",
            status=status, closed_by=closed_by, queue_date=date, booked_via=via,
            joined_at=joined_at or datetime(2026, 6, 20, 9, 0),
            started_at=started_at, finished_at=finished_at,
        )
        s.add(e); s.commit(); s.refresh(e)
        return e.id


def _get(client, hdr, tenant_id, **params):
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    r = client.get(f"/admin/analytics/{tenant_id}{'?' + qs if qs else ''}", headers=hdr)
    assert r.status_code == 200, r.text
    return r.json()


# =============================================================================
# The no-show trap
# =============================================================================
def test_a_sweep_closed_entry_is_not_counted_as_a_no_show(client, hdr):
    """
    The whole reason this endpoint needed care. Staff never tapped Done, so the
    midnight sweep closed it — nobody knows whether the customer arrived.
    """
    tid, [svc], a = _seed()
    _entry(tid, svc, a, status="NoShow", closed_by="system")

    body = _get(client, hdr, tid, from_date=DATE, to_date=DATE)
    assert body["totals"]["no_shows"] == 0
    assert body["totals"]["unclosed"] == 1
    assert body["rates"]["no_show"] == 0.0
    assert body["rates"]["unclosed"] == 1.0


def test_a_staff_marked_no_show_is_counted(client, hdr):
    tid, [svc], a = _seed()
    _entry(tid, svc, a, status="NoShow", closed_by="staff")

    body = _get(client, hdr, tid, from_date=DATE, to_date=DATE)
    assert body["totals"]["no_shows"] == 1
    assert body["totals"]["unclosed"] == 0
    assert body["rates"]["no_show"] == 1.0


def test_the_two_kinds_are_reported_side_by_side(client, hdr):
    tid, [svc], a = _seed()
    for _ in range(3):
        _entry(tid, svc, a, status="Done")
    _entry(tid, svc, a, status="NoShow", closed_by="staff")
    for _ in range(4):
        _entry(tid, svc, a, status="NoShow", closed_by="system")

    body = _get(client, hdr, tid, from_date=DATE, to_date=DATE)
    assert body["totals"]["done"] == 3
    assert body["totals"]["no_shows"] == 1
    assert body["totals"]["unclosed"] == 4
    assert body["rates"]["no_show"] == 0.125         # 1 of 8, not 5 of 8
    assert body["data_quality"]["unclosed"] == 4


def test_the_no_show_rate_is_withheld_when_the_range_has_untracked_rows(client, hdr):
    """
    Legacy rows carry closed_by = ''. A rate computed over them would read low
    and look authoritative. Better to return null and say why.
    """
    tid, [svc], a = _seed()
    _entry(tid, svc, a, status="Done")
    _entry(tid, svc, a, status="NoShow", closed_by="")     # pre-upgrade row

    body = _get(client, hdr, tid, from_date=DATE, to_date=DATE)
    assert body["rates"]["no_show"] is None
    assert "predate" in body["rates"]["no_show_note"]
    assert body["totals"]["no_shows_untracked"] == 1
    # Everything not affected by the ambiguity still reports.
    assert body["rates"]["completion"] == 0.5


def test_the_note_is_absent_once_every_row_is_tracked(client, hdr):
    tid, [svc], a = _seed()
    _entry(tid, svc, a, status="Done")
    body = _get(client, hdr, tid, from_date=DATE, to_date=DATE)
    assert body["rates"]["no_show_note"] is None
    assert body["rates"]["no_show"] == 0.0


def test_a_customer_cancellation_is_distinguished_from_a_staff_one(client, hdr):
    tid, [svc], a = _seed()
    _entry(tid, svc, a, status="Cancelled", closed_by="customer")
    _entry(tid, svc, a, status="Cancelled", closed_by="staff")

    body = _get(client, hdr, tid, from_date=DATE, to_date=DATE)
    assert body["totals"]["cancelled"] == 2
    assert body["totals"]["cancelled_by_customer"] == 1


# =============================================================================
# Rates
# =============================================================================
def test_an_open_queue_does_not_drag_the_rates_down(client, hdr):
    """
    Rates are over closed entries. Otherwise a shop's completion rate would
    fall through the morning and recover by closing time, meaning nothing.
    """
    tid, [svc], a = _seed()
    _entry(tid, svc, a, status="Done")
    for _ in range(9):
        _entry(tid, svc, a, status="Waiting", closed_by="")

    body = _get(client, hdr, tid, from_date=DATE, to_date=DATE)
    assert body["totals"]["bookings"] == 10
    assert body["totals"]["still_open"] == 9
    assert body["totals"]["closed"] == 1
    assert body["rates"]["completion"] == 1.0


def test_rates_are_null_when_nothing_has_closed(client, hdr):
    """Zero would read as 'nobody completed', which is not what happened."""
    tid, [svc], a = _seed()
    _entry(tid, svc, a, status="Waiting", closed_by="")

    body = _get(client, hdr, tid, from_date=DATE, to_date=DATE)
    assert body["rates"]["completion"] is None
    assert body["rates"]["cancellation"] is None


def test_an_empty_range_returns_zeros_not_an_error(client, hdr):
    tid, _, _ = _seed()
    body = _get(client, hdr, tid, from_date="2026-01-01", to_date="2026-01-07")
    assert body["totals"]["bookings"] == 0
    assert body["by_day"] == []
    assert body["rates"]["completion"] is None


# =============================================================================
# Measured durations
# =============================================================================
def test_wait_and_service_times_report_unavailable_without_samples(client, hdr):
    """Rows from before timestamping have no started_at. Say so, don't show 0."""
    tid, [svc], a = _seed()
    _entry(tid, svc, a, status="Done", started_at=None, finished_at=None)

    body = _get(client, hdr, tid, from_date=DATE, to_date=DATE)
    assert body["wait_time"] == {"available": False, "samples": 0,
                                 "median_minutes": None, "p90_minutes": None}
    assert body["service_time"]["available"] is False


def test_wait_time_is_measured_from_joining_to_being_seen(client, hdr):
    tid, [svc], a = _seed()
    joined = datetime(2026, 6, 20, 9, 0)
    for offset in (10, 20, 30):
        _entry(tid, svc, a, status="Done", joined_at=joined,
               started_at=joined + timedelta(minutes=offset),
               finished_at=joined + timedelta(minutes=offset + 45))

    body = _get(client, hdr, tid, from_date=DATE, to_date=DATE)
    assert body["wait_time"]["available"] is True
    assert body["wait_time"]["samples"] == 3
    assert body["wait_time"]["median_minutes"] == 20


def test_service_time_is_measured_only_for_completed_work(client, hdr):
    """A no-show has a start and an end, but nothing was served."""
    tid, [svc], a = _seed()
    started = datetime(2026, 6, 20, 9, 0)
    _entry(tid, svc, a, status="Done", started_at=started,
           finished_at=started + timedelta(minutes=60))
    _entry(tid, svc, a, status="NoShow", closed_by="staff", started_at=started,
           finished_at=started + timedelta(minutes=2))

    body = _get(client, hdr, tid, from_date=DATE, to_date=DATE)
    assert body["service_time"]["samples"] == 1
    assert body["service_time"]["median_minutes"] == 60


def test_a_clock_skewed_negative_duration_is_dropped(client, hdr):
    tid, [svc], a = _seed()
    joined = datetime(2026, 6, 20, 9, 0)
    _entry(tid, svc, a, status="Done", joined_at=joined,
           started_at=joined - timedelta(minutes=5),
           finished_at=joined + timedelta(minutes=30))

    body = _get(client, hdr, tid, from_date=DATE, to_date=DATE)
    assert body["wait_time"]["samples"] == 0


def test_p90_is_not_dragged_up_by_a_single_outlier(client, hdr):
    """
    Nine 5-minute waits and one 120. p90 answers "90% waited no longer than
    this", which is 5 — the lone slow one does not set it. A p90 of 120 here
    would tell an owner to hire, on the strength of one bad morning.
    """
    tid, [svc], a = _seed()
    joined = datetime(2026, 6, 20, 9, 0)
    for m in [5] * 9 + [120]:
        _entry(tid, svc, a, status="Done", joined_at=joined,
               started_at=joined + timedelta(minutes=m))

    body = _get(client, hdr, tid, from_date=DATE, to_date=DATE)
    assert body["wait_time"]["median_minutes"] == 5
    assert body["wait_time"]["p90_minutes"] == 5


def test_p90_does_pick_up_a_real_slow_tail(client, hdr):
    """Two in ten slow is a tail, not an outlier — p90 must show it."""
    tid, [svc], a = _seed()
    joined = datetime(2026, 6, 20, 9, 0)
    for m in [5] * 8 + [90, 120]:
        _entry(tid, svc, a, status="Done", joined_at=joined,
               started_at=joined + timedelta(minutes=m))

    body = _get(client, hdr, tid, from_date=DATE, to_date=DATE)
    assert body["wait_time"]["p90_minutes"] == 90


# =============================================================================
# Breakdowns
# =============================================================================
def test_breakdown_by_day(client, hdr):
    tid, [svc], a = _seed()
    _entry(tid, svc, a, date="2026-06-19", status="Done")
    _entry(tid, svc, a, date="2026-06-20", status="Done")
    _entry(tid, svc, a, date="2026-06-20", status="NoShow", closed_by="staff")

    body = _get(client, hdr, tid, from_date="2026-06-19", to_date="2026-06-20")
    assert [(d["date"], d["bookings"], d["done"]) for d in body["by_day"]] == [
        ("2026-06-19", 1, 1), ("2026-06-20", 2, 1)]
    assert body["by_day"][0]["weekday"] == "Fri"


def test_breakdown_by_weekday_and_hour(client, hdr):
    tid, [svc], a = _seed()
    _entry(tid, svc, a, joined_at=datetime(2026, 6, 20, 14, 30))
    _entry(tid, svc, a, joined_at=datetime(2026, 6, 20, 14, 45))
    _entry(tid, svc, a, joined_at=datetime(2026, 6, 20, 9, 15))

    body = _get(client, hdr, tid, from_date=DATE, to_date=DATE)
    hours = {h["hour"]: h["bookings"] for h in body["by_hour"]}
    assert hours[14] == 2 and hours[9] == 1
    assert len(body["by_hour"]) == 24, "every hour present so the chart has no gaps"

    saturday = next(w for w in body["by_weekday"] if w["name"] == "Saturday")
    assert saturday["bookings"] == 3


def test_breakdown_by_agent_splits_no_shows_from_unclosed(client, hdr):
    tid, [svc], a = _seed()
    _entry(tid, svc, a, status="Done")
    _entry(tid, svc, a, status="NoShow", closed_by="staff")
    _entry(tid, svc, a, status="NoShow", closed_by="system")

    row = _get(client, hdr, tid, from_date=DATE, to_date=DATE)["by_agent"][0]
    assert row["name"] == "Nomsa"
    assert (row["bookings"], row["done"], row["no_shows"], row["unclosed"]) == (3, 1, 1, 1)


def test_breakdown_by_service_totals_scheduled_minutes(client, hdr):
    tid, svcs, a = _seed(n_services=2)     # 30 min and 60 min
    _entry(tid, svcs[0], a)
    _entry(tid, svcs[0], a)
    _entry(tid, svcs[1], a)

    rows = {r["name"]: r for r in _get(client, hdr, tid,
                                       from_date=DATE, to_date=DATE)["by_service"]}
    assert rows["Svc0"]["bookings"] == 2 and rows["Svc0"]["minutes_booked"] == 60
    assert rows["Svc1"]["minutes_booked"] == 60


def test_breakdowns_survive_a_deleted_agent_or_service(client, hdr):
    tid, [svc], a = _seed()
    _entry(tid, svc, a)
    with Session(db.engine) as s:
        s.delete(s.get(Agent, a)); s.delete(s.get(Service, svc)); s.commit()

    body = _get(client, hdr, tid, from_date=DATE, to_date=DATE)
    assert body["by_agent"][0]["name"] == "(removed)"
    assert body["by_service"][0]["name"] == "(removed)"


def test_channel_split(client, hdr):
    tid, [svc], a = _seed()
    _entry(tid, svc, a, via="whatsapp")
    _entry(tid, svc, a, via="whatsapp")
    _entry(tid, svc, a, via="walkin")

    assert _get(client, hdr, tid, from_date=DATE, to_date=DATE)["channel"] == \
        {"whatsapp": 2, "walkin": 1}


def test_agent_rows_are_ranked_by_volume(client, hdr):
    tid, [svc], a = _seed()
    with Session(db.engine) as s:
        b = Agent(tenant_id=tid, name="Thabo")
        s.add(b); s.commit(); s.refresh(b)
        bid = b.id
    _entry(tid, svc, a)
    for _ in range(3):
        _entry(tid, svc, bid)

    names = [r["name"] for r in _get(client, hdr, tid,
                                     from_date=DATE, to_date=DATE)["by_agent"]]
    assert names == ["Thabo", "Nomsa"]


# =============================================================================
# Range handling, scoping, auth
# =============================================================================
def test_default_range_is_the_last_30_days(client, hdr):
    tid, _, _ = _seed()
    body = _get(client, hdr, tid)
    assert body["days"] == 30
    assert body["to_date"] == core.today_str()


def test_other_tenants_entries_are_never_counted(client, hdr):
    mine, [my_svc], my_agent = _seed()
    theirs, [their_svc], their_agent = _seed()
    _entry(mine, my_svc, my_agent)
    for _ in range(5):
        _entry(theirs, their_svc, their_agent)

    assert _get(client, hdr, mine, from_date=DATE, to_date=DATE)["totals"]["bookings"] == 1


def test_dates_outside_the_range_are_excluded(client, hdr):
    tid, [svc], a = _seed()
    _entry(tid, svc, a, date="2026-06-19")
    _entry(tid, svc, a, date="2026-06-20")
    _entry(tid, svc, a, date="2026-06-21")

    body = _get(client, hdr, tid, from_date="2026-06-20", to_date="2026-06-20")
    assert body["totals"]["bookings"] == 1


@pytest.mark.parametrize("params, why", [
    ({"from_date": "2026-06-20", "to_date": "2026-06-19"}, "reversed range"),
    ({"from_date": "2020-01-01", "to_date": "2026-06-20"}, "over the day cap"),
    ({"from_date": "not-a-date", "to_date": "2026-06-20"}, "unparseable"),
])
def test_bad_ranges_are_rejected(client, hdr, params, why):
    tid, _, _ = _seed()
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    r = client.get(f"/admin/analytics/{tid}?{qs}", headers=hdr)
    assert r.status_code == 400, f"{why} was accepted"


def test_a_single_day_range_is_valid(client, hdr):
    tid, _, _ = _seed()
    assert _get(client, hdr, tid, from_date=DATE, to_date=DATE)["days"] == 1


def test_analytics_requires_auth(client):
    tid, _, _ = _seed()
    assert client.get(f"/admin/analytics/{tid}").status_code in (401, 403)


def test_a_tenant_user_cannot_read_another_businesss_analytics(client, hdr):
    mine, _, _ = _seed()
    theirs, _, _ = _seed()
    client.post("/admin/users", headers=hdr, json={
        "email": "owner@test.com", "password": "ownerpass123", "tenant_id": mine})
    token = client.post("/auth/login", json={
        "email": "owner@test.com", "password": "ownerpass123"}).json()["access_token"]
    their_hdr = {"Authorization": f"Bearer {token}"}

    assert client.get(f"/admin/analytics/{mine}", headers=their_hdr).status_code == 200
    assert client.get(f"/admin/analytics/{theirs}", headers=their_hdr).status_code == 403


def test_analytics_query_count_is_flat_in_rows(client, hdr):
    """One pass over the range plus two batched name lookups — never per row."""
    from tests.test_concurrency import count_queries
    tid, [svc], a = _seed()

    for _ in range(3):
        _entry(tid, svc, a)
    with count_queries() as small:
        _get(client, hdr, tid, from_date=DATE, to_date=DATE)

    for _ in range(40):
        _entry(tid, svc, a)
    with count_queries() as big:
        _get(client, hdr, tid, from_date=DATE, to_date=DATE)

    assert small["n"] == big["n"], (
        f"{small['n']} -> {big['n']}\n" + "\n".join(big["sql"]))


# =============================================================================
# Provenance is actually written by the app, not just readable
# =============================================================================
def test_marking_done_records_staff_and_a_finish_time(client, hdr):
    tid, [svc], a = _seed()
    eid = _entry(tid, svc, a, status="Waiting", closed_by="")

    assert client.patch(f"/admin/queue/{eid}/status", headers=hdr,
                        json={"status": "Done"}).status_code == 200
    with Session(db.engine) as s:
        e = s.get(QueueEntry, eid)
    assert e.closed_by == "staff" and e.finished_at is not None


def test_going_into_service_records_a_start_time(client, hdr):
    tid, [svc], a = _seed()
    eid = _entry(tid, svc, a, status="Waiting", closed_by="")

    client.patch(f"/admin/queue/{eid}/status", headers=hdr, json={"status": "InService"})
    with Session(db.engine) as s:
        first = s.get(QueueEntry, eid).started_at
    assert first is not None

    # Re-entering service (staff mis-click, then back) must not reset the clock.
    client.patch(f"/admin/queue/{eid}/status", headers=hdr, json={"status": "InService"})
    with Session(db.engine) as s:
        assert s.get(QueueEntry, eid).started_at == first


@pytest.mark.asyncio
async def test_the_midnight_sweep_tags_its_own_closes_as_system():
    """The trap, closed at the source."""
    tid, [svc], a = _seed()
    eid = _entry(tid, svc, a, status="Waiting", closed_by="",
                 date=core.yesterday_str())

    await jobs.midnight_reset_job()

    with Session(db.engine) as s:
        e = s.get(QueueEntry, eid)
    assert e.status == "NoShow"
    assert e.closed_by == "system", "would have been read as a real no-show"
    assert e.finished_at is not None


def test_a_whatsapp_cancellation_is_tagged_to_the_customer():
    tid, [svc], a = _seed()
    eid = _entry(tid, svc, a, status="Waiting", closed_by="")

    queue_engine.cancel_party(tid, eid)

    with Session(db.engine) as s:
        e = s.get(QueueEntry, eid)
    assert e.status == "Cancelled" and e.closed_by == "customer"
