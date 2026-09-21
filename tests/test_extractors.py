"""The two regex extractors.

Both are deliberately conservative: they return ``None`` rather than guess, and
the negative cases below are the point of them. A false 100% workload or a
false "10 years required" quietly buries or surfaces the wrong postings, and
nobody notices because the number looks plausible.
"""

from __future__ import annotations

import pytest

from src.experience_extractor import extract_for_job as experience_for_job
from src.experience_extractor import extract_min_years
from src.workload_extractor import extract_for_job as workload_for_job
from src.workload_extractor import extract_workload, extract_workload_phrase

# --- workload: found -------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("Pensum: 80%", 80),
    ("Wir suchen eine Person für 60-80%", 60),
    ("60–80 %", 60),
    ("Beschäftigungsgrad 40-60%", 40),
    ("Vollzeit", 100),
    ("Werkstudent", 40),
])
def test_workload_found(text, expected):
    assert extract_workload(text) == expected


def test_workload_range_returns_the_minimum():
    """The user's question is "is the floor low enough for me?"."""
    assert extract_workload("Pensum 60-100%") == 60


def test_workload_from_title():
    """extract_for_job reads the title first, then the description."""
    assert workload_for_job(title="Data Engineer, Pensum 80%", description="") == 80
    assert workload_for_job(title="Data Engineer", description="Pensum: 60%") == 60


def test_bare_percentage_in_a_title_is_not_read_as_workload():
    """Documents the conservative choice: "(80%)" has no keyword next to it, so
    it is skipped. Defensible — the same shape appears as "100% remote" — but it
    does mean a common title form yields nothing."""
    assert workload_for_job(title="Data Engineer (80%)", description="") is None


# --- workload: deliberately not found --------------------------------------


@pytest.mark.parametrize("text", [
    "100% remote work",
    "We grew revenue by 20% last year",
    "100% committed to diversity",
    "Our tests have 95% coverage",
    "",
    "No numbers here at all",
])
def test_workload_skips_ambiguous_percentages(text):
    """A standalone percentage is usually not a workload. Guessing is worse than NULL."""
    assert extract_workload(text) is None


def test_workload_phrase_keeps_the_range_as_written():
    """The letter has to quote the posting's own figure, not a normalised one."""
    assert extract_workload_phrase("Data Engineer", "Pensum 80-100%") == "80–100"
    assert extract_workload_phrase("Data Engineer", "Pensum: 80%") == "80"


def test_workload_phrase_is_none_without_a_signal():
    assert extract_workload_phrase("Data Engineer", "Full time position") is None


# --- experience: found -----------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("5+ years of experience", 5),
    ("mindestens 3 Jahre Berufserfahrung", 3),
    ("at least 2 years of relevant experience", 2),
    ("mínimo 2 años de experiencia", 2),
    ("3-5 years of experience required", 3),
    ("We expect 7 years of experience", 7),
])
def test_experience_found(text, expected):
    assert extract_min_years(text) == expected


def test_experience_range_returns_the_floor():
    assert extract_min_years("3-5 years of experience") == 3


def test_experience_handles_escaped_markdown():
    """Scraped text sometimes arrives as "5\\+ years" — the penalty must still fire."""
    assert extract_min_years(r"5\+ years of experience") == 5


# --- experience: deliberately not found ------------------------------------


@pytest.mark.parametrize("text", [
    "Founded in 2019",
    "We are a team of 12 people",
    "Our product has 5 modules",
    "Salary 80000 per year",
    "",
    "Experience with Python and SQL",   # experience, but no quantity
])
def test_experience_needs_a_year_anchor(text):
    """A bare number near no time unit is not an experience requirement."""
    assert extract_min_years(text) is None


def test_experience_from_job_reads_title_and_description():
    assert experience_for_job(title="5+ years of experience required",
                              description="") == 5
    assert experience_for_job(title="Data Engineer",
                              description="mindestens 3 Jahre Berufserfahrung") == 3


# --- phrasings that used to be missed -------------------------------------
# These were xfail until the patterns were widened. The guard cases below them
# are the reason the fix is narrow: the adjective slot is only allowed after an
# explicit of/de/di, and the workload keyword window is only eight characters.


@pytest.mark.parametrize("text,expected", [
    ("7 years of professional experience", 7),
    ("5+ years of relevant experience", 5),
    ("3 years of hands-on experience", 3),
    ("3-5 years of practical experience", 3),
    ("2 años de experiencia laboral previa", 2),
    ("mindestens 3 Jahre einschlägige Berufserfahrung", 3),
])
def test_experience_with_words_between_connector_and_keyword(text, expected):
    assert extract_min_years(text) == expected


@pytest.mark.parametrize("text", [
    "We opened 5 years ago, and experience shows this works",
    "Founded 12 years ago",
    "A company with 30 years of experience",          # firm age, not a requirement
    "consultoría con más de 30 años de experiencia",
])
def test_widened_pattern_does_not_invent_requirements(text):
    """The connector requirement is what keeps "5 years ago, … experience" out,
    and MAX_PLAUSIBLE_YEARS keeps company-age boasts out."""
    assert extract_min_years(text) is None


@pytest.mark.parametrize("text,expected", [
    ("Arbeitszeit 100%", 100),
    ("Arbeitszeit: 80%", 80),
    ("Anstellungsgrad 80%", 80),
    ("part-time 60%", 60),
])
def test_workload_keyword_coverage(text, expected):
    assert extract_workload(text) == expected


def test_explicit_figure_beats_the_teilzeit_default():
    """This was the one place the module returned a wrong number instead of None:
    the Teilzeit fallback's hard-coded 60 overrode the figure standing next to it."""
    assert extract_workload("Teilzeit 50 %") == 50
    assert extract_workload("Teilzeit, 50%") == 50


def test_the_default_still_applies_without_a_figure():
    assert extract_workload("Teilzeit") == 60
    assert extract_workload("Vollzeit") == 100


def test_a_distant_percentage_does_not_hijack_the_keyword():
    """Eight characters between keyword and number. "Teilzeit möglich. 20 % der
    Zeit Reisen" must fall back to the Teilzeit default, not report 20."""
    assert extract_workload("Teilzeit möglich. 20 % der Zeit Reisen") == 60
