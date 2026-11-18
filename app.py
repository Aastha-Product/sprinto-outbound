"""Web app (FastAPI): a thin layer over the `outbound` package. No business logic here.

Vercel runs this file automatically. Locally:  uvicorn app:app --reload   then open http://127.0.0.1:8000

Why one step per request: Vercel stops a request after a few minutes, and a full run takes longer.
The page (web/index.html) calls /step for steps 1-6 in order; every result is saved in Neon before the
next call, so each request can run in a fresh serverless instance. Step 7 (send) is a separate button.

Public-site guardrails (the owner chose "open to anyone"):
- MAX_RUNS_PER_DAY domains per day across all visitors, a per-IP request limit, max 10 domains per run
- the Send button follows every rule in outbound/send.py, plus MAX_EMAILS_PER_DAY
- all email addresses are masked in API responses (j***@company.com)
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

from outbound import PROJECT_ROOT
from outbound.config import get_settings
from outbound.domains import parse_domains
from outbound.llm import LLMFatalError
from outbound.pipeline import STEP_TITLES, DomainRunner, RunOptions, new_run_id
from outbound.report import summary_rows, write_reports
from outbound.storage import get_store, ping_database

app = FastAPI(title="Sprinto Outbound", docs_url=None, redoc_url=None)

WEB_DIR = PROJECT_ROOT / "web"
MAX_DOMAINS_PER_RUN = 10
MAX_REQUESTS_PER_IP_PER_HOUR = 150
EMAIL_RE = re.compile(r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")


# ---------------------------------------------------------------- helpers
def mask(value: Any) -> Any:
    """Recursively replace every email address with a masked version."""
    if isinstance(value, str):
        return EMAIL_RE.sub(r"\1***@\2", value)
    if isinstance(value, list):
        return [mask(v) for v in value]
    if isinstance(value, dict):
        return {k: mask(v) for k, v in value.items()}
    return value


def public_artifact(art: dict | None) -> dict | None:
    """What the browser may see: masked, without the bulky evidence corpus."""
    if art is None:
        return None
    return mask({k: v for k, v in art.items() if k != "corpus"})


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else "unknown")


def rate_limit(request: Request) -> None:
    store = get_store()
    ip = client_ip(request)
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    if store.count_events("web_request", since=since, ip=ip) >= MAX_REQUESTS_PER_IP_PER_HOUR:
        raise HTTPException(429, "Too many requests from your network this hour. Please try again later.")
    store.log_event("web_request", ip=ip)


def run_domains(run_id: str) -> list[str]:
    store = get_store()
    text = store.load_run_file(run_id, "domains.txt")
    if text is not None:
        return text.split()
    run = store.load_run(run_id)  # runs started from the CLI
    if not run:
        raise HTTPException(404, "Unknown run id")
    return list(run)


# ---------------------------------------------------------------- pages
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
        "gmail": "configured" if (s.gmail_refresh_token.get_secret_value() or (PROJECT_ROOT / s.gmail_token_file).exists()) else "missing",
        "send_enabled": s.send_enabled,
        "limits": {"domains_per_run": MAX_DOMAINS_PER_RUN, "runs_per_day": s.max_runs_per_day, "emails_per_day": s.max_emails_per_day},
    }


@app.get("/api/sample-domains", response_class=PlainTextResponse)
def sample_domains() -> str:
    return (PROJECT_ROOT / "data" / "domains.txt").read_text(encoding="utf-8")


# ---------------------------------------------------------------- runs
class NewRun(BaseModel):
    domains: str


class StepRequest(BaseModel):
    domain: str
    step: int


class SendRequest(BaseModel):
    domain: str


@app.post("/api/runs")
def create_run(body: NewRun, request: Request) -> dict:
    rate_limit(request)
    s = get_settings()
    if s.missing_for("gemini"):
        raise HTTPException(503, "The server has no Gemini key configured yet, so runs are disabled.")
    domains = parse_domains(body.domains)
    if not domains:
        raise HTTPException(400, "No valid domains found. Put one domain per line, e.g. rocketlane.com")
    if len(domains) > MAX_DOMAINS_PER_RUN:
        raise HTTPException(400, f"At most {MAX_DOMAINS_PER_RUN} domains per run.")
    store = get_store()
    used = store.count_events("web_domain")
    if used + len(domains) > s.max_runs_per_day:
        raise HTTPException(429, f"Daily limit reached ({used}/{s.max_runs_per_day} domains today). Try again tomorrow, or view past runs.")
    run_id = new_run_id()
    store.save_run_file(run_id, "domains.txt", "\n".join(domains))
    for d in domains:
        store.log_event("web_domain", run_id=run_id, domain=d, ip=client_ip(request))
    return {"run_id": run_id, "domains": domains, "steps": STEP_TITLES}


@app.post("/api/runs/{run_id}/step")
def run_step(run_id: str, body: StepRequest, request: Request) -> dict:
    """Run ONE of steps 1-6 (never sends). The browser calls this repeatedly."""
    rate_limit(request)
    if body.step not in range(1, 7):
        raise HTTPException(400, "Steps 1-6 only. Sending has its own button.")
    if body.domain not in run_domains(run_id):
        raise HTTPException(400, "That domain is not part of this run.")
    try:
        outcome = DomainRunner(run_id, body.domain, get_settings(), get_store(), RunOptions()).run_step(body.step)
    except LLMFatalError as exc:
        raise HTTPException(503, f"The AI service is unavailable for this site right now ({exc}).") from None
    return {"step": outcome.step, "status": outcome.status, "message": mask(outcome.message), "artifact": public_artifact(outcome.artifact)}


@app.post("/api/runs/{run_id}/send")
def send(run_id: str, body: SendRequest, request: Request) -> dict:
    """Step 7 with sending requested. Every guardrail in outbound/send.py still applies."""
    rate_limit(request)
    if body.domain not in run_domains(run_id):
        raise HTTPException(400, "That domain is not part of this run.")
    outcome = DomainRunner(run_id, body.domain, get_settings(), get_store(), RunOptions(send=True)).run_step(7)
    return {"step": 7, "status": outcome.status, "message": mask(outcome.message), "artifact": public_artifact(outcome.artifact)}


@app.get("/api/runs")
def list_runs() -> list[dict]:
    store = get_store()
    out = []
    for run_id in store.list_runs(limit=15):
        rows = summary_rows(store.load_run(run_id))
        out.append({"run_id": run_id, "domains": len(rows), "qualified": sum(r["verdict"] == "qualified" for r in rows)})
    return out


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict:
    store = get_store()
    domains = run_domains(run_id)
    run = store.load_run(run_id)
    return {
        "run_id": run_id,
        "domains": domains,
        "artifacts": {d: {step: public_artifact(a) for step, a in run.get(d, {}).items()} for d in domains},
        "summary": mask(summary_rows(run)),
    }


@app.get("/api/runs/{run_id}/report", response_class=PlainTextResponse)
def report(run_id: str) -> str:
    run_domains(run_id)
    _csv, md = write_reports(run_id, get_store())
    return mask(md)
