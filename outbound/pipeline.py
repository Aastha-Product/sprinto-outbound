"""Runs the 7 steps for a domain and saves every step's artifact (the defense trail).

Used by BOTH the CLI and the web app:
- CLI: `run_domain()` runs a range of steps in one go.
- Web (Vercel): calls `DomainRunner.run_step(n)` once per HTTP request, because requests time out.
Each step loads what it needs from the previous steps' saved artifacts, so a step can run in a
fresh process (or a fresh serverless function) and still continue the same run.

Artifact (one per step) = step, domain, status, model, timestamp, input_summary, output, reasoning,
evidence, llm_calls_used (running total for this domain, so the per-domain LLM limit holds across requests).
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from outbound import llm
from outbound.config import Settings
from outbound.draft import draft_email
from outbound.evidence import EvidenceCorpus
from outbound.extract import build_corpus, extract_profile
from outbound.models import (
    CompanyProfile,
    Contact,
    Evidence,
    PainPoint,
    PocResult,
    QualificationResult,
    ScrapeResult,
    StepArtifact,
)
from outbound.pain import find_pain_points
from outbound.poc import find_poc
from outbound.qualify import QualifyContext, qualify
from outbound.scrape import Fetcher, scrape_domain
from outbound.send import send_email
from outbound.storage import Store

log = logging.getLogger(__name__)

STEP_FILES = {1: "01_scrape", 2: "02_extract", 3: "03_qualify", 4: "04_poc", 5: "05_pain", 6: "06_email", 7: "07_send"}
STEP_TITLES = {1: "Scrape", 2: "Extract", 3: "Qualify", 4: "Find PoC", 5: "Pain points", 6: "Draft email", 7: "Send"}


@dataclass
class RunOptions:
    send: bool = False  # Step 7 actually sends only with this AND SEND_ENABLED=true
    no_hunter: bool = False  # never spend a Hunter credit (cache only)
    use_cache: bool = True  # reuse cached scrapes
    skip_done: bool = False  # when continuing a run: reuse steps that already finished ('done')
    visitor: str = ""  # step 7 (website): a code-verified visitor inbox; "" = OWNER_EMAIL


@dataclass
class StepOutcome:
    step: int
    status: str  # "done" | "stopped" (pipeline ends here for this domain, by design) | "error"
    message: str
    artifact: dict | None = None


def new_run_id() -> str:
    """e.g. 20261119-143005-a1b2 (time + random suffix so two website visitors never collide)."""
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


class StepStop(Exception):
    """A step can't run because an earlier step decided this domain stops here."""


