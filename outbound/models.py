"""Every data shape the pipeline passes around, defined once with pydantic.

pydantic checks the data for us: if the LLM returns a field with the wrong type,
we get a clear error instead of a silent bug. These classes are also what we hand
to Gemini as the "response schema" so it must answer in exactly this shape.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- evidence
class Evidence(BaseModel):
    """One fact we can defend: what we claim, where we saw it, and the exact words."""

    claim: str
    source_url: str
    quote: str


class FieldEvidence(Evidence):
    """Evidence tied to a specific field of the company profile (e.g. field='funding_stage')."""

    field: str


class Citation(BaseModel):
    """A web page that Gemini's Google Search grounding used to answer a question."""

    url: str
    title: str = ""


# ---------------------------------------------------------------- step 1: scrape
class PageContent(BaseModel):
    """One fetched page: cleaned text (already truncated) or the reason it failed."""

    url: str
    status_code: int | None = None
    title: str | None = None
    text: str = ""
    truncated: bool = False
    error: str | None = None


class DetectedSignal(BaseModel):
    """A cheap, rule-based signal found without the LLM (e.g. a 'trust.vanta.com' link)."""

    signal: str  # e.g. "competitor:vanta", "mentions:SOC 2"
    source_url: str
    snippet: str


class ScrapeResult(BaseModel):
    domain: str
    base_url: str
    fetched_at: datetime = Field(default_factory=utc_now)
    pages: list[PageContent] = []
    skipped_by_robots: list[str] = []
    detected_signals: list[DetectedSignal] = []


# ---------------------------------------------------------------- step 2: extract
class CompanyProfile(BaseModel):
    """Structured facts about the company. Unknown = null and listed in `unknowns`; never guessed."""

    name: str | None = None
    description: str | None = None
    is_b2b_software: bool | None = None
    product_category: str | None = None
    target_customers: str | None = None
    sells_to_enterprise: bool | None = None
    pricing_model: str | None = None
    employee_range: str | None = None
    funding_stage: str | None = None
    hq_location: str | None = None
    markets: list[str] = []
    certifications_mentioned: list[str] = []
    compliance_pages_found: list[str] = []
    competitor_tool_detected: str | None = None
    hiring_roles_seen: list[str] = []
    unknowns: list[str] = []
    evidence: list[FieldEvidence] = []


# ---------------------------------------------------------------- step 3: qualify
class AgentAction(BaseModel):
    """The ONE thing the agent loop decides to do next. Only these three actions exist."""

    action: Literal["fetch_page", "web_search", "decide"]
    argument: str = ""  # a URL for fetch_page, a search query for web_search, empty for decide
    why: str


class Signal(BaseModel):
    """One rubric signal extracted by the LLM. Python (not the LLM) turns signals into points."""

    rule_id: str  # matches a rule id in docs/ICP.md, e.g. "ENT_PLAN"
    value: Literal["yes", "no", "unknown"]
    detail: str = ""  # optional value, e.g. "Series B, March 2025" or "about 250 employees"
    evidence: list[Evidence] = []


class SignalReport(BaseModel):
    """What the qualify prompt returns: one signal per ICP rule. No points; Python scores."""

    signals: list[Signal]
    summary: str = ""


class ScoreLine(BaseModel):
    """One row of the score breakdown table."""

    rule_id: str
    rubric_row: str
    points: int
    max_points: int
    reason: str


class AgentIteration(BaseModel):
    n: int
    action: AgentAction
    learned: str
    new_evidence: list[Evidence] = []
    score_before: int
    score_after: int


class QualificationResult(BaseModel):
    verdict: Literal["qualified", "rejected", "insufficient_data"]
    score: int
    max_possible_score: int | None = None  # score if every unknown rule turned out to be "yes"
    critical_unknowns: list[str] = []
    top_reasons: list[str] = []
    score_breakdown: list[ScoreLine] = []
    signals: list[Signal] = []
    disqualifier: str | None = None
    reasoning: str
    evidence: list[Evidence] = []
    iterations: list[AgentIteration] = []


# ---------------------------------------------------------------- step 4: PoC
class Contact(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    email: str
    position: str | None = None
    seniority: str | None = None
    department: str | None = None
    confidence: int | None = None
    linkedin: str | None = None


class PocResult(BaseModel):
    status: Literal["found", "no_poc", "skipped"] = "found"
    chosen: Contact | None = None
    alternatives: list[Contact] = []
    reasoning: str
    hunter_credits_used: int = 0


# ---------------------------------------------------------------- steps 5-6
class PainPoint(BaseModel):
    title: str
    why_this_person: str
    evidence: list[Evidence] = []


class EmailDraft(BaseModel):
    subject: str
    body: str
    hook: str
    hook_evidence_url: str
    pain_point_used: str
    reasoning: str


# ---------------------------------------------------------------- step 7
class SendResult(BaseModel):
    status: Literal["sent", "dry_run", "blocked", "skipped"]
    to: str  # always OWNER_EMAIL
    would_have_gone_to: str | None = None
    subject: str
    gmail_message_id: str | None = None
    reason: str
    at: datetime = Field(default_factory=utc_now)


# ---------------------------------------------------------------- artifacts
class StepArtifact(BaseModel):
    """What every step saves to runs/<ts>/<domain>/0N_<step>.json: the defense trail."""

    step: str
    domain: str
    model: str | None = None
    timestamp: datetime = Field(default_factory=utc_now)
    input_summary: str
    output: Any
    reasoning: str
    evidence: list[Evidence] = []
