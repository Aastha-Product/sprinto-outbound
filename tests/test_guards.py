"""One test per email rule in guards.py, plus the recipient lock and send-step guardrails."""

import pytest

from outbound.config import Settings
from outbound.guards import RecipientError, assert_recipient, validate_draft
from outbound.models import EmailDraft
from outbound.send import send_email
from outbound.storage import MemoryStore

URL = "https://acme.io/pricing"
GOOD_BODY = ("Hi Alex,\n\nYour pricing page lists SOC 2 Type II, with ISO 27001 still in progress. "
             "Running two frameworks often means the same evidence gets collected twice by engineers. "
             "Is that eating into roadmap time for your team? Sprinto keeps both frameworks on one set of "
             "automated controls. Worth a 15-minute chat next week?")


def draft(**kw) -> EmailDraft:
    base = dict(subject="ISO 27001 alongside SOC 2", body=GOOD_BODY, hook="ISO in progress", hook_evidence_url=URL,
                pain_point_used="duplicate evidence", reasoning="r")
    base.update(kw)
    return EmailDraft(**base)


def problems(**kw):
    return validate_draft(draft(**kw), "Alex", [URL])


def test_good_draft_passes():
    assert problems() == []


def test_body_word_limit():
    assert any("body has" in p for p in problems(body=GOOD_BODY + " more" * 80))


def test_subject_word_limit_and_empty():
    assert any("subject has" in p for p in problems(subject="one two three four five six seven eight"))
    assert "subject is empty" in problems(subject=" ")


def test_first_name_greeting_required():
    assert any("first name" in p for p in problems(body=GOOD_BODY.replace("Hi Alex,", "Hello there,")))
    assert problems(body=GOOD_BODY.replace("Hi Alex,", "Alex,")) == []


def test_greeting_must_be_on_its_own_line():
    run_on = GOOD_BODY.replace("Hi Alex,\n\n", "Hi Alex, ")
    assert any("own line" in p for p in problems(body=run_on))


@pytest.mark.parametrize("phrase", ["synergy", "I hope this email finds you well", "revolutionize", "Let's touch base"])
def test_banned_phrases(phrase):
    assert any("banned phrase" in p for p in problems(body=GOOD_BODY + " " + phrase + "?"))


def test_hook_must_come_from_verified_evidence():
    assert any("hook_evidence_url" in p for p in problems(hook_evidence_url="https://made-up.example/news"))
    assert problems(hook_evidence_url="http://www.acme.io/pricing/") == []  # same page, different spelling


def test_no_unexpected_links_but_sprinto_is_ok():
    assert any("unexpected link" in p for p in problems(body=GOOD_BODY + " See https://calendly.com/x"))
    assert any("unexpected link" in p for p in problems(body=GOOD_BODY + " More at acme.io"))
    assert problems(body=GOOD_BODY + " More at sprinto.com") == []


@pytest.mark.parametrize("ph", ["[Company]", "{first_name}", "<COMPANY NAME>", "XXX"])
def test_placeholders_rejected(ph):
    assert any("placeholder" in p for p in problems(body=GOOD_BODY + " " + ph))


def test_plain_text_only():
    assert any("plain text" in p for p in problems(body=GOOD_BODY + "\n- a bullet"))
    assert any("plain text" in p for p in problems(body=GOOD_BODY.replace("SOC 2", "**SOC 2**")))


def test_needs_a_question():
    assert any("question" in p for p in problems(body=GOOD_BODY.replace("?", ".")))


# ---------------------------------------------------------------- recipient lock + send step
def test_assert_recipient_only_allows_approved_inboxes():
    assert_recipient("Me@Example.com", ["me@example.com"])
    assert_recipient("prof@school.edu", ["me@example.com", "prof@school.edu"])
    with pytest.raises(RecipientError):
        assert_recipient("alex@acme.io", ["me@example.com"])
    with pytest.raises(RecipientError):
        assert_recipient("me@example.com", [])


def sset(**kw):
    return Settings(_env_file=None, owner_email="me@example.com", **kw)


def send(store, settings, domain="acme.io", flagged=False, requested=True, sent_box=None):
    sent_box = sent_box if sent_box is not None else []

    def fake_sender(msg, s):
        sent_box.append(msg)
        return f"id-{len(sent_box)}"

    return send_email("run1", domain, "Subject", "Body?", "alex@acme.io", flagged, settings, store, requested, sender=fake_sender)


def test_dry_run_by_default_and_without_send_enabled():
    store = MemoryStore()
    assert send(store, sset(send_enabled=True), requested=False).status == "dry_run"
    assert send(store, sset(send_enabled=False), requested=True).status == "dry_run"


def test_flagged_draft_is_never_sent():
    assert send(MemoryStore(), sset(send_enabled=True), flagged=True).status == "blocked"


def test_sends_only_to_owner_with_test_prefix():
    box = []
    r = send(MemoryStore(), sset(send_enabled=True), sent_box=box)
    assert r.status == "sent" and r.to == "me@example.com" and r.would_have_gone_to == "alex@acme.io"
    assert box[0]["To"] == "me@example.com"
    assert box[0]["Subject"].startswith("[TEST → alex@acme.io]")


def test_never_twice_per_domain_and_run_cap():
    store, s = MemoryStore(), sset(send_enabled=True, max_emails_per_run=2)
    assert send(store, s, "a.io").status == "sent"
    assert send(store, s, "a.io").status == "skipped"
    assert send(store, s, "b.io").status == "sent"
    assert send(store, s, "c.io").status == "blocked"


def test_daily_cap():
    store, s = MemoryStore(), sset(send_enabled=True, max_emails_per_run=10, max_emails_per_day=1)
    assert send(store, s, "a.io").status == "sent"
    assert "MAX_EMAILS_PER_DAY" in send(store, s, "b.io").reason


def test_test_recipients_list_and_index():
    s = sset(send_enabled=True, test_recipients="prof@school.edu, not-an-email, ME@example.com")
    assert s.recipients() == ["me@example.com", "prof@school.edu"]  # owner first, invalid + duplicate dropped
    box = []

    def fake_sender(msg, _):
        box.append(msg)
        return "id"

    r = send_email("run1", "acme.io", "Subject", "Body?", "alex@acme.io", False, s, MemoryStore(), True, sender=fake_sender, recipient=1)
    assert r.status == "sent" and box[0]["To"] == "prof@school.edu"
    r = send_email("run2", "acme.io", "Subject", "Body?", "alex@acme.io", False, s, MemoryStore(), True, sender=fake_sender, recipient=7)
    assert r.status == "blocked" and len(box) == 1  # an index outside the list never sends
