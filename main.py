"""
QueueBot — composition root.

Every piece of behaviour lives under app/. This file only builds the FastAPI
application: middleware, lifecycle, background scheduler, and route mounting.

Adding a second vertical (e.g. ordering) means writing its flow and routes as
new modules and mounting them here — nothing in this file grows a special case.
"""
import asyncio

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app import config
from app.auth import seed_superadmin
from app.db import create_db_and_tables
from app.jobs import midnight_reset_job, reconcile_notifications
from app.messaging import outbox_worker
from app.webhook import router as webhook_router
from app.api import (agent_routes, analytics_routes, auth_routes, health_routes,
                     queue_routes, service_routes, tenant_routes)

app = FastAPI(title="QueueBot — Smart Queue Platform")
scheduler = AsyncIOScheduler()

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def on_startup():
    print("🚀 QueueBot starting...")
    create_db_and_tables()
    seed_superadmin()
    if not config.WEBHOOK_SECRETS:
        print("⚠️  WEBHOOK_SECRET not set — /webhook is UNAUTHENTICATED. "
              "Anyone who knows this URL can forge or cancel bookings. Set it now.")
    else:
        print(f"✅ /webhook authenticated ({len(config.WEBHOOK_SECRETS)} secret(s) accepted)")
    scheduler.add_job(midnight_reset_job, "cron", hour=0, minute=1, id="midnight_reset")
    scheduler.add_job(reconcile_notifications, "interval",
                      seconds=config.RECONCILE_SECONDS, id="reconcile_notifications")
    scheduler.start()
    # Catch up on anything missed while the process was down: the 00:01 cron
    # doesn't fire retroactively, so a restart spanning midnight would otherwise
    # leave yesterday's queue open forever.
    await midnight_reset_job()
    app.state.outbox_task = asyncio.create_task(outbox_worker())
    print("✅ DB ready. Scheduler running.")


@app.on_event("shutdown")
async def on_shutdown():
    scheduler.shutdown()
    task = getattr(app.state, "outbox_task", None)
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# Unauthenticated: the webhook carries its own shared-secret check, health is a
# probe, and login is what issues tokens in the first place.
app.include_router(webhook_router)
app.include_router(health_routes.public_router)
app.include_router(auth_routes.public_router)

# Everything below authenticates via the router-level dependency, then scopes
# each read and write to the caller's tenant.
app.include_router(auth_routes.router)
app.include_router(queue_routes.router)
app.include_router(tenant_routes.router)
app.include_router(service_routes.router)
app.include_router(agent_routes.router)
app.include_router(analytics_routes.router)
app.include_router(health_routes.router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=9000)


