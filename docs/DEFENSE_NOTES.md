# Defense notes: 10 likely questions, with answers from the actual code

Numbers below come from the real runs saved in Neon (latest: run `20261119-022009-3f15`, 19 Nov 2026).
Re-check them in `decision_report.md` (website: "Open decision report") before the defense.

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
`03_qualify.json` with action, why, what was learned and score before → after. Example from my run: **Attio** scored
50 from its website alone (funding unknown). Iteration 1 searched "attio.com Sprinto customer" (no citable answer, so
no change). Iteration 2 searched its funding and found *"In August 2025, Attio announced a $52 million Series B"*:
score 50 → 65, qualified. **Signeasy** went the other way: the search found it was bootstrapped with only a Seed round,
so STAGE_FUNDING became `no` and it was rejected instead of being guessed into the list.

### 5. "What if the website has no data?"
Then the profile is mostly `unknown`, the loop searches the web for the critical facts, and if they're still unknown
the verdict is `insufficient_data`. That's an honest outcome, not a forced yes. Pages that fail are recorded
(`01_scrape.json` → `error`) and the run carries on with the next domain.

### 6. "Show me a case where the system disagreed with your intuition."
**Rocketlane.** My seed list guessed "likely fit", but the scraper found its "Trust center" link points to
`app.sprinto.com`, a Sprinto-hosted trust center, so it's already a customer. `qualify.py::list_disqualifier` rejects it
with that link as evidence, before any scoring call. Also: `getastra.com` (listed customer), `vanta.com` (competitor),
`thesouledstore.com` (D2C, not B2B). In the first run, web search also showed that **SpotDraft** and **Hevo Data**
(both on my "likely fit" list) are already Sprinto customers; four of my five original guesses were wrong, which is
why the seed list was replaced.

Final verdicts (latest runs):

| Domain | Verdict | Score | Why |
|---|---|---|---|
| attio.com | **qualified** | 73 | Series B (+10) raised Aug 2025, inside the 24-month window (+5); 190+ staff; holds ISO 27001 but no confirmed SOC 2 Type II (room for a second framework, +8); enterprise tier with SSO/audit logs; US + EU |
| testsigma.com | rejected | DQ | `DQ_SPRINTO_CUSTOMER`: web search found it "uses Sprinto to power its Trust Center" (now on the known-customers list). Before that it was still a Series A company (+10) whose June 2024 round is outside the recency window. |
| signeasy.com | rejected | 53 | latest funding is a Seed round, so not Series A–C |
| gocomet.com | rejected | 39 | Series A (+10), but Feb 2022 is outside the recency window and other rows are weak |
| hex.tech | rejected | 45 | below 60 |
| stripe.com | rejected | DQ | `DQ_LATE_STAGE`: about 8,500 employees |
| leena.ai | rejected | 30 | −20: its trust center is on Drata |
| getastra.com / rocketlane.com | rejected | DQ | already Sprinto customers (list / app.sprinto.com link) |
| vanta.com | rejected | DQ | competitor |
| thesouledstore.com | rejected | DQ | D2C, not B2B |

One qualified company out of ten is the system working as intended: the brief's failure mode is "qualifying every company".

**Changes after an external review (19 Nov 2026):** stage now asks "what round is the company at" (+10) separately from
"did it raise recently" (+5, 24-month window checked in Python), so a 2024 Series A company is no longer treated as
"not Series A–C". Research rounds can only add verified answers, never silently lose them. The contact rule prefers
relevance over email confidence and returns `no_poc` rather than a random executive. Pain points are labelled
observed or inferred, and inferred ones are only asked as a question in the email.

### 7. "Why this person?"
`poc.py` ranks Hunter contacts with the title table in ICP.md: under 150 people → CTO, then founder/CEO, then
VP/Head of Engineering; 150–500 → CISO/Head of Security first. Ties are broken by Hunter confidence (≥ 70 preferred),
and nobody without a first name is ever chosen. `04_poc.json` explains why the chosen person beat the top 3 alternatives.
The LLM is only used when no title matches the table at all. In my run, Attio has 190+ people (the 150–500 band) and
Hunter returned no CISO, VP Engineering or Head of IT, so the rule fell to "any executive" and picked **Nicolas Sharp,
CEO (confidence 84)** over the VP of Marketing, Head of Solutions and VP of Sales (79 each). A founder-CEO at a
Series B company is a realistic buyer for "enterprise deals stalled on SOC 2".

