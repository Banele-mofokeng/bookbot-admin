"""
Shared test setup. Points the app at a throwaway SQLite file BEFORE main is
imported, so the real engine binds to it. Integration tests then use the same
main.engine. No Postgres or Redis instance required for these tests.
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

import pytest
from sqlmodel import SQLModel
import main


@pytest.fixture(autouse=True)
def fresh_db():
    """Recreate all tables before each test, with the super-admin seeded."""
    SQLModel.metadata.drop_all(main.engine)
    SQLModel.metadata.create_all(main.engine)
    main.seed_superadmin()
    yield


@pytest.fixture
def super_token():
    from fastapi.testclient import TestClient
    c = TestClient(main.app)
    r = c.post("/auth/login", json={"email": "super@test.com", "password": "superpass123"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]
