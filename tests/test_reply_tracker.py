"""The reply tracker's pure parts: header handling, correlation, classification.

IMAP itself is not exercised — that needs a server. Everything around it is:
MIME decoding, the two correlation strategies, the confidence threshold, and the
mapping from a classification to a status change. Those are where the bugs live.
"""

from __future__ import annotations

import email
from email.message import EmailMessage

import pytest

from src.agent import reply_tracker as rt
from src.llm import MockAnthropic, scripted_tool_response
from tests.conftest import make_job

# --- header decoding -------------------------------------------------------


def test_decodes_an_encoded_word_subject():
    """Recruiting systems send RFC 2047 headers; raw bytes in the UI look broken."""
    assert rt._decode("=?utf-8?q?Einladung_zum_Gespr=C3=A4ch?=") == "Einladung zum Gespräch"


def test_decode_handles_none_and_empty():
    assert rt._decode(None) == ""
    assert rt._decode("") == ""


def test_decode_survives_an_unknown_charset():
    """A bad charset label must not take down the whole poll."""
    assert isinstance(rt._decode("=?not-a-charset?q?hello?="), str)


@pytest.mark.parametrize("raw,expected", [
    ("Jane Doe <jane@example.com>", "jane@example.com"),
    ("jane@example.com", "jane@example.com"),
    ("JANE@EXAMPLE.COM", "jane@example.com"),
    ("Recruiting <a@example.com>, Other <b@example.com>", "a@example.com"),
])
def test_first_address_is_extracted_and_lowercased(raw, expected):
    assert rt._first_address(raw) == expected


def test_first_address_of_garbage_is_none():
    assert rt._first_address("not an address") is None
    assert rt._first_address(None) is None


# --- message ids -----------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("<abc@example.com>", "<abc@example.com>"),
    ("  <abc@example.com>  ", "<abc@example.com>"),
    ("abc@example.com", "abc@example.com"),
])
def test_message_id_normalisation(raw, expected):
    assert rt._normalize_msgid(raw) == expected


def test_references_header_yields_every_id():
    refs = rt._parse_references("<a@example.com> <b@example.com>")
    assert refs == ["<a@example.com>", "<b@example.com>"]


def test_empty_references_is_an_empty_list():
    assert rt._parse_references(None) == []


# --- body extraction -------------------------------------------------------


def test_plain_text_part_is_preferred_over_html():
    msg = EmailMessage()
    msg["Subject"] = "Re: Application"
    msg.set_content("The plain text version.")
    msg.add_alternative("<p>The HTML version.</p>", subtype="html")
    body = rt._extract_body(email.message_from_bytes(bytes(msg)))
    assert "plain text" in body
    assert "<p>" not in body


def test_html_only_mail_is_stripped_of_tags():
    msg = EmailMessage()
    msg["Subject"] = "Re: Application"
    msg.add_alternative("<p>Hello <b>there</b></p>", subtype="html")
    body = rt._extract_body(email.message_from_bytes(bytes(msg)))
    assert "Hello" in body
    assert "<b>" not in body


def test_body_is_clipped_to_the_excerpt_length():
    """The excerpt cap is what keeps classification cost predictable."""
    msg = EmailMessage()
    msg.set_content("x" * (rt.BODY_EXCERPT_CHARS * 3))
    body = rt._extract_body(email.message_from_bytes(bytes(msg)))
    assert len(body) <= rt.BODY_EXCERPT_CHARS


# --- correlation -----------------------------------------------------------


def test_matches_on_message_id_threading(db):
    db.upsert_job(make_job())
    job_id = db.get_jobs(limit=1)[0]["id"]
    db.mark_sent(job_id, eml_or_smtp="manual", message_id="<sent-1@example.com>",
                 recipient_email="jobs@example.com", recipient_domain="example.com")
    job, method = rt._match_reply(db, {
        "in_reply_to": "<sent-1@example.com>",
        "references": [],
        "from_email": "someone-else@other.example",
    })
    assert job is not None and job["id"] == job_id
    assert method == "message_id"


