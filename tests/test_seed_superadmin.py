"""
Super-admin seeding.

seed_superadmin() creates the platform operator's login from
SUPERADMIN_EMAIL / SUPERADMIN_PASSWORD. The behaviour that matters is what it
does when the account is ALREADY there — because that is where a password
change silently does nothing, and the symptom ("Invalid email or password"
for a password the operator just set in the environment) points nowhere near
the cause.
"""
from sqlmodel import Session, select

from app import auth, config, db
from app.models import User


def _user(email):
    with Session(db.engine) as s:
        return s.exec(select(User).where(User.email == email)).first()


def test_seeds_the_account_when_it_does_not_exist(monkeypatch):
    monkeypatch.setattr(config, "SUPERADMIN_EMAIL", "fresh.ops@example.com")
    monkeypatch.setattr(config, "SUPERADMIN_PASSWORD", "brand-new-password")

    auth.seed_superadmin()

    u = _user("fresh.ops@example.com")
    assert u is not None
    assert u.is_super is True
    assert u.is_active is True
    assert u.tenant_id is None
    assert auth.verify_password("brand-new-password", u.password_hash)


def test_an_existing_account_keeps_its_password(monkeypatch):
    """The env var seeds; it does not reset.

    An account that already exists keeps the password it has, so changing
    SUPERADMIN_PASSWORD and restarting does NOT change the password of a
    deployed operator. The old password keeps working and the new one is
    ignored — silently, which is what makes this so confusing in practice.
    """
    monkeypatch.setattr(config, "SUPERADMIN_EMAIL", "existing.ops@example.com")
    monkeypatch.setattr(config, "SUPERADMIN_PASSWORD", "the-original-password")

    auth.seed_superadmin()
    u = _user("existing.ops@example.com")
    assert u is not None
    assert auth.verify_password("the-original-password", u.password_hash)

    # Now the operator "changes" the password in the environment and the app
    # restarts, as a redeploy would.
    monkeypatch.setattr(config, "SUPERADMIN_PASSWORD", "the-new-password")
    auth.seed_superadmin()

    u = _user("existing.ops@example.com")
    assert auth.verify_password("the-original-password", u.password_hash), \
        "the original password must keep working"
    assert not auth.verify_password("the-new-password", u.password_hash), \
        "SUPERADMIN_PASSWORD must not silently reset an existing account"


def test_it_says_so_when_the_password_was_not_applied(monkeypatch, capsys):
    """The operator has to be told, or they go looking in the env vars.

    The message names the account and says where to reset it, because the
    env var is the only place someone would think to look for the reason a
    login is being rejected.
    """
    monkeypatch.setattr(config, "SUPERADMIN_EMAIL", "existing.ops@example.com")
    monkeypatch.setattr(config, "SUPERADMIN_PASSWORD", "the-original-password")
    auth.seed_superadmin()
    capsys.readouterr()

    monkeypatch.setattr(config, "SUPERADMIN_PASSWORD", "the-new-password")
    auth.seed_superadmin()
    out = capsys.readouterr().out

    assert "already exists" in out
    assert "password unchanged" in out
    assert "PATCH /admin/users/" in out


def test_no_env_pair_means_no_account(monkeypatch, capsys):
    """No pair, no login — and the operator is told why."""
    monkeypatch.setattr(config, "SUPERADMIN_EMAIL", "")
    monkeypatch.setattr(config, "SUPERADMIN_PASSWORD", "")

    auth.seed_superadmin()

    out = capsys.readouterr().out
    assert "no super-admin seeded" in out


def test_the_email_is_lowercased(monkeypatch):
    """Login lowercases the email, so the seed must match it exactly."""
    monkeypatch.setattr(config, "SUPERADMIN_EMAIL", "Mixed.Case@Example.com")
    monkeypatch.setattr(config, "SUPERADMIN_PASSWORD", "some-password")

    auth.seed_superadmin()

    assert _user("mixed.case@example.com") is not None
    assert _user("Mixed.Case@Example.com") is None
