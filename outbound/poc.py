"""Step 4: find the right point of contact (PoC) with Hunter.io, without wasting credits.

Order of operations (only ever for `qualified` companies):
1. Cache first: if we searched this domain before, reuse it. Costs 0 credits.
2. `--no-hunter`: if set and there's no cache, stop here (status `skipped`).
3. Free quota check (`/v2/account`). If fewer than HUNTER_MIN_REMAINING searches are left, stop gracefully.
4. One Domain Search: `type=personal`, `limit=10` (the free-plan maximum). Raw JSON is cached forever.
5. Rank contacts in Python with the title table in docs/ICP.md (size band -> priority -> confidence).
   The LLM is used ONLY if no title matches the table at all, to interpret unusual titles.
6. Contacts without a first name are never chosen. No suitable contact -> status `no_poc`.
"""

from __future__ import annotations

import logging

import httpx
from pydantic import BaseModel

from outbound import llm
from outbound.config import Settings
from outbound.icp import ICP, PocTier, load_icp, normalise_title
from outbound.models import CompanyProfile, Contact, PocResult
from outbound.qualify import employee_lower_bound
from outbound.storage import Store

log = logging.getLogger(__name__)

HUNTER_BASE = "https://api.hunter.io/v2"
HUNTER_LIMIT = 10  # free plan: limit + offset must be <= 10


class HunterError(RuntimeError):
    """Hunter returned an error (bad key, plan limit, ...)."""


def _hunter_headers(settings: Settings) -> dict[str, str]:
    # The key goes in a header, not the URL, so it can never show up in a logged URL.
    return {"X-API-KEY": settings.hunter_api_key.get_secret_value(), "User-Agent": settings.user_agent}


def hunter_remaining_searches(settings: Settings, client: httpx.Client | None = None) -> int:
    """Ask Hunter how many Domain Searches are left this month. This call is free."""
    settings.require("hunter")
    get = client.get if client else httpx.get
    resp = get(f"{HUNTER_BASE}/account", headers=_hunter_headers(settings), timeout=settings.request_timeout_seconds)
    if resp.status_code >= 400:
        raise HunterError(f"Hunter /account returned HTTP {resp.status_code}")
    data = resp.json().get("data", {})
    searches = data.get("requests", {}).get("searches") or data.get("calls") or {}
    return max(int(searches.get("available", 0)) - int(searches.get("used", 0)), 0)


def hunter_domain_search(domain: str, settings: Settings, client: httpx.Client | None = None) -> dict:
    """One paid Domain Search (1 credit). Returns Hunter's raw JSON."""
    settings.require("hunter")
    get = client.get if client else httpx.get
    params = {"domain": domain, "type": "personal", "limit": HUNTER_LIMIT}
    resp = get(f"{HUNTER_BASE}/domain-search", params=params, headers=_hunter_headers(settings), timeout=settings.request_timeout_seconds)
    if resp.status_code >= 400:
        detail = ""
        try:
            detail = "; ".join(e.get("details", "") for e in resp.json().get("errors", []))
        except ValueError:
            pass
        raise HunterError(f"Hunter domain-search returned HTTP {resp.status_code} {detail}".strip())
    return resp.json()


def parse_contacts(raw: dict) -> list[Contact]:
    contacts = []
    for e in raw.get("data", {}).get("emails", []) or []:
        if not e.get("value"):
            continue
        contacts.append(Contact(
            first_name=(e.get("first_name") or "").strip() or None,
            last_name=(e.get("last_name") or "").strip() or None,
            email=e["value"],
            position=e.get("position"),
            seniority=e.get("seniority"),
            department=e.get("department"),
            confidence=e.get("confidence"),
            linkedin=e.get("linkedin"),
        ))
    return contacts


# ---------------------------------------------------------------- ranking (deterministic)
def tiers_for_size(icp: ICP, employees: int | None) -> list[PocTier]:
    """Title tiers that apply to a company of this size (unknown size -> smallest band)."""
    size = employees if employees is not None else 1
    out = []
    for t in icp.poc_tiers:
        if t.min_employees is None or t.min_employees <= size <= (t.max_employees or size):
            out.append(t)
    if employees is None or not any(t.min_employees is not None for t in out):
        # Unknown size or a size outside every band: use the smallest band + "any" rows.
        smallest = min((t.min_employees for t in icp.poc_tiers if t.min_employees is not None), default=None)
        out = [t for t in icp.poc_tiers if t.min_employees is None or t.min_employees == smallest]
    return sorted(out, key=lambda t: t.priority)


def match_tier(contact: Contact, tiers: list[PocTier]) -> PocTier | None:
    title = normalise_title(contact.position or "")
    for tier in tiers:
        for kw in tier.keywords:
            if kw.startswith("seniority:"):
                # "seniority:executive in executive it management" -> seniority must match AND department must be listed
                rule, _, depts = kw.split(":", 1)[1].partition(" in ")
                if (contact.seniority or "").lower() != rule.strip():
                    continue
                if depts.strip() and (contact.department or "").lower() not in depts.split():
                    continue
                return tier
            elif normalise_title(kw) in title:
                return tier
    return None


