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
os.environ.setdefault("ADMIN_TOKEN", "test-token")

import pytest
from sqlmodel import SQLModel
import main


@pytest.fixture(autouse=True)
def fresh_db():
    """Recreate all tables before each test for isolation."""
    SQLModel.metadata.drop_all(main.engine)
    SQLModel.metadata.create_all(main.engine)
    yield
