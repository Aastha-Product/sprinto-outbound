"""All safety checks for emails. Pure functions, easy to test (see tests/test_guards.py).

`validate_draft()` returns a list of problems (empty list = the draft is safe to send).
`assert_recipient()` is the last line of defence before Gmail: the To address must be an approved inbox
(OWNER_EMAIL or TEST_RECIPIENTS, see Settings.recipients).
"""

from __future__ import annotations

import re

from outbound.evidence import canonical_url
from outbound.models import EmailDraft

MAX_BODY_WORDS = 120
MAX_SUBJECT_WORDS = 7
ALLOWED_LINK_DOMAINS = ("sprinto.com",)

BANNED_PHRASES = [
    "synergy", "synergies", "revolutionize", "revolutionise", "game-changer", "game changer", "cutting-edge",
    "cutting edge", "hope this email finds you", "hope this finds you", "hope you're doing well",
    "hope you are doing well", "i came across", "i saw your post", "touch base", "circle back", "leverage",
    "best-in-class", "world-class", "disrupt",
]
PLACEHOLDER_RE = re.compile(r"\[[^\]]{1,40}\]|\{[^}]{1,40}\}|<[A-Za-z _]{1,30}>|\bXXX+\b|\bTODO\b|\bFIRST[_ ]NAME\b|\bCOMPANY[_ ]NAME\b", re.I)
URL_RE = re.compile(r"https?://[^\s)>\]]+|www\.[^\s)>\]]+", re.I)
BARE_DOMAIN_RE = re.compile(r"\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|io|ai|co|net|org|in|app|dev|so|tech|xyz)\b", re.I)
MARKDOWN_RE = re.compile(r"\*\*|__|^\s*#|^\s*[-*•]\s", re.M)


class RecipientError(RuntimeError):
    """Raised when anything tries to send to an address that is not an approved test inbox."""


def word_count(text: str) -> int:
    return len(re.findall(r"\b[\w'’-]+\b", text))


def find_links(text: str) -> list[str]:
    """Every URL or bare domain in the text that is not on the allow-list."""
    found = URL_RE.findall(text) + BARE_DOMAIN_RE.findall(text)
    bad = []
    for item in found:
        host = re.sub(r"^https?://", "", item.lower()).split("/")[0].removeprefix("www.")
        if not any(host == d or host.endswith("." + d) for d in ALLOWED_LINK_DOMAINS):
            bad.append(item)
    return bad


def validate_draft(draft: EmailDraft, first_name: str, evidence_urls: list[str]) -> list[str]:
    """Check every email rule from CLAUDE.md. Returns human-readable problems; [] means OK."""
    problems: list[str] = []
    body, subject = draft.body.strip(), draft.subject.strip()

    if not subject:
        problems.append("subject is empty")
    elif word_count(subject) > MAX_SUBJECT_WORDS:
        problems.append(f"subject has {word_count(subject)} words (max {MAX_SUBJECT_WORDS})")
    if word_count(body) > MAX_BODY_WORDS:
        problems.append(f"body has {word_count(body)} words (max {MAX_BODY_WORDS})")
    if not body:
        problems.append("body is empty")

    first_line = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
    if not first_name or not re.match(rf"^(hi|hello|hey)?\s*{re.escape(first_name)}\b", first_line, re.I):
        problems.append(f"first line must greet the person by first name ('Hi {first_name},')")
    elif not re.fullmatch(rf"(hi|hello|hey)?\s*{re.escape(first_name)}\s*[,!]?", first_line, re.I):
        problems.append(f"put the greeting 'Hi {first_name},' on its own line, then a blank line")

    text = f"{subject}\n{body}".lower()
    for phrase in BANNED_PHRASES:
        if re.search(rf"\b{re.escape(phrase)}\b", text):
            problems.append(f"banned phrase: '{phrase}'")

    known = {canonical_url(u) for u in evidence_urls}
    if not draft.hook_evidence_url or canonical_url(draft.hook_evidence_url) not in known:
        problems.append("hook_evidence_url is not one of the verified evidence sources")

    links = find_links(f"{subject}\n{body}")
    if links:
        problems.append(f"unexpected link(s): {', '.join(links[:3])}")
    if m := PLACEHOLDER_RE.search(f"{subject}\n{body}"):
        problems.append(f"placeholder left in text: '{m.group(0)}'")
    if MARKDOWN_RE.search(body):
        problems.append("body must be plain text (no bullets, bold or headings)")
    if "?" not in body:
        problems.append("body needs a question (pain point or call to action)")
    return problems


def assert_recipient(to_address: str, allowed: list[str]) -> None:
    """Refuse to send anywhere except the approved inboxes (OWNER_EMAIL + TEST_RECIPIENTS)."""
    if to_address.strip().lower() not in {a.strip().lower() for a in allowed if a.strip()}:
        raise RecipientError("Refusing to send: the recipient is not an approved test inbox.")