def test_falls_back_to_the_sender_domain(db):
    db.upsert_job(make_job())
    job_id = db.get_jobs(limit=1)[0]["id"]
    db.mark_sent(job_id, eml_or_smtp="manual", message_id="<sent-2@example.com>",
                 recipient_email="jobs@example.com", recipient_domain="example.com")
    job, method = rt._match_reply(db, {
        "in_reply_to": None, "references": [], "from_email": "recruiter@example.com",
    })
    assert job is not None and job["id"] == job_id
    assert method == "domain"


@pytest.mark.parametrize("sender", [
    "someone@gmail.com", "someone@outlook.com", "someone@bluewin.ch", "someone@web.de",
])
def test_freemail_domains_are_not_used_for_matching(db, sender):
    """Matching on gmail.com would tie every personal mail to a random application."""
    db.upsert_job(make_job())
    job_id = db.get_jobs(limit=1)[0]["id"]
    db.mark_sent(job_id, eml_or_smtp="manual", recipient_email="jobs@gmail.com",
                 recipient_domain="gmail.com")
    job, method = rt._match_reply(db, {
        "in_reply_to": None, "references": [], "from_email": sender,
    })
    assert job is None


def test_no_match_returns_nothing(db):
    job, method = rt._match_reply(db, {
        "in_reply_to": None, "references": [], "from_email": "stranger@nowhere.example",
    })
    assert job is None


# --- classification --------------------------------------------------------


def test_classification_is_parsed_from_the_tool_call():
    client = MockAnthropic()
    result = rt._classify(client, "Invitation to interview", "We would like to talk.")
    assert result["classification"] == "interview"
    assert result["confidence"] > rt.AUTO_BUMP_CONFIDENCE


def test_low_confidence_stays_below_the_auto_bump_threshold():
    """Mid-confidence replies are recorded for review, never acted on."""
    client = MockAnthropic(scripted=[scripted_tool_response(
        "classify_reply", {"classification": "needs_review", "confidence": 0.44,
                           "summary": "Ambiguous."})])
    result = rt._classify(client, "Re: Application", "Some question.")
    assert result["confidence"] < rt.AUTO_BUMP_CONFIDENCE


def test_a_failing_classification_returns_none_not_an_exception():
    """One malformed reply must not abort the whole poll."""
    class Boom:
        class messages:
            @staticmethod
            def create(**kwargs):
                raise RuntimeError("API down")
    assert rt._classify(Boom(), "subject", "body") is None


@pytest.mark.parametrize("classification,expected", [
    ("interview", "interview"),
    ("rejected", "rejected"),
    ("offer", "offer"),
    ("acknowledgement", None),
    ("needs_review", None),
    ("unrelated", None),
])
def test_only_decisive_classifications_map_to_a_status(classification, expected):
    """An ATS auto-acknowledgement is not progress and must not move the card."""
    assert rt.CLASSIFICATION_TO_STATUS[classification] == expected


def test_every_classification_in_the_schema_has_a_mapping():
    """A new enum value without a mapping would raise a KeyError mid-poll."""
    client = MockAnthropic()
    rt._classify(client, "s", "b")
    schema = client.custom_tools()[0]["input_schema"]["properties"]
    for value in schema["classification"]["enum"]:
        assert value in rt.CLASSIFICATION_TO_STATUS


# --- settings -------------------------------------------------------------


def test_missing_imap_settings_raise_a_clear_error(monkeypatch, tmp_path):
    monkeypatch.setattr(rt, "ROOT", tmp_path)
    for var in ("IMAP_HOST", "IMAP_USER", "IMAP_PASS"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(RuntimeError, match="IMAP_HOST"):
        rt._imap_settings()


def test_auto_bump_defaults_to_on_but_is_switchable(monkeypatch, tmp_path):
    monkeypatch.setattr(rt, "ROOT", tmp_path)
    monkeypatch.setenv("IMAP_HOST", "imap.example.com")
    monkeypatch.setenv("IMAP_USER", "you@example.com")
    monkeypatch.setenv("IMAP_PASS", "secret")
    monkeypatch.setenv("REPLY_AUTO_BUMP", "false")
    assert rt._imap_settings()["auto_bump"] is False
    monkeypatch.setenv("REPLY_AUTO_BUMP", "true")
    assert rt._imap_settings()["auto_bump"] is True
