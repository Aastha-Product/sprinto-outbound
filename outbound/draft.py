"""Step 6: draft the cold email, check it with guards.py, regenerate once if it fails.

- Hook candidates = verified evidence only (qualification signals + pain points). The LLM must say which
  source URL its hook came from (`hook_evidence_url`), and guards.py checks that URL is really one of them.
- The signature is added by Python from SENDER_NAME / SENDER_TITLE, never written by the LLM.
- If the draft still breaks a rule after one retry, it is saved with `flagged=True` and is never sent.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel

from outbound import llm
from outbound.config import Settings
from outbound.guards import MAX_BODY_WORDS, MAX_SUBJECT_WORDS, validate_draft
from outbound.models import CompanyProfile, Contact, EmailDraft, Evidence, PainPoint, QualificationResult

log = logging.getLogger(__name__)


class DraftOutcome(BaseModel):
    draft: EmailDraft
    full_body: str  # body + signature, exactly what would be sent
    flagged: bool
    problems: list[str]
    attempts: int


def hook_candidates(qualification: QualificationResult, pains: list[PainPoint]) -> list[Evidence]:
    """All verified facts we may build a hook on (de-duplicated)."""
    seen, out = set(), []
    for s in qualification.signals:
        if s.value != "yes" or s.rule_id.startswith(("DQ_", "COMP_")):
            continue
        for e in s.evidence:
            if (e.source_url, e.quote) not in seen:
                seen.add((e.source_url, e.quote))
                out.append(e)
    for p in pains:
        for e in p.evidence:
            if (e.source_url, e.quote) not in seen:
                seen.add((e.source_url, e.quote))
                out.append(e)
    return out


def signature(settings: Settings) -> str:
    return f"\n\n{settings.sender_name}\n{settings.sender_title}"


def draft_email(contact: Contact, profile: CompanyProfile, qualification: QualificationResult, pains: list[PainPoint], settings: Settings) -> DraftOutcome:
    """Step 6. Generates, validates, regenerates once with the checker's feedback if needed."""
    hooks = hook_candidates(qualification, pains)
    evidence_urls = [e.source_url for e in hooks]
    first = contact.first_name or ""
    variables = {
        "sender_name": settings.sender_name,
        "sender_title": settings.sender_title,
        "first_name": first,
        "position": contact.position or "leader",
        "company": profile.name or "your company",
        "hooks": "\n".join(f"- {e.claim} | source: {e.source_url} | quote: \"{e.quote}\"" for e in hooks) or "(none)",
        "pain_points": "\n".join(f"- {p.title}: {p.why_this_person}" for p in pains) or "(none)",
        "max_words": MAX_BODY_WORDS,
        "max_subject_words": MAX_SUBJECT_WORDS,
        "feedback": "",
    }
    draft: EmailDraft | None = None
    problems: list[str] = []
    attempts = 0
    for attempts in (1, 2):
        draft = llm.generate_json("email", variables, EmailDraft, temperature=0.4)
        problems = validate_draft(draft, first, evidence_urls)
        if not problems:
            break
        log.warning("[%s] draft attempt %d failed checks: %s", profile.name, attempts, "; ".join(problems))
        if llm.calls_left() < 1:
            break
        variables["feedback"] = "\n## Your previous draft was rejected by the checker. Fix ALL of these:\n" + "\n".join(f"- {p}" for p in problems)
    assert draft is not None
    return DraftOutcome(draft=draft, full_body=draft.body.strip() + signature(settings), flagged=bool(problems), problems=problems, attempts=attempts)
