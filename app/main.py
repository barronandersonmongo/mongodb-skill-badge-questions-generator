"""FastAPI entry point: uvicorn app.main:app --reload

The questions screen — the work this tool exists for — is the site root, with its
JSON under /api/questions. The functions that curate the badge catalog behind it
are under /admin, with their JSON under /api/admin. Nothing enforces that
boundary: there are no authorizations, and both are reachable by anyone who can
reach the service. It separates the two kinds of work.
"""

import logging

from fastapi import FastAPI

from app.logging_config import configure_logging
from app.routers import admin_logs, admin_pages, admin_skill_badges, pages, questions

# Before the routers are touched, so anything they log at import time is captured.
configure_logging()
logging.getLogger(__name__).info("Application starting")

app = FastAPI(title="MongoDB Skill Badge Questions Generator")
app.include_router(pages.router)
app.include_router(questions.router)
app.include_router(admin_pages.router)
app.include_router(admin_skill_badges.router)
app.include_router(admin_logs.router)


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}
