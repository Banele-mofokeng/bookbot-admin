"""
Shared test setup. Points the app at a throwaway SQLite file BEFORE main is
imported, so the real engine binds to it. Integration tests then use the same
db.engine. No Postgres or Redis instance required for these tests.
"""
import os
import tempfile

_db_fd, _db_path = tempfile.mkstemp(suffix=".db")
os.environ["DATABASE_URL"] = f"sqlite:///{_db_path}"
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")
# Auth env — must be set before importing main (captured at module load).
os.environ["JWT_SECRET"] = "test-secret"
os.environ["SUPERADMIN_EMAIL"] = "super@test.com"
os.environ["SUPERADMIN_PASSWORD"] = "superpass123"

import threading

import pytest
from sqlmodel import SQLModel
import main
from app import auth, config, db


@pytest.fixture(autouse=True)
def fresh_db():
    """Recreate all tables before each test, with the super-admin seeded."""
    SQLModel.metadata.drop_all(db.engine)
    SQLModel.metadata.create_all(db.engine)
    # Same indexes production runs with, so queries are exercised as deployed.
    db.ensure_indexes()
    # Drop every pooled connection. A SQLite connection carries its own cached
    # copy of the schema; one checked out before this drop/create cycle can go
    # on planning against the old one and ignore indexes that now exist. Fresh
    # connections per test, rather than a subtly stale pool.
    db.engine.dispose()
    auth.seed_superadmin()
    yield


class FakeRedis:
    """
    Enough Redis for the two things the app uses it for: booking_lock
    (SET NX EX plus the compare-and-delete EVAL) and the conversation session
    store (GET / SETEX / DEL).

    Expiry is not modelled. Nothing under test depends on a key ageing out —
    the session TTL exists to tidy up abandoned conversations, not to drive
    behaviour — and a fake clock here would only be a second thing to keep
    honest.
    """

    def __init__(self):
        self.store = {}
        self._lock = threading.Lock()
        self.set_calls = 0

    def set(self, key, value, nx=False, ex=None):
        with self._lock:
            self.set_calls += 1
            if nx and key in self.store:
                return None
            self.store[key] = value
            return True

    def get(self, key):
        with self._lock:
            return self.store.get(key)

    def setex(self, key, ttl, value):
        with self._lock:
            self.store[key] = value
            return True

    def delete(self, key):
        with self._lock:
            return 1 if self.store.pop(key, None) is not None else 0

    def eval(self, script, numkeys, key, arg):
        with self._lock:
            if self.store.get(key) == arg:
                del self.store[key]
                return 1
            return 0


class BrokenRedis:
    def set(self, *a, **k):
        raise ConnectionError("redis down")

    def eval(self, *a, **k):
        raise ConnectionError("redis down")


@pytest.fixture
def fake_redis(monkeypatch):
    """A working lock. Anything that books goes through booking_lock."""
    fake = FakeRedis()
    monkeypatch.setattr(config, "redis_client", fake)
    return fake


@pytest.fixture
def broken_redis(monkeypatch):
    """An unreachable lock — booking_lock is documented to degrade open."""
    monkeypatch.setattr(config, "redis_client", BrokenRedis())


@pytest.fixture
def super_token():
    from fastapi.testclient import TestClient
    c = TestClient(main.app)
    r = c.post("/auth/login", json={"email": "super@test.com", "password": "superpass123"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]
