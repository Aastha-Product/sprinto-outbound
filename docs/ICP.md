# Sprinto ICP & Qualification Rubric

> Source of truth for Step 3 (Qualify). `outbound/qualify.py` loads this file into the prompt.
> **Owner: edit this freely, because defending it is part of the assignment.** It's a researched starting point, not a final answer.

## Why this definition (research summary)
- The case brief: SaaS, **Series A–C**, either **preparing for an audit** or **under pressure from enterprise
  customers** to get certified.
- Sprinto positions itself for cloud-first startups and mid-market teams, especially those **selling into
  enterprise**: "if customers ask for SOC 2 or ISO 27001, Sprinto fits." Buyers are security, compliance,
  engineering leaders and founders.
- Sprinto's own content says companies usually start with **SOC 2 Type 1**, then move to Type 2 and add ISO
  27001. So "first certification" and "adding a second framework" are both entry points.
- Third-party reviews put the startup tier at roughly **$8–10K/year** for a single framework. The prospect must be
  big enough to pay that, so pre-seed companies with a handful of employees are a weak fit.
- Competitors: **Vanta, Drata, Secureframe, Scrut, Thoropass, OneTrust/Tugboat**. A company already running one
  of these (e.g., trust center hosted on `trust.vanta.com` / Drata / Secureframe) is a switching sale, so score it low.

## Hard disqualifiers (any one → `rejected`)
1. Not a B2B software / cloud product (e.g., D2C brand, agency, retailer, consumer app with no B2B tier).
2. Is a compliance-automation vendor (Sprinto competitor) or Sprinto itself.
3. Public company or clearly late-stage (≈ > 2,000 employees, IPO'd, or "Fortune 500" scale).
4. Publicly listed as an existing Sprinto customer (case study / logo on sprinto.com).

## Known Sprinto customers (disqualifier #4)
> `outbound/qualify.py` reads the domains in this list (one per bullet, text after the domain is a note).
> Add a domain here when you find it as a case study or logo on sprinto.com. The agent loop also runs a
> web search for "<company> Sprinto" as a secondary check.

- getastra.com (Astra Security, publicly listed as a Sprinto customer)

## Scoring (0–100), applied only if no disqualifier
| Signal | Points | Evidence that counts |
|---|---|---|
| Sells to enterprise / mid-market | 0–20 | Enterprise plan, "Book a demo", enterprise logos, SSO/SAML, sales-led pricing |
| Handles customer data | 0–15 | Integrations with customer systems, stores PII/financial/health data, data processing language |
| Compliance gap or in-progress signal | 0–25 | No trust/security page; "SOC 2 in progress / coming soon"; only GDPR page; has SOC 2 but not ISO (or vice-versa); security questionnaire mentions |
| Stage fit: Series A–C | 0–15 | Funding news within ~24 months; ~30–500 employees |
| Hiring signal | 0–15 | Open roles: security, compliance, GRC, IT, DevSecOps, enterprise AEs |
| Geography pushing compliance | 0–10 | Sells to US (SOC 2) / EU-UK (GDPR, ISO) customers |
| Already on a competitor | −20 | Vanta/Drata/Secureframe trust center or badge |

## Verdicts
- **≥ 60** → `qualified`
- **40–59** → borderline: the agent loop gathers more evidence (max 3 iterations), then re-scores
- **< 40** → `rejected`
- If key fields (B2B? stage? compliance status?) are still unknown after the loop → `insufficient_data`

Every point awarded must cite evidence `{claim, source_url, quote}`. No evidence, no points.

## Who is the right PoC (Step 4)
| Company size | Preferred titles (in order) |
|---|---|
| < 150 employees | CTO → Co-founder/CEO → VP/Head of Engineering |
| 150–500 | CISO / Head of Security → VP Engineering / CTO → Head of IT / Compliance |
| Any size, fallback | Head of Ops / COO, then any `executive` seniority in IT/management |

Prefer Hunter `confidence ≥ 70` and `type=personal`. Record why the chosen person beat the others.

## Typical pain points by persona (Step 5 picks 2, grounded in evidence)
- **CTO / Eng leader:** engineers pulled off roadmap to collect audit evidence; security questionnaires slowing deals.
- **Founder / CEO:** enterprise deals stalled waiting on SOC 2; audit cost and timeline uncertainty.
- **Security / Compliance lead:** spreadsheets across multiple frameworks; continuous monitoring after the audit.
- **Ops / COO:** vendor risk reviews, policy management, employee onboarding/offboarding controls.
