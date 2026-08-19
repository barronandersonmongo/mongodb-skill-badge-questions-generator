"""FastAPI entry point: uvicorn app.main:app --reload

The questions screen — the work this tool exists for — is the site root, with its
JSON under /api/questions. The functions that curate the badge catalog behind it
are under /admin, with their JSON under /api/admin. Nothing enforces that
boundary: there are no authorizations, and both are reachable by anyone who can
reach the service. It separates the two kinds of work.
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.logging_config import configure_logging, log_file_path
from app.routers import (
    admin_docs,
    admin_logs,
    admin_pages,
    admin_skill_badges,
    pages,
    questions,
)

# Attached before the routers are touched, so anything they log while being
# imported is captured. Only the handler is set up here — importing this module is
# not the same as running the service, so nothing is logged about starting yet.
configure_logging()

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Announce a real start.

    Logging this at import instead would record a start every time anything merely
    imported the application — including each run of the test suite, which would
    write into the operator's log.
    """
    logger.info("Application starting; logging to %s", log_file_path())
    yield
    logger.info("Application shutting down")


app = FastAPI(title="MongoDB Skill Badge Questions Generator", lifespan=lifespan)

# The theme is one stylesheet served from disk rather than inline in the base
# template, so a browser caches it across screens and the look is defined in one
# place. Everything else still comes from a CDN; there is no build step.
app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).parent / "static")),
    name="static",
)
app.include_router(pages.router)
app.include_router(questions.router)
app.include_router(admin_pages.router)
app.include_router(admin_skill_badges.router)
app.include_router(admin_logs.router)
app.include_router(admin_docs.router)


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}
