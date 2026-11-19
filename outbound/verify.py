"""Visitor inbox verification for the public site.

A visitor may receive the test email at their own address, but only after proving they own it: we email a
6-digit code and they type it back. This stops the site being used to make the owner's Gmail mail strangers.
Codes are stored hashed, expire after 15 minutes and allow 5 tries; a verified inbox stays valid for 24 hours.
Daily caps limit how many code emails go out (per address and in total).
"""

from __future__ import annotations

import hashlib
import secrets
import time
from datetime import datetime, timedelta, timezone

from outbound.config import EMAIL_RE, Settings
from outbound.guards import assert_recipient
from outbound.storage import Store

CODE_TTL_S = 15 * 60
VERIFIED_TTL_S = 24 * 3600
MAX_TRIES = 5
MAX_CODES_PER_EMAIL_PER_DAY = 3
MAX_CODES_PER_DAY = 30


class VerifyError(ValueError):
    """A user-facing reason the code could not be sent or accepted."""


def _norm(email: str) -> str:
    return email.strip().lower()


def _key(email: str) -> str:
    return hashlib.sha256(_norm(email).encode()).hexdigest()  # the raw address is never stored


def _hash(email: str, code: str) -> str:
    return hashlib.sha256(f"{_norm(email)}:{code.strip()}".encode()).hexdigest()


def start(email: str, settings: Settings, store: Store, sender=None) -> None:
    """Email a fresh 6-digit code to `email`. `sender` is swappable so tests never touch Gmail."""
    email = email.strip()
    if len(email) > 254 or not EMAIL_RE.match(email):
        raise VerifyError("That doesn't look like an email address.")
    if not settings.send_enabled:
        raise VerifyError("Sending is switched off on this server.")
    settings.require("gmail")
    day = datetime.now(timezone.utc) - timedelta(days=1)
    if store.count_events("verify_code", since=day) >= MAX_CODES_PER_DAY:
        raise VerifyError("Too many verification emails today. Please try again tomorrow.")
    if store.count_events("verify_code", since=day, status=_key(email)[:16]) >= MAX_CODES_PER_EMAIL_PER_DAY:
        raise VerifyError("Too many codes for this address today. Use the last one, or try tomorrow.")

    from outbound.send import build_message, gmail_send  # late import: send.py imports this module

    code = f"{secrets.randbelow(10**6):06d}"
    store.set_cache("verify", _key(email), {"hash": _hash(email, code), "expires": time.time() + CODE_TTL_S, "tries": 0, "verified_until": 0})
    msg = build_message("Your Sprinto Outbound test code",
                        f"Your code is {code}\n\nIt expires in 15 minutes. If you didn't ask for this, you can ignore this email.",
                        settings, email)
    assert_recipient(msg["To"], [email])
    (sender or gmail_send)(msg, settings)
    store.log_event("verify_code", status=_key(email)[:16])


def check(email: str, code: str, store: Store) -> None:
    """Accept the code (raises VerifyError otherwise) and mark the inbox verified for 24 hours."""
    rec = store.get_cache("verify", _key(email))
    if not rec or time.time() > rec.get("expires", 0):
        raise VerifyError("That code has expired. Ask for a new one.")
    if rec.get("tries", 0) >= MAX_TRIES:
        raise VerifyError("Too many wrong tries. Ask for a new code.")
    if not secrets.compare_digest(rec["hash"], _hash(email, code)):
        rec["tries"] = rec.get("tries", 0) + 1
        store.set_cache("verify", _key(email), rec)
        raise VerifyError("That code is not right.")
    rec.update(expires=0, verified_until=time.time() + VERIFIED_TTL_S)  # the code is single-use
    store.set_cache("verify", _key(email), rec)


def is_verified(email: str, store: Store) -> bool:
    rec = store.get_cache("verify", _key(email))
    return bool(rec) and rec.get("verified_until", 0) > time.time()
