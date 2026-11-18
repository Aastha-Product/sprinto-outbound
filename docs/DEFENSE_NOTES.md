# Defense notes: 10 likely questions, with answers from the actual code

Replace each `‹fill after run›` with numbers from `runs\<id>\summary.csv` / `decision_report.md` after your first real run.

---

### 1. "Why is this your ICP?"
The case brief says SaaS, Series A–C, either preparing for an audit or pushed by enterprise customers. I turned that
into 18 checkable questions in `docs/ICP.md`, grouped in 6 rows that add up to 100:
**selling to enterprise (20)** because enterprise buyers are who demand SOC 2; **customer data (15)** because that's
what auditors care about; **compliance gap (25, the biggest)** because a company that's already fully certified has
no reason to buy; **stage (15)**, since Series A–C, 30–500 people, can pay ~$8–10K/yr but isn't too big; **hiring (15)**,
since security/GRC hires mean the pain is current; **geography (10)**, since US → SOC 2 and EU/UK → GDPR/ISO.
Plus a −20 penalty for already using Vanta/Drata (a switching sale is harder) and 4 hard disqualifiers.

### 2. "How do you know the score isn't just the LLM's opinion?"
The LLM never gives points. In `prompts/qualify.md` it only answers each question `yes / no / unknown` with a quote.
`outbound/qualify.py::score_signals()` applies the fixed points from ICP.md: yes + verified evidence = points,
anything else = 0, each row capped. It's a pure function, so the same signals always give the same score (tested in
`tests/test_qualify.py::test_same_signals_always_give_same_score`). Changing a number in ICP.md changes the scorer
with no code change.

### 3. "How do you stop hallucinated facts and hooks?"
Three layers:
1. `outbound/evidence.py`: every quote must appear in the text of a page we fetched (or a web-search source we got
   back). An invented URL or quote is dropped (`test_hallucinated_evidence_is_dropped`).
2. `extract.py` clears any profile field whose evidence didn't verify; it becomes "unknown", never a guess.
3. The email's `hook_evidence_url` must be one of the verified sources (`guards.py`). Otherwise it's regenerated once,
   then flagged and never sent. The signature is added by Python, so the LLM can't invent a sender.

### 4. "What does the agent loop actually do? Isn't it just a for-loop?"
It runs only when it's needed: borderline score (40–59), a critical unknown (B2B? stage? compliance status?), or
the mandatory "is this already a Sprinto customer?" web search for anyone scoring ≥ 40. Each iteration the LLM picks
ONE action from a fixed set (`fetch_page` on the same domain, `web_search`, or `decide`). Python validates it
(an off-domain URL becomes `decide`), runs it, adds the new evidence and re-scores. It stops at 3 iterations, when the
LLM budget gets low, or when even "every unknown = yes" can't reach 60. Every iteration is saved in
`03_qualify.json` with action, why, what was learned and score before → after. Example from my run: ‹fill after run›.

### 5. "What if the website has no data?"
Then the profile is mostly `unknown`, the loop searches the web for the critical facts, and if they're still unknown
the verdict is `insufficient_data`. That's an honest outcome, not a forced yes. Pages that fail are recorded
(`01_scrape.json` → `error`) and the run carries on with the next domain.

### 6. "Show me a case where the system disagreed with your intuition."
**Rocketlane.** My seed list guessed "likely fit", but the scraper found its "Trust center" link points to
`app.sprinto.com`, a Sprinto-hosted trust center, so it's already a customer. `qualify.py::list_disqualifier` rejects it
with that link as evidence, before any scoring call. Also: `getastra.com` (listed customer), `vanta.com` (competitor),
`thesouledstore.com` (D2C, not B2B). Final verdict table: ‹fill after run›.

### 7. "Why this person?"
`poc.py` ranks Hunter contacts with the title table in ICP.md: under 150 people → CTO, then founder/CEO, then
VP/Head of Engineering; 150–500 → CISO/Head of Security first. Ties are broken by Hunter confidence (≥ 70 preferred),
and nobody without a first name is ever chosen. `04_poc.json` explains why the chosen person beat the top 3 alternatives.
The LLM is only used when no title matches the table at all.

### 8. "How does it scale beyond 25 Hunter searches?"
Hunter is called only for *qualified* companies (most are rejected before step 4), results are cached forever, and the
free `/account` call stops the pipeline before the last 3 searches. In practice ‹fill after run›/10 seed domains used a
credit. Beyond that: Hunter's paid tier, or swap `poc.py` for Apollo's API. Nothing else changes, because step 4
only has to return a `PocResult`.

### 9. "What does it cost per company?"
Gemini: 1 call for a company rejected by the lists, ~5–12 calls for a full run (capped at 12 by
`MAX_LLM_CALLS_PER_DOMAIN`). On the free tier that's ~$0. Hunter: 0–1 search. Time: ~20–60 s per company
vs. 15–20 minutes by hand (from the brief). My run: ‹fill after run› (see `llm_calls` and `hunter_credits` in summary.csv).

### 10. "Could this email a real prospect by mistake? Could someone abuse the public site?"
No. `send.py` takes no recipient at all; it always uses `OWNER_EMAIL` and calls `assert_recipient()` on the final
message right before Gmail. Sending needs `--send` (or the button) AND `SEND_ENABLED=true`, max 3 per run, never the
same domain twice, max 5/day on the website. The Gmail permission is `gmail.send` only. The public site limits
everyone to 20 domains/day and 10 per run, rate-limits each IP, and masks every email address in its responses.
All of this is tested in `tests/test_guards.py` and `tests/test_app.py`.

---

**Bonus: "Why Gemini / why not a headless browser / why Neon?"** Gemini: free tier plus built-in Google Search
grounding for the agent loop. No headless browser: lighter, polite, works on Vercel; the trade-off (JS-heavy sites) is
covered by web search. Neon: Vercel functions have no permanent disk, so artifacts, the Hunter cache and the send
log need a database, and Postgres is the simplest one Vercel connects to.
