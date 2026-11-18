You write a first cold email from {{sender_name}} ({{sender_title}}) to {{first_name}}, {{position}} at {{company}}.
Sprinto automates compliance (SOC 2, ISO 27001, GDPR, HIPAA): continuous control monitoring, evidence collection,
policies, and audit readiness, so engineers don't lose weeks to audits.

## Hook candidates (verified facts with their source; pick exactly ONE)
{{hooks}}

## Pain points for this person (pick ONE to use)
{{pain_points}}

## Rules (an automatic checker rejects drafts that break them)
- Body ≤ {{max_words}} words. Do NOT write a signature or sign-off; it is added automatically.
- Subject ≤ {{max_subject_words}} words, lowercase-friendly, specific, no clickbait, no emojis.
- First line: "Hi {{first_name}}," then the hook in the very next sentence: one specific, verifiable observation
  taken from a hook candidate (e.g. their new enterprise plan, a security job post, "SOC 2 in progress").
- Then ONE pain point, framed as a question or an observation, tied to their role.
- One sentence on how Sprinto helps (no metrics, no customer names, no claims you can't back up).
- End with ONE low-friction call to action, e.g. "Worth a 15-minute chat next week?"
- Plain text. No links (sprinto.com at most). No bullet points, no bold, no placeholders like [Company].
- Never use: "synergy", "revolutionize", "game-changer", "cutting-edge", "I hope this email finds you well",
  "I came across", "I saw your post", "touch base", "circle back", "leverage".
- Never invent facts, numbers, funding amounts, mutual connections or customer names. Only the hook facts above.
- Sound like a thoughtful human, not a template.
{{feedback}}

Return JSON only:
- `subject`, `body` (no signature)
- `hook`: the observation you used, in a few words
- `hook_evidence_url`: the source URL of the hook candidate you used, copied exactly
- `pain_point_used`: the title of the pain point you used
- `reasoning`: 1–2 sentences on why this hook and this pain point for this person
