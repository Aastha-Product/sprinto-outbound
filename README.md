# sprinto-outbound

Automated cold-outreach pipeline for Sprinto (MESA Term 2, AI Case 2).
Scrape → Extract → Qualify (agent loop) → Find PoC (Hunter) → Pain points → Draft → Send (Gmail).

## Start here
1. Follow `docs/SETUP_WINDOWS.md` (install, `.venv`, `.env`, API keys, Gmail OAuth).
2. Open this folder in VS Code, open the Claude Code panel, and paste the prompt from `BUILD_PROMPT.md`.
3. Build phase by phase; type `next` after testing each one.

## Files already here
| File | Purpose |
|---|---|
| `CLAUDE.md` | Context + guardrails Claude Code reads every session |
| `BUILD_PROMPT.md` | The phased build prompt to paste |
| `docs/ICP.md` | Your ICP & scoring rubric (edit and defend it) |
| `docs/SETUP_WINDOWS.md` | Beginner setup guide |
| `docs/MESA_Case_02.pdf` | The case brief |
| `.env.example` | Template for your secrets/config (copy to `.env`) |
| `.claude/settings.json` | Blocks Claude Code from reading secrets, pushing, mass-deleting |
| `data/domains.txt` | Mixed seed list of prospect domains |

*(Claude Code will replace this README with a full one in Phase 6.)*
