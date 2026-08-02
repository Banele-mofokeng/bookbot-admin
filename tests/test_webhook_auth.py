"""
Webhook authentication. Every call posts a non-'messages.upsert' event so the
handler returns early with {"status": "ignored"} — that exercises the auth
dependency without needing Redis or a seeded tenant.
"""
import pytest
from fastapi.testclient import TestClient

import main

# Anything that isn't messages.upsert short-circuits at the top of the handler.
IGNORED_EVENT = {"event": "connection.update", "data": {}}


@pytest.fixture
def client():
    return TestClient(main.app)


@pytest.fixture
def secret(monkeypatch):
    """Turn webhook auth on for one test."""
    monkeypatch.setattr(main, "WEBHOOK_SECRETS", ["s3cret"])
    return "s3cret"


def test_open_when_no_secret_configured(client, monkeypatch):
    """Unconfigured deployments keep working — the upgrade must not go silent."""
    monkeypatch.setattr(main, "WEBHOOK_SECRETS", [])
    r = client.post("/webhook", json=IGNORED_EVENT)
    assert r.status_code == 200
    assert r.json() == {"status": "ignored"}


def test_rejects_missing_credentials(client, secret):
    assert client.post("/webhook", json=IGNORED_EVENT).status_code == 401


def test_rejects_wrong_secret(client, secret):
    r = client.post("/webhook", json=IGNORED_EVENT,
                    headers={"X-Webhook-Token": "wrong"})
    assert r.status_code == 401


def test_rejects_empty_token_header(client, secret):
    r = client.post("/webhook", json=IGNORED_EVENT, headers={"X-Webhook-Token": "  "})
    assert r.status_code == 401


def test_accepts_token_header(client, secret):
    r = client.post("/webhook", json=IGNORED_EVENT,
                    headers={"X-Webhook-Token": secret})
    assert r.status_code == 200


def test_accepts_bearer_header(client, secret):
    r = client.post("/webhook", json=IGNORED_EVENT,
                    headers={"Authorization": f"Bearer {secret}"})
    assert r.status_code == 200


def test_accepts_query_param(client, secret):
    """The fallback that always works — the webhook URL itself is editable."""
    r = client.post(f"/webhook?token={secret}", json=IGNORED_EVENT)
    assert r.status_code == 200


def test_rejects_wrong_query_param(client, secret):
    assert client.post("/webhook?token=wrong", json=IGNORED_EVENT).status_code == 401


def test_rotation_accepts_both_secrets(client, monkeypatch):
    """'old,new' lets Evolution be repointed without dropping bookings."""
    monkeypatch.setattr(main, "WEBHOOK_SECRETS", ["old", "new"])
    for token in ("old", "new"):
        r = client.post("/webhook", json=IGNORED_EVENT,
                        headers={"X-Webhook-Token": token})
        assert r.status_code == 200, token
    assert client.post("/webhook", json=IGNORED_EVENT,
                       headers={"X-Webhook-Token": "retired"}).status_code == 401


def test_non_ascii_secret_is_rejected_not_crashed(client, secret):
    """
    compare_digest raises TypeError on non-ASCII str — the encode() guard turns
    that into a clean 401. Only reachable via the query param; header values
    can't carry non-ASCII.
    """
    r = client.post("/webhook?token=s%C3%A9cret", json=IGNORED_EVENT)
    assert r.status_code == 401


def test_health_reports_webhook_auth_state(client, monkeypatch):
    monkeypatch.setattr(main, "WEBHOOK_SECRETS", ["s3cret"])
    assert client.get("/health").json()["webhook_auth"] is True
    monkeypatch.setattr(main, "WEBHOOK_SECRETS", [])
    assert client.get("/health").json()["webhook_auth"] is False
