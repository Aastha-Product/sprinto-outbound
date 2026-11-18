"""Check that everything is configured. Prints ✅ / ❌ / ⚠️ per item and NEVER prints a secret.

Run:  python scripts\\check_setup.py
"""

import _bootstrap  # noqa: F401  (sets up imports + UTF-8 console)

import sys

from outbound import PROJECT_ROOT
from outbound.config import SETUP_GUIDE, get_settings
from outbound.logging_setup import setup_logging

OK, BAD, WARN = "✅", "❌", "⚠️ "


def mask_email(email: str) -> str:
    """a.person@gmail.com -> a***@gmail.com"""
    name, _, domain = email.partition("@")
    return f"{name[:1]}***@{domain}" if domain else "***"


def main() -> int:
    setup_logging()
    problems = 0

    def line(status: str, label: str, detail: str = "") -> None:
        nonlocal problems
        if status == BAD:
            problems += 1
        print(f"{status} {label}" + (f": {detail}" if detail else ""))

    print("── Files")
    env = PROJECT_ROOT / ".env"
    line(OK if env.exists() else BAD, ".env file", "found" if env.exists() else "missing (Copy-Item .env.example .env)")
    s = get_settings()

    print("\n── Owner / sender")
    if s.owner_email_ok():
        line(OK, "OWNER_EMAIL", mask_email(s.owner_email))
    else:
        line(BAD, "OWNER_EMAIL", "not set (or still you@gmail.com)")
    line(OK if s.sender_name else BAD, "SENDER_NAME", s.sender_name or "not set")
    line(OK if s.sender_title else BAD, "SENDER_TITLE", s.sender_title or "not set")

    print("\n── Gemini")
    has_key = not s.missing_for("gemini_key")
    line(OK if has_key else BAD, "GEMINI_API_KEY", "set" if has_key else "not set")
    line(OK if s.gemini_model else BAD, "GEMINI_MODEL", s.gemini_model or "not set (run python scripts\\list_models.py)")
    if has_key and s.gemini_model:
        try:
            from outbound.llm import ping

            reply = ping()
            line(OK if reply else WARN, "Gemini ping", f"model replied {reply[:20]!r}")
        except Exception as exc:
            line(BAD, "Gemini ping", f"{type(exc).__name__}: {str(exc)[:150]}")
    line(OK, "USE_SEARCH_GROUNDING", str(s.use_search_grounding))

    print("\n── Hunter.io")
    if s.missing_for("hunter"):
        line(WARN, "HUNTER_API_KEY", "not set (only needed from Phase 3)")
    else:
        line(OK, "HUNTER_API_KEY", "set")
        try:
            from outbound.poc import hunter_remaining_searches

            left = hunter_remaining_searches(s)
            status = OK if left >= s.hunter_min_remaining else WARN
            line(status, "Hunter searches left this month", f"{left} (pipeline stops below {s.hunter_min_remaining})")
        except Exception as exc:
            line(BAD, "Hunter /v2/account", f"{type(exc).__name__}: {str(exc)[:150]}")

    print("\n── Gmail")
    creds = PROJECT_ROOT / s.gmail_credentials_file
    token = PROJECT_ROOT / s.gmail_token_file
    line(OK if creds.exists() else WARN, "credentials.json", "found" if creds.exists() else "not yet (only needed from Phase 5)")
    line(OK if token.exists() else WARN, "token.json", "found" if token.exists() else "not yet (run scripts\\gmail_auth.py in Phase 5)")
    line(OK, "SEND_ENABLED", f"{s.send_enabled} (emails only go out with --send AND true)")

    print("\n── Limits")
    print(
        f"   pages/domain={s.max_pages_per_domain}  chars/page={s.max_chars_per_page}  "
        f"agent iterations={s.max_agent_iterations}  LLM calls/domain={s.max_llm_calls_per_domain}  "
        f"emails/run={s.max_emails_per_run}"
    )

    print()
    if problems:
        print(f"{BAD} {problems} problem(s). Fix them in .env. Help: {SETUP_GUIDE}")
        return 1
    print(f"{OK} All required settings look good.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
