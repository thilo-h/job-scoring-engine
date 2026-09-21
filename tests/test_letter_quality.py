"""The deterministic letter rules.

No model is involved anywhere in this file. Each finding maps to a numbered
section of ``assets/example/letter_style_guide.md`` — if a rule changes there,
a test here should change with it.
"""

from __future__ import annotations

from datetime import date

import pytest

from src.agent import letter_quality as lq

TODAY = date(2026, 9, 21)


def structure(**overrides) -> dict:
    """A minimal letter structure, overridable field by field."""
    base = {
        "company_block": [],
        "subject": "Application: Data Engineer",
        "repo_url": None,
        "date_line": "Musterstadt, 4 May 2026",
        "salutation": "Dear Hiring Team,",
        "intro_paragraph": "I am applying for the Data Engineer role.",
        "middle_paragraphs": ["At Muster Analytics I built ingestion pipelines."],
        "sections": [],
        "closing_paragraph": "I am available immediately at 80%.",
        "signoff": "Kind regards,",
        "name_line": "Alex Muster",
    }
    base.update(overrides)
    return base


def codes(findings) -> set[str]:
    return {f.code for f in findings}


def check(struct, *, stations=(), ad_text="", ad_pensum=None, language="en"):
    return lq.check_letter(struct, language=language, stations=list(stations),
                           today=TODAY, ad_text=ad_text, ad_pensum=ad_pensum)


# --- normalisation (§6, §7): safe, applied automatically -------------------


def test_missing_space_after_punctuation_is_repaired():
    out = lq.normalize_text("I built pipelines.And then I left.", "en")
    assert "pipelines. And" in out


@pytest.mark.parametrize("raw,expected_fragment", [
    ("40-60 %", "40–60"),          # en dash for numeric ranges
    ("2022-2023", "2022–2023"),
])
def test_numeric_ranges_get_an_en_dash(raw, expected_fragment):
    assert expected_fragment in lq.normalize_text(raw, "de")


def test_percent_spacing_follows_the_language():
    """German and Spanish get a non-breaking space; English gets none."""
    assert "80\u00a0%" in lq.normalize_text("80% Pensum", "de")
    assert "80%" in lq.normalize_text("80 % workload", "en")


def test_swiss_spelling_is_opt_in():
    """Off by default — only a profile that asks for it gets ss for ß."""
    assert "ß" in lq.normalize_text("Grüßen", "de")
    assert "ß" not in lq.normalize_text("Grüßen", "de", swiss_spelling=True)
    assert "Grüssen" in lq.normalize_text("Grüßen", "de", swiss_spelling=True)


def test_swiss_spelling_only_applies_to_german():
    """An English letter keeps whatever it has — the rule is about German."""
    assert lq.normalize_text("straße", "en", swiss_spelling=True) == "straße"


def test_normalize_is_idempotent():
    """It runs on every save, so applying it twice must not keep changing the text."""
    once = lq.normalize_text("40-60 % — done.And more", "de", swiss_spelling=True)
    assert lq.normalize_text(once, "de", swiss_spelling=True) == once


def test_hyphen_is_restored_from_a_source():
    """A lost hyphen is repaired only when a source text proves it belonged there."""
    out = lq.restore_hyphens("We worked on the roomprogramme", ("room-programme is the artefact",))
    assert "room-programme" in out


def test_normalize_leaves_clean_text_alone():
    clean = "I built pipelines at Muster Analytics — three of them."
    assert lq.normalize_text(clean, "en") == clean


# --- CV stations -----------------------------------------------------------


def test_stations_parse_from_the_example_cv(cv_markdown):
    stations = lq.parse_cv_stations(cv_markdown)
    assert len(stations) >= 5
    assert any(s.ongoing for s in stations)
    assert any(s.end is not None for s in stations)


def test_acronym_alias_is_case_sensitive(cv_markdown):
    """"MUAS" means the school; a lowercase "muas" in prose does not."""
    stations = lq.parse_cv_stations(cv_markdown)
    acronyms = [a for s in stations for a in s.aliases if a[0].isupper() and a[1]]
    assert ("MUAS", True) in acronyms


def test_legal_form_is_stripped_from_aliases(cv_markdown):
    stations = lq.parse_cv_stations(cv_markdown)
    aliases = {a for s in stations for a, _ in s.aliases}
    assert "Muster Analytics" in aliases
    assert not any(a.endswith(" AG") or a.endswith(" GmbH") for a in aliases)