class DomainRunner:
    """Runs steps for one (run, domain) pair, loading earlier artifacts from the store."""

    def __init__(self, run_id: str, domain: str, settings: Settings, store: Store, options: RunOptions | None = None):
        self.run_id, self.domain, self.settings, self.store = run_id, domain, settings, store
        self.options = options or RunOptions()

    # ---------- artifact helpers
    def artifact(self, n: int) -> dict | None:
        return self.store.load_artifact(self.run_id, self.domain, STEP_FILES[n])

    def output(self, n: int) -> Any:
        art = self.artifact(n)
        if art is None:
            raise StepStop(f"step {n} ({STEP_TITLES[n]}) has not run yet for {self.domain} in run {self.run_id}")
        if art.get("status") != "done":
            raise StepStop(f"step {n} ({STEP_TITLES[n]}) ended with '{art.get('status')}': {art.get('reasoning', '')[:200]}")
        return art["output"]

    def _calls_before(self, n: int) -> int:
        for prev in range(n - 1, 0, -1):
            art = self.artifact(prev)
            if art is not None:
                return int(art.get("llm_calls_used", 0))
        return 0

    def _save(self, n: int, status: str, input_summary: str, output: Any, reasoning: str, evidence: list[Evidence] | None = None, extra: dict | None = None) -> dict:
        art = StepArtifact(
            step=STEP_FILES[n], domain=self.domain, model=llm.model_name() or None, input_summary=input_summary,
            output=output, reasoning=reasoning, evidence=evidence or [],
        ).model_dump(mode="json")
        art.update({"status": status, "llm_calls_used": llm.calls_used(), "run_id": self.run_id})
        art.update(extra or {})
        self.store.save_artifact(self.run_id, self.domain, STEP_FILES[n], art)
        return art

    # ---------- the steps
    def run_step(self, n: int) -> StepOutcome:
        """Run ONE step and save its artifact. Never raises: errors are saved as an 'error' artifact."""
        llm.start_domain(self.domain, already_used=self._calls_before(n))
        llm.use_store(self.store)  # identical prompts are answered from the cache, for free
        handler: Callable[[], StepOutcome] = getattr(self, f"_step{n}")
        try:
            return handler()
        except StepStop as stop:
            return StepOutcome(n, "stopped", str(stop))
        except llm.LLMFatalError as exc:  # no point trying other domains: save, then stop the whole run
            self._save(n, "error", f"step {n} for {self.domain}", None, f"Step failed: {exc}")
            raise
        except Exception as exc:  # keep going with other domains; record what happened
            if log.isEnabledFor(logging.DEBUG):
                log.exception("[%s] step %d failed", self.domain, n)
            else:
                log.error("[%s] step %d failed: %s: %s (run with -v for details)", self.domain, n, type(exc).__name__, str(exc)[:200])
            message = f"{type(exc).__name__}: {str(exc)[:300]}"
            art = self._save(n, "error", f"step {n} for {self.domain}", None, f"Step failed: {message}")
            return StepOutcome(n, "error", message, art)

    def _step1(self) -> StepOutcome:
        res = scrape_domain(self.domain, self.settings, self.store, use_cache=self.options.use_cache)
        ok = [p for p in res.pages if not p.error]
        failed = [f"{p.url} ({p.error})" for p in res.pages if p.error]
        silent = [f"{p.url} ({p.quality_note or p.quality})" for p in ok if not p.readable]
        reasoning = (f"Fetched {len(ok)} page(s) from {self.domain}: {', '.join(p.url for p in ok) or 'none'}. "
                     f"{'Failed: ' + '; '.join(failed) + '. ' if failed else ''}"
                     f"{'Fetched but NOT readable (treated as unknown, not as empty): ' + '; '.join(silent) + '. ' if silent else ''}"
                     f"Found {len(res.detected_signals)} rule-based signal(s): {', '.join(sorted({s.signal for s in res.detected_signals})) or 'none'}.")
        status = "done" if ok else "stopped"
        art = self._save(1, status, f"domain {self.domain}", res.model_dump(mode="json"), reasoning if ok else "No page could be fetched. " + reasoning)
        return StepOutcome(1, status, reasoning, art)

    def _step2(self) -> StepOutcome:
        scrape = ScrapeResult.model_validate(self.output(1))
        profile, notes = extract_profile(scrape)
        filled = [f for f in CompanyProfile.model_fields if f not in ("unknowns", "evidence") and f not in profile.unknowns]
        reasoning = (f"Filled {len(filled)} field(s) with verified evidence ({', '.join(filled) or 'none'}). "
                     f"Unknown: {', '.join(profile.unknowns) or 'none'}. " + (" ".join(notes[:8]) if notes else ""))
        art = self._save(2, "done", f"{len(scrape.pages)} scraped page(s)", profile.model_dump(mode="json"), reasoning.strip(), list(profile.evidence))
        return StepOutcome(2, "done", reasoning, art)

    def _step3(self) -> StepOutcome:
        scrape = ScrapeResult.model_validate(self.output(1))
        profile = CompanyProfile.model_validate(self.output(2))
        fetcher = Fetcher(self.settings)
        try:
            ctx = QualifyContext(domain=self.domain, scrape=scrape, profile=profile, corpus=build_corpus(scrape), settings=self.settings, fetcher=fetcher)
            result = qualify(ctx)
        finally:
            fetcher.close()
        status = "done" if result.verdict == "qualified" else "stopped"
        art = self._save(3, status, f"profile of {profile.name or self.domain}", result.model_dump(mode="json"), result.reasoning,
                         result.evidence, extra={"corpus": ctx.corpus.to_list()})
        return StepOutcome(3, status, f"{result.verdict} (score {result.score})", art)

    def _corpus(self) -> EvidenceCorpus:
        art = self.artifact(3) or {}
        return EvidenceCorpus.from_list(art.get("corpus", []))

    def _step4(self) -> StepOutcome:
        qual = QualificationResult.model_validate(self.output(3))
        profile = CompanyProfile.model_validate(self.output(2))
        if qual.verdict != "qualified":
            raise StepStop(f"{self.domain} is {qual.verdict}; Hunter is only used for qualified companies")
        result, _raw = find_poc(self.domain, profile, self.settings, self.store, no_hunter=self.options.no_hunter)
        status = "done" if result.status == "found" else "stopped"
        art = self._save(4, status, f"qualified company {profile.name or self.domain}", result.model_dump(mode="json"), result.reasoning)
        return StepOutcome(4, status, result.reasoning, art)

    def _step5(self) -> StepOutcome:
        poc = PocResult.model_validate(self.output(4))
        profile = CompanyProfile.model_validate(self.output(2))
        qual = QualificationResult.model_validate(self.output(3))
        assert poc.chosen is not None
        pains, notes = find_pain_points(poc.chosen, profile, qual, self._corpus())
        status = "done" if pains else "stopped"
        reasoning = (f"Pain points for {poc.chosen.first_name} ({poc.chosen.position}): "
                     + "; ".join(f"{p.title} [{p.kind}]: {p.why_this_person}" for p in pains) + (" " + " ".join(notes) if notes else ""))
        art = self._save(5, status, f"{poc.chosen.position} at {profile.name}", [p.model_dump(mode="json") for p in pains], reasoning.strip(),
                         [e for p in pains for e in p.evidence])
        return StepOutcome(5, status, reasoning, art)

    def _step6(self) -> StepOutcome:
        poc = PocResult.model_validate(self.output(4))
        profile = CompanyProfile.model_validate(self.output(2))
        qual = QualificationResult.model_validate(self.output(3))
        pains = [PainPoint.model_validate(p) for p in self.output(5)]
        contact: Contact = poc.chosen  # type: ignore[assignment]
        outcome = draft_email(contact, profile, qual, pains, self.settings)
        d = outcome.draft
        reasoning = (f"Hook: {d.hook} (source {d.hook_evidence_url}). Pain point: {d.pain_point_used}. {d.reasoning}"
                     + (f" FLAGGED, will not be sent: {'; '.join(outcome.problems)}" if outcome.flagged else " Passed all email checks."))
        hook_ev = [e for e in qual.evidence + [e for p in pains for e in p.evidence] if e.source_url == d.hook_evidence_url][:2]
        art = self._save(6, "done", f"email to {contact.first_name} ({contact.position})", outcome.model_dump(mode="json"), reasoning, hook_ev)
        self.store.save_run_file(self.run_id, f"{self.domain}/06_email.txt", email_text(contact, outcome.draft.subject, outcome.full_body, outcome.flagged, outcome.problems))
        return StepOutcome(6, "done", reasoning, art)

    def _step7(self) -> StepOutcome:
        poc = PocResult.model_validate(self.output(4))
        draft = self.output(6)
        contact: Contact = poc.chosen  # type: ignore[assignment]
        result = send_email(self.run_id, self.domain, draft["draft"]["subject"], draft["full_body"], contact.email, draft["flagged"],
                            self.settings, self.store, send_requested=self.options.send, visitor=self.options.visitor)
        status = "done" if result.status in ("sent", "dry_run") else "stopped"
        art = self._save(7, status, f"draft for {self.domain}", result.model_dump(mode="json"), result.reason)
        return StepOutcome(7, status, f"{result.status}: {result.reason}", art)


