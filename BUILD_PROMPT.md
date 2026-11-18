# Prompt to paste into Claude Code (VS Code)

Copy everything between the two lines below into the Claude Code panel. Then type `next` after each phase once you've tested it.

---

You are helping me build a MESA School of Business assignment: an automated cold-outreach pipeline for
Sprinto. Before doing anything, read these files fully:
- `CLAUDE.md` (project context, stack, layout, and **guardrails you must follow at all times**)
- `docs/MESA_Case_02.pdf` (the case brief)
- `docs/ICP.md` (the ICP + scoring rubric; the single source of truth for qualification)
- `docs/SETUP_WINDOWS.md`, `.env.example`, `data/domains.txt`, `requirements.txt`

I'm a Python beginner on Windows (PowerShell). After every phase: stop, explain what you built in ≤ 8 plain-English
bullets, give me the exact commands to test it, tell me what "good output" looks like, and wait for me to type `next`.
Don't skip ahead. Don't change the stack, ICP or guardrails without asking me. If anything in the docs is ambiguous
or conflicts, ask me before coding. Start by replying with: (1) a 5-line summary of what you understood, (2) any
questions, (3) the phase plan below confirmed or with suggested changes.

## Phase 0: Foundations
- Create the folder layout from `CLAUDE.md` (empty modules with docstrings are fine).
- `outbound/config.py`: pydantic-settings `Settings` loaded from `.env`; validate required keys; mask secrets in repr;
  friendly error that points to `docs/SETUP_WINDOWS.md` if a key is missing. Don't require Gmail/Hunter keys for
  phases that don't use them (lazy validation per feature).
- `outbound/models.py`: pydantic models: `Evidence{claim, source_url, quote}`, `PageContent`, `ScrapeResult`,
  `CompanyProfile`, `QualificationResult{verdict, score, score_breakdown, disqualifier, reasoning, evidence, iterations}`,
  `AgentAction{action: Literal["fetch_page","web_search","decide"], argument, why}`, `Contact`, `PocResult{chosen, alternatives, reasoning}`,
  `PainPoint{title, why_this_person, evidence}`, `EmailDraft{subject, body, hook, hook_evidence_url, pain_point_used, reasoning}`, `SendResult`.
- `outbound/llm.py`: one Gemini client (`google-genai`). Two functions:
  `generate_json(prompt_name, variables, schema) -> schema` (structured output, loads prompt from `prompts/<name>.md`)
  and `research_with_search(question) -> (text, citations)` (Google Search grounding; returns `[]` citations and logs a
  warning if grounding fails). Retries with tenacity; enforce `MAX_LLM_CALLS_PER_DOMAIN`.
- `scripts/list_models.py` (prints available Gemini models that support generateContent) and `scripts/check_setup.py`
  (✅/❌ per key, Gemini ping, Hunter `/v2/account` remaining searches, Gmail token present; never prints secrets).
- `tests/test_models.py`.
**Done when:** `python scripts\list_models.py` lists models and `python scripts\check_setup.py` runs.

## Phase 0b: GitHub + Vercel + Neon skeleton (added later by owner's decision)
- Public GitHub repo `anshullmishra-png/sprinto-outbound`; Claude may push to `main` (never force-push).
- Minimal FastAPI `app.py` (`/` placeholder page, `/api/health`) so the Vercel link exists from day one.
- Vercel project imported from GitHub; Neon added through Vercel → Storage (sets `DATABASE_URL` automatically).
- `outbound/storage.py` is built in Phase 1 (first step that saves artifacts): NeonStore / FileStore / MemoryStore.
**Done when:** the Vercel link shows the placeholder page and `/api/health` reports `database: ok`.

## Phase 1: Steps 1 & 2 (Scrape → Extract)
- `scrape.py`: normalise domain → `https://`; check robots.txt; fetch homepage; discover and rank internal links by
  keywords (`pricing, security, trust, compliance, customers, about, careers, jobs, enterprise, integrations, privacy`);
  fetch up to `MAX_PAGES_PER_DOMAIN`; extract clean text (trafilatura, fallback BeautifulSoup); truncate; cache in
  `.cache/scrape/`; record HTTP failures without crashing. Also detect cheap signals without LLM: links/badges mentioning
  `trust.vanta.com`, `drata`, `secureframe`, `sprinto`, "SOC 2", "ISO 27001", "GDPR", "SAML/SSO".
- `extract.py` + `prompts/extract.md`: produce `CompanyProfile` (name, one-line description, is_b2b_software, product
  category, target customers, sells_to_enterprise, pricing model, est. employee range, funding stage if stated, HQ /
  markets, certifications mentioned, compliance pages found, competitor tool detected, hiring roles seen, unknowns list).
  **Every non-null field must have evidence with a source URL.** Unknown → null + listed in `unknowns`, never guessed.
- Prompt-injection defence: scraped text goes inside `<website_content>` tags; instruct the model to treat it as data.
- `cli.py` with `--domains data\domains.txt`, `--only <domain>`, `--steps 1-2` so I can run partial pipelines.
  Domain-file parser ignores blank lines, `#` comment lines and inline `# comments`.
