"""Web app (FastAPI): a thin layer over the `outbound` package. No business logic here.

Vercel runs this file automatically. Locally:  uvicorn app:app --reload   then open http://127.0.0.1:8000

Phase 0b: placeholder page + /api/health. The real UI (run steps, view evidence, send) comes in Phase 6.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import FileResponse

from outbound import PROJECT_ROOT
from outbound.config import get_settings
from outbound.storage import ping_database

app = FastAPI(title="Sprinto Outbound", docs_url=None, redoc_url=None)

WEB_DIR = PROJECT_ROOT / "web"


@app.get("/")
def home() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/api/health")
def health() -> dict:
    """Shows whether each service is configured. Never shows a secret or the owner's email."""
    s = get_settings()
    return {
        "status": "ok",
        "database": ping_database(s),
        "gemini": "configured" if not s.missing_for("gemini") else "missing",
        "hunter": "configured" if not s.missing_for("hunter") else "missing",
        "send_enabled": s.send_enabled,
    }
