"""Resolving the business a webhook call belongs to."""
from typing import Optional
from sqlmodel import Session, select

from app.core import normalize_number
from app.db import engine
from app.models import Tenant

# =============================================================================
# 4. TENANT HELPERS
# =============================================================================

def get_tenant_by_number(raw: str) -> Optional[Tenant]:
    clean = normalize_number(raw.replace("@s.whatsapp.net", "").replace("@lid", ""))
    with Session(engine) as s:
        return s.exec(
            select(Tenant).where(Tenant.whatsapp_number == clean, Tenant.is_active == True)
        ).first()

