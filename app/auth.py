"""Password hashing, JWT issue/verify, and the request dependencies that
scope every admin route to the caller's tenant.
"""
import hashlib
import hmac
import secrets
import jwt
from datetime import datetime, timedelta
from typing import Optional
from fastapi import HTTPException, Header, Depends, Request
from sqlmodel import Session, select

from app import config, core
from app.db import engine
from app.models import Tenant, User

# =============================================================================
# 2b. AUTH — PASSWORDS, JWT, DEPENDENCIES
# =============================================================================

def hash_password(password: str) -> str:
    """PBKDF2-SHA256 (stdlib, no native deps). Format: 'iterations$salt_hex$hash_hex'."""
    iterations = 200_000
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"{iterations}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        iter_s, salt_hex, hash_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iter_s))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


def create_access_token(user: "User") -> str:
    payload = {
        "sub": str(user.id),
        "email": user.email,
        "is_super": user.is_super,
        "tenant_id": user.tenant_id,
        "exp": datetime.utcnow() + timedelta(hours=config.JWT_EXP_HOURS),
    }
    return jwt.encode(payload, config.JWT_SECRET, algorithm=config.JWT_ALG)


def get_current_user(authorization: str = Header(default="")) -> "User":
    """Validate the Bearer JWT and return the live User row."""
    if not config.JWT_SECRET:
        raise HTTPException(status_code=503, detail="JWT_SECRET not configured on server")
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    try:
        payload = jwt.decode(token, config.JWT_SECRET, algorithms=[config.JWT_ALG])
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    with Session(engine) as s:
        user = s.get(User, int(payload.get("sub", 0)))
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    return user


def require_super(user: "User" = Depends(get_current_user)) -> "User":
    if not user.is_super:
        raise HTTPException(status_code=403, detail="Super-admin only")
    return user


def ensure_tenant_access(user: "User", tenant_id: int):
    """Super-admins reach any tenant; tenant users only their own."""
    if user.is_super:
        return
    if user.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="Forbidden — not your business")


def verify_webhook_secret(
    request: Request,
    x_webhook_token: str = Header(default=""),
    authorization: str = Header(default=""),
):
    """
    Authenticate an inbound Evolution webhook call against config.WEBHOOK_SECRETS.

    The secret may arrive three ways, because Evolution deployments differ in
    what they can be configured to send:
      - 'X-Webhook-Token: <secret>'
      - 'Authorization: Bearer <secret>'
      - '?token=<secret>' on the webhook URL (always available — the URL itself
        is editable in the Evolution instance settings)

    With WEBHOOK_SECRET unset the check is skipped, so an already-deployed bot
    keeps taking bookings across the upgrade instead of going silent. Startup
    logs a warning in that case and /health reports it as off.
    """
    if not config.WEBHOOK_SECRETS:
        return
    if x_webhook_token.strip():
        presented = x_webhook_token.strip()
    elif authorization.lower().startswith("bearer "):
        presented = authorization.split(" ", 1)[1].strip()
    else:
        presented = (request.query_params.get("token") or "").strip()
    # Compare against every configured secret so rotation works, in constant
    # time so a wrong guess can't be tuned byte by byte.
    matched = False
    for secret in config.WEBHOOK_SECRETS:
        if hmac.compare_digest(presented.encode(), secret.encode()):
            matched = True
    if not presented or not matched:
        raise HTTPException(status_code=401, detail="Invalid webhook credentials")


def seed_superadmin():
    """Create the platform operator login from env if it doesn't exist yet."""
    if not (config.SUPERADMIN_EMAIL and config.SUPERADMIN_PASSWORD):
        print("⚠️  SUPERADMIN_EMAIL/PASSWORD not set — no super-admin seeded.")
        return
    email = config.SUPERADMIN_EMAIL.strip().lower()
    with Session(engine) as s:
        existing = s.exec(select(User).where(User.email == email)).first()
        if existing:
            return
        s.add(User(email=email, password_hash=hash_password(config.SUPERADMIN_PASSWORD),
                   tenant_id=None, is_super=True, is_active=True))
        s.commit()
    print(f"✅ Seeded super-admin {email}")

