"""FastAPI entry point: uvicorn app.main:app --reload

Admin pages are served at /admin; the JSON they call is under /api/admin.
"""

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from app.routers import admin_pages, admin_skill_badges

app = FastAPI(title="MongoDB Skill Badge Questions Generator")
app.include_router(admin_pages.router)
app.include_router(admin_skill_badges.router)


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/admin/skill-badges", status_code=307)


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}