def test_timeline_brief_marks_finished_and_ongoing(cv_markdown):
    brief = lq.cv_timeline_brief(lq.parse_cv_stations(cv_markdown), TODAY)
    assert "LAUFEND" in brief
    assert "ABGESCHLOSSEN" in brief


def test_missing_end_date_is_reported_and_not_repairable(cv_markdown):
    """Only the author can fix their own CV, so this finding is not for a model."""
    stations = lq.parse_cv_stations(cv_markdown)
    stations.append(lq.Station(name="Somewhere", section="experience", missing_end=True))
    findings = check(structure(), stations=stations)
    missing = [f for f in findings if f.code == "cv_end_date"]
    assert missing and all(not f.repairable for f in missing)


# --- length (§2) -----------------------------------------------------------


def test_short_letter_is_reported():
    assert "word_count" in codes(check(structure()))


def test_letter_inside_the_corridor_has_no_length_finding():
    body = " ".join(["Wort"] * 500)
    findings = check(structure(middle_paragraphs=[body]))
    assert "word_count" not in codes(findings)


def test_overlong_paragraph_is_reported():
    long_para = " ".join(["Wort"] * 260)          # well past seven lines
    findings = check(structure(middle_paragraphs=[long_para, long_para]))
    assert "paragraph_too_long" in codes(findings)


# --- workload (§3) ---------------------------------------------------------


def test_closing_must_name_the_posting_workload():
    findings = check(structure(closing_paragraph="I am available immediately."),
                     ad_pensum="80–100")
    assert "pensum" in codes(findings)


def test_correct_workload_range_passes():
    findings = check(structure(closing_paragraph="I am available immediately at 80–100%."),
                     ad_pensum="80–100")
    assert "pensum" not in codes(findings)


def test_a_different_workload_than_the_posting_is_reported():
    findings = check(structure(closing_paragraph="I am available at 50%."),
                     ad_pensum="80–100")
    assert "pensum" in codes(findings)


# --- redundancy (§10) ------------------------------------------------------


def test_summary_sentence_is_reported():
    findings = check(structure(middle_paragraphs=[
        "At Muster Analytics I built pipelines.",
        "Both experiences reflect the responsibilities listed in this role.",
    ]))
    assert "summary_sentence" in codes(findings)


def test_station_mentioned_in_two_paragraphs_is_reported(cv_markdown):
    stations = lq.parse_cv_stations(cv_markdown)
    findings = check(structure(middle_paragraphs=[
        "At Muster Analytics I built the ingestion pipelines.",
        "Muster Analytics also taught me how to run an evaluation harness.",
    ]), stations=stations)
    assert "station_repeated" in codes(findings)


def test_sentence_starting_with_a_conjunction_is_reported():
    findings = check(structure(middle_paragraphs=[
        "I built pipelines at Muster Analytics. And I ran the eval harness."
    ]))
    assert "fragment" in codes(findings)


def test_enclosure_listing_the_letter_itself_is_reported():
    findings = check(structure(
        closing_paragraph="My cover letter and CV are attached. Available at 80%."))
    assert "attachments" in codes(findings)


# --- repository allow-list (§11) ------------------------------------------


def test_repo_url_outside_the_allow_list_is_discarded():
    url, finding = lq.validate_repo_url("https://github.com/someone/unlisted",
                                        ["https://github.com/example/allowed"])
    assert url is None
    assert finding is not None


def test_allowed_repo_url_survives():
    allowed = "https://github.com/example/allowed"
    url, finding = lq.validate_repo_url(allowed, [allowed])
    assert url == allowed
    assert finding is None


def test_no_repo_url_is_not_a_finding():
    url, finding = lq.validate_repo_url(None, ["https://github.com/example/allowed"])
    assert url is None and finding is None


# --- page fit --------------------------------------------------------------


def test_page_overflow_becomes_an_informational_finding():
    """Overflow is information for the author, never an instruction to cut."""
    finding = lq.page_fit_finding(pages=2, overflow_lines=6)
    assert finding is not None
    assert not finding.repairable


def test_single_page_is_no_finding():
    assert lq.page_fit_finding(pages=1, overflow_lines=0) is None


# --- findings are serialisable --------------------------------------------


def test_findings_round_trip_to_dicts():
    """They are stored as JSON in the DB, so every field has to survive."""
    for f in check(structure()):
        d = f.to_dict()
        assert set(d) >= {"code", "message", "repairable"}
        assert isinstance(d["message"], str) and d["message"]
