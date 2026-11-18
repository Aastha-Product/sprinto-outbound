"""Step 5: the 2 most likely pain points for the chosen person, each tied to verified evidence.

The LLM proposes pain points (prompts/pain_points.md, using the persona table from docs/ICP.md).
Python keeps only evidence that verifies against our sources. A pain point with no verified evidence is
dropped; if fewer than 2 survive we ask once more, then keep what we have.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel

from outbound import llm
from outbound.evidence import EvidenceCorpus
from outbound.icp import load_icp
from outbound.models import CompanyProfile, Contact, PainPoint, QualificationResult

log = logging.getLogger(__name__)


class PainPointList(BaseModel):
    pain_points: list[PainPoint]


def describe_person(contact: Contact, company: str) -> str:
    name = " ".join(p for p in (contact.first_name, contact.last_name) if p)
    return f"{name}, {contact.position or 'unknown title'} at {company} (seniority: {contact.seniority or 'unknown'}, department: {contact.department or 'unknown'})"


def yes_signals_text(q: QualificationResult) -> str:
    rows = []
    for s in q.signals:
        if s.value == "yes" and s.evidence:
            e = s.evidence[0]
            rows.append(f"- {s.rule_id}: {s.detail or e.claim} | {e.source_url} | \"{e.quote}\"")
    return "\n".join(rows) or "(none)"


def find_pain_points(contact: Contact, profile: CompanyProfile, qualification: QualificationResult, corpus: EvidenceCorpus) -> tuple[list[PainPoint], list[str]]:
    """Step 5. Returns (pain_points, notes)."""
    company = profile.name or "the company"
    variables = {
        "person": describe_person(contact, company),
        "profile": profile.model_dump_json(indent=1, exclude={"evidence"}),
        "signals": yes_signals_text(qualification),
        "personas": load_icp().personas_text,
        "content": corpus.as_prompt_block(4000),
    }
    notes: list[str] = []
    kept: list[PainPoint] = []
    for attempt in (1, 2):
        result = llm.generate_json("pain_points", variables, PainPointList, temperature=0.2)
        kept = []
        for p in result.pain_points[:2]:
            good = corpus.filter(p.evidence)
            if good:
                kept.append(p.model_copy(update={"evidence": good}))
            else:
                notes.append(f"attempt {attempt}: dropped '{p.title}' (no verified evidence)")
        if len(kept) == 2 or llm.calls_left() <= 2:
            break
    if len(kept) < 2:
        notes.append(f"only {len(kept)} pain point(s) had verified evidence")
    log.info("[%s] pain: %d pain point(s) with evidence", company, len(kept))
    return kept, notes