def email_text(contact: Contact, subject: str, body: str, flagged: bool, problems: list[str]) -> str:
    """A human-readable copy of the draft (06_email.txt)."""
    header = f"Would go to: {contact.first_name} {contact.last_name or ''} <{contact.email}> ({contact.position})\nActually sent to: OWNER_EMAIL only\nSubject: {subject}\n"
    flag = f"\n!! FLAGGED, will not be sent: {'; '.join(problems)}\n" if flagged else ""
    return header + flag + "\n" + body + "\n"


def parse_steps(spec: str) -> list[int]:
    """'1-2' -> [1, 2]; '3' -> [3]; '1-7' -> all."""
    spec = spec.strip()
    if "-" in spec:
        a, b = (int(x) for x in spec.split("-", 1))
    else:
        a = b = int(spec)
    if not (1 <= a <= b <= 7):
        raise ValueError("--steps must be like 1-7, 1-2 or 3 (numbers 1 to 7)")
    return list(range(a, b + 1))


def run_domain(run_id: str, domain: str, steps: list[int], settings: Settings, store: Store, options: RunOptions,
               on_step: Callable[[str, StepOutcome], None] | None = None) -> list[StepOutcome]:
    """Run the given steps in order; stop this domain at the first step that isn't 'done'."""
    runner = DomainRunner(run_id, domain, settings, store, options)
    outcomes = []
    for n in steps:
        existing = runner.artifact(n) if options.skip_done else None
        if existing and existing.get("status") == "done":
            outcome = StepOutcome(n, "done", "already done in this run (reused, no new API calls)", existing)
        else:
            outcome = runner.run_step(n)
        outcomes.append(outcome)
        if on_step:
            on_step(domain, outcome)
        if outcome.status != "done":
            break
    return outcomes


def find_latest_run(store: Store, domain: str, needs_step: int) -> str | None:
    """Most recent run that has an artifact for `needs_step` for this domain (to continue from).

    If that step ended as 'stopped' (e.g. the company was rejected), the next step will say so clearly.
    """
    for run_id in store.list_runs(limit=50):
        if store.load_artifact(run_id, domain, STEP_FILES[needs_step]):
            return run_id
    return None