### 8. "How does it scale beyond 25 Hunter searches?"
Hunter is called only for *qualified* companies (most are rejected before step 4), results are cached forever, and the
free `/account` call stops the pipeline before the last 3 searches. In practice **1 of 10** seed domains used a credit
(Attio), and every re-run of Attio cost 0 (cache). Beyond that: Hunter's paid tier, or swap `poc.py` for Apollo's API. Nothing else changes, because step 4
only has to return a `PocResult`.

### 9. "What does it cost per company?"
Gemini: 1 call for a company rejected by the lists, ~5–12 calls for a full run (capped at 12 by
`MAX_LLM_CALLS_PER_DOMAIN`), and every run is capped at 50 paid calls / 15 web searches. Hunter: 0–1 search.
Time: ~20–60 s per company vs. 15–20 minutes by hand (from the brief). My runs: list rejections used **1 call** each
(Astra, Vanta, The Souled Store); full qualification with the agent loop used **3–8 calls**; Attio end-to-end
(qualify + contact + pain points + email) used **8 calls, 1 Hunter credit**. Re-qualifying 5 companies with web search
cost 17 calls and 8 searches. We use the cheapest model (`gemini-3.5-flash-lite`) and cache every answer, so a
re-run on unchanged data is free (L4: "your bill is a decision").

### 10. "Could this email a real prospect by mistake? Could someone abuse the public site?"
No. `send.py` takes no recipient at all; it always uses `OWNER_EMAIL` and calls `assert_recipient()` on the final
message right before Gmail. Sending needs `--send` (or the button) AND `SEND_ENABLED=true`, max 3 per run, never the
same domain twice, max 5/day on the website. The Gmail permission is `gmail.send` only. The public site limits
everyone to 20 domains/day and 10 per run, rate-limits each IP, and masks every email address in its responses.
All of this is tested in `tests/test_guards.py` and `tests/test_app.py`.

### 11. "Your scraper says it worked. How do you know it actually read anything?"
Because "it returned 200" isn't the test; Python counts. `scrape.assess_page` marks a page `thin` if it has under 50
readable words (usually drawn by JavaScript after load: View Source ≠ Inspect), and also names Cloudflare walls,
CAPTCHAs, login forms and HTTP 429. Those pages count as **unknown, never as empty**. That matters for the rules that
score something *missing*: in my cached scrapes `trust.vanta.com` came back with 3 words, and the trust centers of
Hevo Data and SpotDraft were blocked by robots.txt. Both of those are Sprinto customers whose trust centers run on
Sprinto. Before this check, "no SOC 2 found on the pages checked" could have earned +8 for a gap that doesn't exist.
Now `ICP.md` → *Rules that need a readable trust page* forces those rules to 0 and the report's **Needs a human**
list says which page to open (`tests/test_silent_failures.py`). A trust page that returns 404 still counts as real
absence. For thin pages we also read what the HTML already carries for machines (meta description, JSON-LD
headcount/founding year, `__NEXT_DATA__`), so many "empty" pages still yield facts without a browser.

### 12. "Why a loop and not a fixed pipeline? And where's your Evidence Map?"
A fixed pipeline decides in advance that the homepage is enough; for a thin homepage it guesses. Here the model fills
named fields, and **Python** decides whether they're filled (verified quote or not); the loop only runs while a
critical field is empty, the score is borderline, or the customer check is pending, and it's capped (3 iterations,
12 LLM calls). At the cap the answer is `insufficient_data` + the "Needs a human" list: a designed hand-off, not a
failure. The full map (question → source page → "sufficient when" → limit → if unanswered → short-circuit) is in
`docs/EVIDENCE_MAP.md`, including two places where I deliberately differ from the lecture's example.

---

**Bonus: "Why Gemini / why not a headless browser / why Neon?"** Gemini: free tier plus built-in Google Search
grounding for the agent loop. No headless browser: lighter, polite, works on Vercel, and 10–50× cheaper per page;
the trade-off (JS-heavy sites) is detected (`thin` pages), partly recovered from data embedded in the HTML, and
otherwise covered by web search or handed to a human. Neon: Vercel functions have no permanent disk, so artifacts, the Hunter cache and the send
log need a database, and Postgres is the simplest one Vercel connects to.
