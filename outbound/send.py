"""Step 7: send the email through the Gmail API, with every guardrail checked in order.

The recipient is never a free-text address: callers pass an index into Settings.recipients()
(OWNER_EMAIL first, then the TEST_RECIPIENTS the owner approved), and `assert_recipient` re-checks the
final message against that list right before calling Gmail.

Checks, in order (the first one that applies decides the outcome):
1. Draft flagged by guards.py           -> blocked
2. No --send flag / Send button         -> dry_run (nothing leaves your machine)
3. SEND_ENABLED is not true             -> dry_run
4. Domain already sent in this run      -> skipped
5. MAX_EMAILS_PER_RUN reached           -> blocked
6. MAX_EMAILS_PER_DAY reached (website) -> blocked
7. Otherwise: send, log it, return the Gmail message id.
Subject is prefixed "[TEST → poc_email]" so you can see who it would have gone to.
"""

from __future__ import annotations

import base64
import logging
from email.mime.text import MIMEText
from email.utils import formataddr

from outbound import PROJECT_ROOT
from outbound.config import Settings
from outbound.guards import assert_recipient
from outbound.models import SendResult
from outbound.storage import Store

log = logging.getLogger(__name__)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.send"]  # send-only; never read/modify


def tagged_subject(subject: str, poc_email: str) -> str:
    return f"[TEST → {poc_email}] {subject}"


def build_message(subject: str, body: str, settings: Settings, to: str) -> MIMEText:
    msg = MIMEText(body, "plain", "utf-8")
    msg["To"] = to
    msg["From"] = formataddr((settings.sender_name, settings.owner_email))
    msg["Subject"] = subject
    return msg


def gmail_credentials(settings: Settings):
    """Load OAuth credentials: token.json locally, or the three GMAIL_* env vars on Vercel. Auto-refreshes."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    token_path = PROJECT_ROOT / settings.gmail_token_file
    refresh = settings.gmail_refresh_token.get_secret_value()
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)
    elif refresh and settings.gmail_client_id:
        creds = Credentials(None, refresh_token=refresh, token_uri="https://oauth2.googleapis.com/token",
                            client_id=settings.gmail_client_id, client_secret=settings.gmail_client_secret.get_secret_value(),
                            scopes=GMAIL_SCOPES)
    else:
        raise RuntimeError("Gmail is not authorised. Run: python scripts\\gmail_auth.py (see docs/SETUP_WINDOWS.md).")
    if not creds.valid:
        creds.refresh(Request())
        if token_path.exists():
            token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds


def gmail_send(msg: MIMEText, settings: Settings) -> str:
    """Send through Gmail and return the message id."""
    from googleapiclient.discovery import build

    service = build("gmail", "v1", credentials=gmail_credentials(settings), cache_discovery=False)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    return service.users().messages().send(userId="me", body={"raw": raw}).execute()["id"]


def send_email(run_id: str, domain: str, subject: str, body: str, poc_email: str, flagged: bool,
               settings: Settings, store: Store, send_requested: bool, sender=None, recipient: int = 0) -> SendResult:
    """Step 7. `sender` is swappable so tests never touch Gmail. `recipient` indexes Settings.recipients() (0 = owner)."""
    sender = sender or gmail_send
    full_subject = tagged_subject(subject, poc_email)
    allowed = settings.recipients()
    to = allowed[recipient] if 0 <= recipient < len(allowed) else ""

    def result(status: str, reason: str, msg_id: str | None = None) -> SendResult:
        return SendResult(status=status, to=to, would_have_gone_to=poc_email, subject=full_subject, gmail_message_id=msg_id, reason=reason)

    if flagged:
        return result("blocked", "draft failed the email checks (guards.py), so it is never sent")
    if not send_requested:
        return result("dry_run", "dry run: no --send flag / Send button, nothing was sent")
    if not settings.send_enabled:
        return result("dry_run", "SEND_ENABLED is not true in .env, nothing was sent")
    if not to:
        return result("blocked", "that test recipient is not on the approved list")
    settings.require("gmail")
    already = store.sent_in_run(run_id)
    if domain in already:
        return result("skipped", "this domain was already emailed in this run")
    if len(already) >= settings.max_emails_per_run:
        return result("blocked", f"MAX_EMAILS_PER_RUN ({settings.max_emails_per_run}) reached")
    if store.sent_today() >= settings.max_emails_per_day:
        return result("blocked", f"MAX_EMAILS_PER_DAY ({settings.max_emails_per_day}) reached")

    msg = build_message(full_subject, body, settings, to)
    assert_recipient(msg["To"], allowed)  # final check, right before sending
    msg_id = sender(msg, settings)
    store.log_event("send", run_id=run_id, domain=domain, status="sent")
    log.info("[%s] sent to approved inbox #%d (Gmail id %s)", domain, recipient, msg_id)
    return result("sent", "sent to the owner's inbox only" if recipient == 0 else "sent to an approved test inbox only", msg_id)
