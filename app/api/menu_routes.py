"""Menu CRUD for ordering tenants."""
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, SQLModel, select

from app import orders
from app.auth import ensure_tenant_access, get_current_user
from app.db import engine
from app.models import MenuItem, User

router = APIRouter(dependencies=[Depends(get_current_user)])


@router.get("/admin/menu/{tenant_id}")
def list_menu(tenant_id: int, user: User = Depends(get_current_user)):
    """Every item including inactive ones — staff need to see what's sold out
    in order to switch it back on."""
    ensure_tenant_access(user, tenant_id)
    with Session(engine) as s:
        items = s.exec(
            select(MenuItem).where(MenuItem.tenant_id == tenant_id)
        ).all()
    # Same order the customer sees, so the dashboard is a true preview.
    return orders.sort_menu(items)


class MenuItemCreate(SQLModel):
    tenant_id:    int
    name:         str
    category:     str  = ""
    price_cents:  int  = 0
    prep_minutes: int  = 10
    is_active:    bool = True
    sort_order:   int  = 0


@router.post("/admin/menu")
def create_menu_item(data: MenuItemCreate, user: User = Depends(get_current_user)):
    ensure_tenant_access(user, data.tenant_id)
    if data.price_cents < 0 or data.prep_minutes < 0:
        raise HTTPException(status_code=400,
                            detail="Price and prep time cannot be negative")
    item = MenuItem(**data.dict())
    with Session(engine) as s:
        s.add(item)
        s.commit()
        s.refresh(item)
    return item


@router.patch("/admin/menu/{item_id}")
def update_menu_item(item_id: int, updates: Dict[str, Any],
                     user: User = Depends(get_current_user)):
    with Session(engine) as s:
        item = s.get(MenuItem, item_id)
        if not item:
            raise HTTPException(status_code=404, detail="Menu item not found")
        ensure_tenant_access(user, item.tenant_id)
        # tenant_id is never re-assignable — moving an item between businesses
        # would silently hand one tenant's pricing to another.
        for k, v in updates.items():
            if k != "tenant_id" and hasattr(item, k):
                setattr(item, k, v)
        if item.price_cents < 0 or item.prep_minutes < 0:
            raise HTTPException(status_code=400,
                                detail="Price and prep time cannot be negative")
        s.add(item)
        s.commit()
        s.refresh(item)
    return item


@router.delete("/admin/menu/{item_id}")
def delete_menu_item(item_id: int, user: User = Depends(get_current_user)):
    """
    Remove an item from the menu for good.

    Past orders are unaffected: their lines carry their own name and price, so
    nothing in the history or the takings changes.
    """
    with Session(engine) as s:
        item = s.get(MenuItem, item_id)
        if not item:
            raise HTTPException(status_code=404, detail="Menu item not found")
        ensure_tenant_access(user, item.tenant_id)
        s.delete(item)
        s.commit()
    return {"status": "deleted"}
