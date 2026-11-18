# Evidence Map: what this build does when it doesn't know enough

The L4 lecture's "Evidence Map" template, filled in from **what the code actually does** (not a plan).
One row per question needed to qualify a company. Rubric points live in `docs/ICP.md`; this page explains
*where each answer comes from, how hard we look, and what happens if we still don't know*.

| Question | Data that answers it | Where it lives | Sufficient when (checked in Python) | Limit | If unanswered | Short-circuit |
|---|---|---|---|---|---|---|
| Competitor or existing Sprinto customer? | domain on a list; a trust-center link to `app.sprinto.com` or `trust.vanta.com`; "Powered by Vanta" | `ICP.md` lists · homepage links · trust page · web search "<company> Sprinto customer" | domain is listed, or a scraped link/text matches (`qualify.list_disqualifier`, `scrape.detect_signals`) | 0 LLM calls for lists/links; 1 web search for anyone scoring ≥ 40 | not a customer as far as we know | **YES**: listed / linked → rejected before any LLM scoring |
| B2B software? | a product description that names a business buyer | homepage hero · /product · /about · meta description / JSON-LD | `profile.is_b2b_software` is filled **and** its quote verifies against a fetched page | part of the shared loop budget (max 3 iterations) | `insufficient_data` → "Needs a human" | **YES**: B2B = false → rejected (`DQ_NOT_B2B`) |
| Compliance gap? (the 25-point critical row) | "SOC 2 in progress", a trust page, or its verified absence | homepage footer (kept for signals) · /security · /trust · trust subdomain | at least one GAP rule answered yes/no with a verified quote. **Absence claims only count if every trust/security page we found was readable** (`qualify.clean_signals`) | shared loop budget | `insufficient_data`; if a trust page was unreadable, the gap rules score 0 and the domain is listed under "Needs a human" | — |
| Right stage? (Series A–C, 30–500 people; critical) | funding round, headcount, open-role count | /careers · /about · /press · JSON-LD `numberOfEmployees` · web search | a STAGE rule answered with a verified quote; employee count > 2,000 → late stage | shared loop budget; Python refuses to stop while this is unknown and an about/careers/press page is unread (`fallback_if_giving_up`) | `insufficient_data` → "Needs a human" | **YES**: > 2,000 employees → rejected (`DQ_LATE_STAGE`) |
| Worst case | homepage + 5 subpages at scrape time, then 3 loop iterations (each = 1 page fetch or 1 web search) | | | **6 pages + 3 extra fetches/searches, ≤ 12 LLM calls per domain** (`MAX_PAGES_PER_DOMAIN`, `MAX_AGENT_ITERATIONS`, `MAX_LLM_CALLS_PER_DOMAIN`), and **≤ 50 paid calls / 15 searches per run** (`MAX_LLM_CALLS_PER_RUN`, `MAX_SEARCHES_PER_RUN`); at the ceiling the run stops and keeps what it has | | |

**Cost dials (L4, "your bill is a decision"):** cleaned and de-duplicated text (how much we send) · the cheapest
flash-lite model (who reads it) · an answer cache: the same prompt to the same model is answered from Neon for
14 days and the same web search for 7 days, so re-running a company costs nothing (whether we send it at all).
When a critical fact is missing, the loop fetches **/careers first** (rung one for stage), then /about, /press.

## How "enough" is decided (lecture: "the model makes the judgment; your code decides whether it has enough")
- The model only fills named fields: each ICP question becomes `yes / no / unknown` plus a quote.
- Python decides "enough": a field counts only if its quote is really on a page we fetched (`evidence.py`), and a
  critical row with every rule `unknown` keeps the loop running (`critical_unknowns`).
- A page counts as "read" only if Python says so (`PageContent.readable`): a 200 response with fewer than 50 words,
  a Cloudflare challenge, a CAPTCHA or a login form is **unknown**, never "empty" (`scrape.assess_page`).

## The scraping ladder (lecture: "the order you try things in")
| Rung | This build |
|---|---|
| 1. Do you need to scrape at all? | Contacts come from the Hunter.io **API**, not scraping. Company facts have no API, so we scrape. |
| 2. Slow down | 1 s between requests, robots.txt respected, 7-day scrape cache, Hunter cache forever. |
| 3. Real browser | **Not used** (Vercel can't run one). Instead: read the data the HTML already carries (meta description, JSON-LD, `__NEXT_DATA__`), then Gemini web search. |
| 4. Pay an unblocking service | Not used. Possible later: Jina Reader / Firecrawl for the "thin" pages only. |
| 5. Degrade gracefully | Every failure is named (`thin`, `bot_blocked`, `rate_limited`, `login_wall`, `captcha`, HTTP code) and the run continues; the report lists them under "Needs a human". |
| 6. Stop | Never logs in, never solves a CAPTCHA, never retries a bot wall, skips anything robots.txt forbids. |

## Where we deliberately differ from the lecture's Sprinto example
- **"Already certified → disqualify and stop."** We don't. Sprinto's own material says customers start with SOC 2 and
  later add ISO 27001, so a company with exactly one framework scores `GAP_ONE_FRAMEWORK` (+8) instead of being
  dropped. A company using a competitor gets −20, not a rejection.
- **"Two of three answered, one unknown: qualify, drop, or flag?"** Today: a critical unknown (stage or gap) →
  `insufficient_data`, no Hunter credit spent, listed for a human. The lecture's map suggests "qualify at lower rank,
  flagged" for an unknown stage. That's an owner decision; see `OWNER_CHECKLIST.md` section H.
- **Stage is the current round, recency is a bonus.** STAGE_SERIES (+10) asks what stage the company is at now (a 2024
  Series A company is still Series A); STAGE_RECENT_FUNDING (+5) rewards a round inside the 24-month window, checked in
  Python from the round's date.

## Source policy (L4: "just because it's public doesn't mean it's free to take")
| Concern | What this build does |
|---|---|
| **Legal / terms of service** | Reads only public marketing pages (home, pricing, security, customers, about, careers, press). Never logs in, never creates an account, never clicks through a user agreement, never solves a CAPTCHA, never retries a bot wall (the hiQ lesson: the liability came from the clicked-through agreement). Sites whose terms forbid automated access should be added to a skip list by the owner before a run. |
| **robots.txt** | Checked for every page; disallowed pages are skipped and recorded as `skipped: disallowed by robots.txt`. |
| **Load on their servers** | 1 request per second per site, at most 6 pages + 3 agent fetches, a 7-day page cache and a 14-day answer cache, so the same site isn't fetched again for a week. |
| **Privacy (GDPR, DPDP)** | The only personal data is a business contact from Hunter.io (name, title, work email), fetched only for qualified companies. Emails never leave the owner's inbox; the website masks every email address and hides contact names behind an eye icon. No personal data is scraped from websites. |
| **Ownership** | Scraped text is used only to make a qualification decision and is quoted briefly as evidence. Nothing is republished or sold; run data stays in the owner's Neon database. |
| **Data providers first** (L4: "scraping is a last resort") | Contacts come from Hunter's API, not scraping. Company facts have no free API, so the build scrapes public pages and uses Google Search grounding for facts the site doesn't state. |
