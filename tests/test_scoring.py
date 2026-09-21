"""The deterministic scorer.

These tests pin the properties the scorer is supposed to have, not the exact
numbers it currently produces. A weight change should not break the suite; a
change in the *ordering* of a good and a bad match should.
"""

from __future__ import annotations

import pytest

from src.scoring import JobScorer
from tests.conftest import make_job


@pytest.fixture
def scorer(config) -> JobScorer:
    return JobScorer(config)


# --- weights ---------------------------------------------------------------


def test_weights_sum_to_one(config):
    """A drifting weight sum silently rescales every score."""
    total = sum(config["preferences"]["weights"].values())
    assert total == pytest.approx(1.0)


def test_breakdown_covers_every_weighted_dimension(scorer, job, config):
    raw = scorer.score_breakdown(job)["raw_scores"]
    assert set(raw) == set(config["preferences"]["weights"])


# --- ordering --------------------------------------------------------------


def test_good_match_outranks_senior_mismatch(scorer):
    good = make_job()
    bad = make_job(
        title="Senior Frontend Engineer",
        company="Example Corp",
        location="Berlin, Germany",
        description="8+ years of experience required. React, CSS.",
        min_years_experience=8,
        workload_percent=None,
    )
    assert scorer.score(good) > scorer.score(bad)


def test_scores_stay_in_range(scorer):
    jobs = [
        make_job(),
        make_job(title="Senior Director of Engineering", min_years_experience=15),
        make_job(title="", company="", location="", description=""),
    ]
    for j in jobs:
        assert 0.0 <= scorer.score(j) <= 1.0


def test_scoring_is_deterministic(scorer, job):
    """Same input, same config, same number — the whole point of this half."""
    assert scorer.score(job) == scorer.score(job)


# --- individual dimensions -------------------------------------------------


def test_target_company_beats_unknown_company(scorer):
    tier_1 = make_job(company="Sample Energy")
    unknown = make_job(company="Some Company Nobody Listed")
    b1 = scorer.score_breakdown(tier_1)["raw_scores"]["company_tier"]
    b2 = scorer.score_breakdown(unknown)["raw_scores"]["company_tier"]
    assert b1 > b2


def test_primary_location_beats_elsewhere(scorer):
    here = make_job(location="Musterstadt, Switzerland")
    far = make_job(location="Sydney, Australia")
    raw = scorer.score_breakdown
    assert raw(here)["raw_scores"]["location_match"] > raw(far)["raw_scores"]["location_match"]


def test_role_family_matches_first_in_list_order(scorer):
    """`data_ml_engineer` is listed before `ai_engineer` in the example profile."""
    assert scorer.role_family(make_job(title="Data Engineer")) == "data_ml_engineer"
    assert scorer.role_family(make_job(title="AI Engineer")) == "ai_engineer"


def test_unrecognised_title_has_no_role_family(scorer):
    assert scorer.role_family(make_job(title="Pastry Chef")) is None


def test_unknown_domain_scores_neutral_not_zero(scorer, config):
    """Punishing every posting that doesn't use your vocabulary is a bug."""
    neutral = config["preferences"]["domain_neutral_score"]
    job = make_job(
        title="Data Engineer",
        company="Some Company",
        description="We build software. Python, SQL.",
    )
    assert scorer.score_breakdown(job)["raw_scores"]["domain_match"] == pytest.approx(neutral)


def test_domain_in_title_beats_domain_only_in_description(scorer):
    """Title and company are real signals; the same word in prose is often boilerplate."""
    in_title = make_job(title="Data Engineer, Smart Grid", company="Some Company",
                        description="Python and SQL.")
    in_body = make_job(title="Data Engineer", company="Some Company",
                       description="Python and SQL. We care about the energy transition.")
    raw = scorer.score_breakdown
    assert (raw(in_title)["raw_scores"]["domain_match"]
            > raw(in_body)["raw_scores"]["domain_match"])


def test_workload_match_peaks_at_preferred(scorer, config):
    preferred = config["search"]["workload"]["preferred_percent"]
    raw = scorer.score_breakdown
    at = raw(make_job(workload_percent=preferred))["raw_scores"]["workload_match"]
    off = raw(make_job(workload_percent=20))["raw_scores"]["workload_match"]
    assert at > off


def test_unknown_workload_is_not_penalised_to_zero(scorer):
    """NULL means unknown. Scoring it as the worst case hides real jobs."""
    unknown = scorer.score_breakdown(make_job(workload_percent=None))
    assert unknown["raw_scores"]["workload_match"] > 0.0


# --- penalties and boosts --------------------------------------------------


def test_experience_penalty_grows_with_demanded_years(scorer):
    """`experience_penalty` is its own term, separate from the keyword penalty."""
    modest = scorer.score_breakdown(make_job(min_years_experience=4))["experience_penalty"]
    steep = scorer.score_breakdown(make_job(min_years_experience=10))["experience_penalty"]
    assert steep > modest > 0.0


def test_experience_penalty_is_zero_at_or_below_target(scorer, config):
    target = config["preferences"]["experience_penalty"]["target_max_years"]
    at_target = scorer.score_breakdown(make_job(min_years_experience=target))
    assert at_target["experience_penalty"] == 0.0


def test_unknown_experience_carries_no_penalty(scorer):
    """None means unknown. Charging for it would hide every posting that is vague."""
    unknown = scorer.score_breakdown(make_job(min_years_experience=None))
    assert unknown["experience_penalty"] == 0.0


def test_experience_penalty_is_capped(scorer, config):
    cap = config["preferences"]["experience_penalty"]["cap"]
    absurd = scorer.score_breakdown(make_job(min_years_experience=30))["experience_penalty"]
    assert absurd <= cap + 1e-9


def test_senior_exempt_pattern_avoids_the_seniority_penalty(scorer):
    """"Senior Manager" can be junior management; "Senior Engineer" cannot."""
    exempt = scorer.score_breakdown(make_job(title="Senior Manager, Data"))["penalty"]
    plain = scorer.score_breakdown(make_job(title="Senior Data Engineer"))["penalty"]
    assert exempt < plain


@pytest.mark.parametrize("job_kwargs", [
    {},
    {"min_years_experience": 10},
    {"title": "Senior Data Engineer"},
    {"description": "Spanish required. Junior role, remote."},
])
def test_breakdown_reconciles_with_final_score(scorer, job_kwargs):
    """The big number has to be explainable from the parts shown under it.

    Boosts and penalties sit outside the weighted web, so a reviewer who adds
    the radar area up will not get the headline figure. The breakdown is what
    closes that gap — if this drifts, the UI is lying about its own arithmetic.
    """
    b = scorer.score_breakdown(make_job(**job_kwargs))
    web = sum(b["weighted_scores"].values())
    rebuilt = (web + b["boost"] + b["language_boost"]
               - b["penalty"] - b["experience_penalty"])
    expected = round(max(0.0, min(1.0, rebuilt)), 3)
    assert b["final_score"] == pytest.approx(expected, abs=1e-9)


def test_breakdown_final_score_equals_score(scorer, job):
    """The drawer and the list must not disagree about the same job."""
    assert scorer.score_breakdown(job)["final_score"] == scorer.score(job)