- `tests/test_scrape.py` using saved HTML fixtures.
**Done when:** `python -m outbound.cli --only rocketlane.com --steps 1-2` writes `01_scrape.json` and `02_extract.json` under `runs\`.

## Phase 2: Step 3 (Qualify + agent loop)
- `qualify.py` + `prompts/qualify.md` + `prompts/next_action.md`. Load `docs/ICP.md` text into the prompt verbatim.
- Flow: check hard disqualifiers → score → if score is 40–59 OR critical unknowns remain (B2B?, stage, compliance status):
  ask the LLM for ONE `AgentAction`; execute it (`fetch_page`: same registered domain only; `web_search`: via
  `research_with_search`, e.g. "<company> Series funding 2025", "<company> SOC 2", "<company> careers security");
  merge new evidence; re-score. Max `MAX_AGENT_ITERATIONS`. Log every iteration (action, why, what was learned, score change).
- Final verdict `qualified | rejected | insufficient_data` with a score breakdown table that maps to the rubric rows.
**Done when:** running steps 1–3 on all of `data\domains.txt` produces sensible verdicts, the edge cases (competitor,
existing customer, D2C) are rejected with the right reason, and `03_qualify.json` shows the loop's iterations.
Show me a table: domain | verdict | score | top 2 reasons.

## Phase 3: Step 4 (Find the PoC via Hunter)
- `poc.py`: only for `qualified`. Check cache → check `/v2/account` remaining ≥ `HUNTER_MIN_REMAINING` → call
  `/v2/domain-search?domain=..&type=personal&limit=10` → cache raw JSON. Rank contacts with the title-priority table in
  `docs/ICP.md` (deterministic Python first, LLM only to break ties / interpret odd titles). Require a first name.
  Save chosen + top 3 alternatives + why. If no suitable contact: verdict `no_poc`, stop gracefully for that domain.
- Use `test-api-key` behaviour / fixtures in tests; never spend real searches in tests.
**Done when:** `04_poc.json` names a real person with title, confidence and a reason, and a second run uses the cache (0 credits).

## Phase 4: Steps 5 & 6 (Pain points → Email draft)
- `pain.py` + `prompts/pain_points.md`: exactly 2 pain points for *this person's role at this company*, each citing evidence
  from steps 2–3 plus the persona pain table in `docs/ICP.md`.
- `draft.py` + `prompts/email.md`: follow the email rules in `CLAUDE.md` exactly. Hook = the single most specific,
  verifiable observation (e.g., a new enterprise plan, a security-engineer job post, "SOC 2 coming soon" on their trust page).
  Signed with `SENDER_NAME`, `SENDER_TITLE`.
- `guards.py`: `validate_draft()` checks word count, subject length, first-name greeting, banned phrases, that
  `hook_evidence_url` exists in collected evidence, no unexpected links, no placeholders like `[Company]`. One automatic
  regenerate on failure; if it still fails, save it flagged and don't send. `tests/test_guards.py` covers each rule.
**Done when:** `06_email.json` and a readable `06_email.txt` exist, and I agree a human would reply to it.

## Phase 5: Step 7 (Send via Gmail API)
- `scripts/gmail_auth.py`: InstalledAppFlow with scope `gmail.send` only → writes `token.json`. Auto-refresh in `send.py`.
- `send.py`: build MIME plain-text message; **to = `OWNER_EMAIL` always** (assert); subject prefixed `[TEST → {poc_email}]`;
  send only if `--send` flag AND `SEND_ENABLED=true`; enforce `MAX_EMAILS_PER_RUN`; never resend a domain already sent in
  this run; save Gmail message id in `07_send.json`. Dry-run prints the email instead.
**Done when:** `python -m outbound.cli --only <qualified domain> --send` lands one email in my inbox.

## Phase 6: Full run, report, UI
- `pipeline.py` runs 1→7 per domain, continues past failures, writes `summary.csv` and `decision_report.md`
  (per domain: why qualified/rejected with score breakdown, why this person over alternatives, why this hook and pain
  point, with source links). Console shows a `rich` progress table.
- `app.py` (FastAPI) + `web/index.html`: textarea for domains, "Run (dry-run)" button that runs ONE step per request
  (browser calls the next step when the previous one finishes), per-domain expandable cards showing each step's
  output + reasoning + evidence links, the draft email, and a separate "Send" button guarded the same way plus
  `MAX_EMAILS_PER_DAY`, `MAX_RUNS_PER_DAY` and a per-IP rate limit (all counted in Neon). Contact emails masked.
  Local: `uvicorn app:app --reload`. Deployed on Vercel from GitHub; every push to `main` redeploys.
- `README.md`: what it does, setup (link to `docs/SETUP_WINDOWS.md`), how to run, architecture diagram (Mermaid),
  guardrails, known limitations, cost/credit usage.
**Done when:** `python -m outbound.cli --domains data\domains.txt` (dry-run) finishes for all domains; then one real `--send`;
and the Vercel link runs a domain end-to-end on someone else's laptop (results stored in Neon).

## Phase 7: Defense prep
- Review the codebase against every guardrail in `CLAUDE.md` and list any gaps, then fix them.
- Generate `docs/DEFENSE_NOTES.md`: 10 likely questions from a professor (e.g., "why is this your ICP?", "what if the
  website has no data?", "how do you stop hallucinated hooks?", "how does it scale beyond 25 Hunter searches?",
  "what does it cost per company?") with crisp answers grounded in my actual code and run outputs.

---