def rank_contacts(contacts: list[Contact], profile: CompanyProfile, icp: ICP) -> list[tuple[Contact, PocTier]]:
    """Contacts with a first name and a matching title, best first."""
    tiers = tiers_for_size(icp, employee_lower_bound(profile.employee_range))
    min_conf = icp.settings.get("min_confidence", 0)
    ranked = []
    for c in contacts:
        if not c.first_name:
            continue
        tier = match_tier(c, tiers)
        if tier:
            ranked.append((c, tier))
    ranked.sort(key=lambda ct: (ct[1].priority, (ct[0].confidence or 0) < min_conf, -(ct[0].confidence or 0)))
    return ranked


class TitlePick(BaseModel):
    """LLM tie-break answer: index into the candidate list, or -1 for nobody suitable."""

    index: int
    why: str


def llm_pick(contacts: list[Contact], profile: CompanyProfile, icp: ICP) -> tuple[Contact | None, str]:
    """Only used when NO title matched the ICP table: ask the LLM to interpret unusual titles."""
    named = [c for c in contacts if c.first_name and c.position]
    if not named:
        return None, "no contact with both a first name and a job title"
    listing = "\n".join(f"{i}. {c.position} (seniority: {c.seniority or '?'}, department: {c.department or '?'})" for i, c in enumerate(named))
    pick = llm.generate_json(
        "poc_titles",
        {"company": profile.name or "the company", "size": profile.employee_range or "unknown",
         "poc_rules": icp.text.split("## Who is the right PoC", 1)[-1].split("## Typical pain points", 1)[0], "candidates": listing},
        TitlePick,
    )
    if 0 <= pick.index < len(named):
        return named[pick.index], f"no title matched the ICP table exactly; LLM interpreted the titles: {pick.why}"
    return None, f"LLM found no suitable decision-maker: {pick.why}"


def _describe(c: Contact) -> str:
    name = " ".join(p for p in (c.first_name, c.last_name) if p)
    return f"{name} ({c.position or 'no title'}, confidence {c.confidence if c.confidence is not None else '?'})"


# ---------------------------------------------------------------- main entry point
def find_poc(domain: str, profile: CompanyProfile, settings: Settings, store: Store, no_hunter: bool = False,
             client: httpx.Client | None = None) -> tuple[PocResult, dict | None]:
    """Step 4 for one qualified domain. Returns (result, raw_hunter_json_or_None)."""
    icp = load_icp()
    credits = 0
    raw = store.get_cache("hunter", domain)
    if raw is not None:
        log.info("[%s] poc: using cached Hunter result (0 credits)", domain)
    elif no_hunter:
        return PocResult(status="skipped", reasoning="--no-hunter was set and there is no cached Hunter result for this domain"), None
    else:
        settings.require("hunter")
        left = hunter_remaining_searches(settings, client)
        if left < settings.hunter_min_remaining:
            return PocResult(status="skipped", reasoning=f"only {left} Hunter searches left (HUNTER_MIN_REMAINING={settings.hunter_min_remaining}); not spending one"), None
        raw = hunter_domain_search(domain, settings, client)
        store.set_cache("hunter", domain, raw)
        credits = 1
        log.info("[%s] poc: Hunter search done (1 credit, %d left before this call)", domain, left)

    contacts = parse_contacts(raw)
    ranked = rank_contacts(contacts, profile, icp)
    size = profile.employee_range or "unknown size"
    if ranked:
        chosen, tier = ranked[0]
        alts = [c for c, _ in ranked[1:4]]
        why = (f"Company size: {size}. ICP title priority for this size picks '{tier.title}' (priority {tier.priority}). "
               f"Chose {_describe(chosen)}.")
        if alts:
            why += " Ranked below: " + "; ".join(f"{_describe(c)} [{t.title}, priority {t.priority}]" for c, t in ranked[1:4]) + "."
        return PocResult(status="found", chosen=chosen, alternatives=alts, reasoning=why, hunter_credits_used=credits), raw

    if not contacts:
        return PocResult(status="no_poc", reasoning="Hunter returned no personal email addresses for this domain", hunter_credits_used=credits), raw
    chosen, why = llm_pick(contacts, profile, icp)
    if chosen is None:
        return PocResult(status="no_poc", reasoning=why, alternatives=[c for c in contacts if c.first_name][:3], hunter_credits_used=credits), raw
    alts = [c for c in contacts if c is not chosen and c.first_name][:3]
    return PocResult(status="found", chosen=chosen, alternatives=alts, reasoning=why, hunter_credits_used=credits), raw
