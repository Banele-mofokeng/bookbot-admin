"""Login, whoami, and super-admin user management."""
from datetime import datetime, timedelta, time, date
from typing import Optional, Dict, Any, List
from fastapi import APIRouter, HTTPException, Depends
from sqlmodel import SQLModel, Session, select

from app import config, core
from app.auth import get_current_user, require_super, ensure_tenant_access
from app.db import engine
from app.models import (Tenant, User, Service, Agent, AgentService,
                        AgentSchedule, AgentBlock, QueueEntry, OutboxMessage)

# Same guard the single admin router used to carry: every route below
# authenticates, then scopes to the caller's tenant.
router = APIRouter(dependencies=[Depends(get_current_user)])

from app.auth import (create_access_token, hash_password, verify_password)

# Login and whoami are not behind the router guard — one issues the token,
# the other authenticates explicitly.
public_router = APIRouter()

class LoginBody(SQLModel):
    email: str
    password: str


def _user_public(user: User, tenant: Optional[Tenant]) -> dict:
    return {
        "id":        user.id,
        "email":     user.email,
        "is_super":  user.is_super,
        "tenant_id": user.tenant_id,
        "tenant":    tenant.dict() if tenant else None,
    }


@public_router.post("/auth/login")
def login(body: LoginBody):
    email = (body.email or "").strip().lower()
    with Session(engine) as s:
        user = s.exec(select(User).where(User.email == email)).first()
        if not user or not user.is_active or not verify_password(body.password, user.password_hash):
            raise HTTPException(status_code=401, detail="Invalid email or password")
        tenant = s.get(Tenant, user.tenant_id) if user.tenant_id else None
        token = create_access_token(user)
        return {"access_token": token, "token_type": "bearer", "user": _user_public(user, tenant)}


@public_router.get("/auth/me")
def whoami(user: User = Depends(get_current_user)):
    with Session(engine) as s:
        tenant = s.get(Tenant, user.tenant_id) if user.tenant_id else None
    return _user_public(user, tenant)


class UserCreate(SQLModel):
    email:     str
    password:  str
    tenant_id: Optional[int] = None
    is_super:  bool = False


@router.get("/admin/users")
def list_users(user: User = Depends(require_super)):
    with Session(engine) as s:
        rows = s.exec(select(User)).all()
        return [{"id": u.id, "email": u.email, "is_super": u.is_super,
                 "tenant_id": u.tenant_id, "is_active": u.is_active} for u in rows]


@router.post("/admin/users")
def create_user(data: UserCreate, _: User = Depends(require_super)):
    """Provision a login. Super-admin only (no public signup)."""
    email = data.email.strip().lower()
    if not data.password or len(data.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    with Session(engine) as s:
        if s.exec(select(User).where(User.email == email)).first():
            raise HTTPException(status_code=409, detail="Email already registered")
        if not data.is_super:
            if not data.tenant_id or not s.get(Tenant, data.tenant_id):
                raise HTTPException(status_code=400, detail="Valid tenant_id required for a tenant user")
        u = User(email=email, password_hash=hash_password(data.password),
                 tenant_id=None if data.is_super else data.tenant_id,
                 is_super=data.is_super, is_active=True)
        s.add(u); s.commit(); s.refresh(u)
        return {"id": u.id, "email": u.email, "is_super": u.is_super, "tenant_id": u.tenant_id}


@router.patch("/admin/users/{user_id}")
def update_user(user_id: int, updates: Dict[str, Any], _: User = Depends(require_super)):
    """Reset password / deactivate. Super-admin only."""
    with Session(engine) as s:
        u = s.get(User, user_id)
        if not u:
            raise HTTPException(status_code=404, detail="User not found")
        if "password" in updates:
            pw = updates["password"]
            if not pw or len(pw) < 8:
                raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
            u.password_hash = hash_password(pw)
        if "is_active" in updates:
            u.is_active = bool(updates["is_active"])
        s.add(u); s.commit()
        return {"id": u.id, "email": u.email, "is_active": u.is_active}

