"""Step 2: turn the scraped pages into a structured `CompanyProfile`, where every fact has evidence.

Flow:
1. Build an `EvidenceCorpus` from the scraped pages (+ cheap signals).
2. Ask Gemini (prompts/extract.md) to fill the `CompanyProfile`, quoting the pages.
3. Python checks every evidence item against the real page text (`EvidenceCorpus.verify`).
4. Any filled field WITHOUT verified evidence is reset to null/[] and listed in `unknowns`.
   So the profile can only contain facts we can show the source for.
"""

from __future__ import annotations

import logging

from outbound import llm
from outbound.evidence import EvidenceCorpus
from outbound.models import CompanyProfile, ScrapeResult

log = logging.getLogger(__name__)

# Fields that hold facts (everything except the bookkeeping fields).
FACT_FIELDS = [f for f in CompanyProfile.model_fields if f not in ("unknowns", "evidence")]


def build_corpus(scrape: ScrapeResult) -> EvidenceCorpus:
    corpus = EvidenceCorpus()
    for page in scrape.pages:
        corpus.add_page(page, scrape.detected_signals)
    return corpus


def format_signals(scrape: ScrapeResult) -> str:
    if not scrape.detected_signals:
        return "(none found)"
    return "\n".join(f"- {s.signal} | {s.source_url} | {s.snippet}" for s in scrape.detected_signals)


def enforce_evidence(profile: CompanyProfile, corpus: EvidenceCorpus) -> tuple[CompanyProfile, list[str]]:
    """Drop unverifiable evidence, then null every field that no longer has evidence.

    Returns (clean_profile, notes) where notes explain what was removed (saved as reasoning).
    """
    notes: list[str] = []
    verified = []
    for ev in profile.evidence:
        if corpus.verify(ev):
            verified.append(ev)
        else:
            notes.append(f"dropped evidence for '{ev.field}' (quote not found on {ev.source_url})")
    has_evidence = {ev.field for ev in verified}

    data = profile.model_dump()
    unknowns = set(profile.unknowns)
    for field in FACT_FIELDS:
        value = data[field]
        filled = value not in (None, "", [])
        if filled and field not in has_evidence:
            data[field] = [] if isinstance(value, list) else None
            notes.append(f"cleared '{field}' (no verified evidence)")
            filled = False
        if not filled:
            unknowns.add(field)
        else:
            unknowns.discard(field)
    data["unknowns"] = sorted(unknowns)
    data["evidence"] = [ev.model_dump() for ev in verified if ev.field in FACT_FIELDS]
    return CompanyProfile.model_validate(data), notes


def extract_profile(scrape: ScrapeResult) -> tuple[CompanyProfile, list[str]]:
    """Step 2 for one domain. Returns (profile, notes)."""
    corpus = build_corpus(scrape)
    if not corpus.sources:
        log.warning("[%s] extract: no pages could be read; profile is all unknown", scrape.domain)
        return CompanyProfile(unknowns=list(FACT_FIELDS)), ["no readable pages, so nothing could be extracted"]
    raw = llm.generate_json(
        "extract",
        {"domain": scrape.domain, "content": corpus.as_prompt_block(), "signals": format_signals(scrape)},
        CompanyProfile,
    )
    profile, notes = enforce_evidence(raw, corpus)
    log.info("[%s] extract: %d fields filled, %d unknown, %d evidence dropped", scrape.domain,
             len(FACT_FIELDS) - len(profile.unknowns), len(profile.unknowns), sum("dropped" in n for n in notes))
    return profile, notes
