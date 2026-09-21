"""Markdown ⇄ structure, and the review pass over a saved letter.

The round-trip matters more than it looks: the applicant edits markdown in the
browser, and the DOCX renderer works from the parsed structure. If the parser
loses a paragraph, the rendered letter quietly differs from what they saw.
"""

from __future__ import annotations

import pytest

from src.agent.letter_review import parse_markdown_to_structure, review_markdown

LETTER = """Musterstadt, 4 May 2026

**Application: Data Engineer**

Dear Hiring Team,

I am applying for the Data Engineer role at Sample Energy.

At Muster Analytics I own the evaluation harness for a retrieval service.

I am available immediately at 80%.

Kind regards,
Alex Muster
"""


def test_parses_the_head_fields():
    s = parse_markdown_to_structure(LETTER)
    assert s["date_line"] == "Musterstadt, 4 May 2026"
    assert s["subject"] == "Application: Data Engineer"
    assert s["salutation"] == "Dear Hiring Team,"
    assert s["signoff"].rstrip(",") == "Kind regards"
    assert s["name_line"] == "Alex Muster"


def test_paragraphs_stay_separate():
    """Collapsing blank-line-separated blocks is the bug this guards against."""
    s = parse_markdown_to_structure(LETTER)
    assert s["intro_paragraph"].startswith("I am applying")
    assert len(s["middle_paragraphs"]) == 1
    assert s["closing_paragraph"].startswith("I am available")


def test_company_block_is_read_from_blockquote_lines():
    letter = "> Sample Energy\n> Musterstadt\n\n" + LETTER
    s = parse_markdown_to_structure(letter)
    assert s["company_block"] == ["Sample Energy", "Musterstadt"]


def test_letter_without_subject_still_parses():
    """Older saved letters have no bold subject line."""
    letter = LETTER.replace("**Application: Data Engineer**\n\n", "")
    s = parse_markdown_to_structure(letter)
    assert s["subject"] == ""
    assert s["salutation"] == "Dear Hiring Team,"


def test_repo_line_is_extracted_and_removed_from_the_body():
    letter = LETTER.rstrip() + "\n\nProject repository: https://github.com/example/repo\n"
    s = parse_markdown_to_structure(letter)
    assert s["repo_url"] == "https://github.com/example/repo"
    assert "github.com" not in s["closing_paragraph"]


def test_sections_with_bullets_are_parsed():
    letter = """Musterstadt, 4 May 2026

Dear Hiring Team,

I am applying.

Why I fit:

- I built ingestion pipelines
- I own an evaluation harness

Available at 80%.

Kind regards,
Alex Muster
"""
    s = parse_markdown_to_structure(letter)
    assert s["sections"]
    assert s["sections"][0]["heading"] == "Why I fit"
    assert len(s["sections"][0]["bullets"]) == 2


def test_empty_markdown_is_rejected():
    with pytest.raises(ValueError):
        parse_markdown_to_structure("   \n\n  ")


def test_crlf_input_parses_the_same():
    """A letter pasted from Word arrives with Windows line endings."""
    assert (parse_markdown_to_structure(LETTER.replace("\n", "\r\n"))
            == parse_markdown_to_structure(LETTER))


def test_custom_sender_name_detects_the_trailing_block():
    letter = LETTER.replace("Alex Muster", "Jamie Beispiel")
    s = parse_markdown_to_structure(letter, sender_name="Jamie Beispiel")
    assert s["name_line"] == "Jamie Beispiel"


# --- the review pass -------------------------------------------------------


def test_review_returns_serialisable_findings(config):
    """No model call, no LibreOffice — page measurement is opt-out for speed."""
    findings = review_markdown(
        LETTER,
        job={"title": "Data Engineer", "description": "Pensum 80-100%"},
        config=config,
        measure_page=False,
    )
    assert isinstance(findings, list)
    assert all(isinstance(f, dict) and "code" in f for f in findings)


def test_review_flags_the_wrong_workload(config):
    findings = review_markdown(
        LETTER,
        job={"title": "Data Engineer", "description": "Pensum 40-60%"},
        config=config,
        measure_page=False,
    )
    assert any(f["code"] == "pensum" for f in findings)


def test_review_is_stable_across_calls(config):
    """Saving twice must not change the findings — they are not a model's opinion."""
    args = dict(job={"title": "Data Engineer", "description": "Pensum 80-100%"},
                config=config, measure_page=False)
    assert review_markdown(LETTER, **args) == review_markdown(LETTER, **args)
