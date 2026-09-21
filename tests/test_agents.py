"""The LLM agents, driven by the mock client.

What these tests can prove: that the request is shaped the way the design
claims — the right tool schema, the right cached blocks, the right batching —
and that the response is parsed correctly, including the awkward shapes real
models produce.

What they cannot prove: that the model answers well. No fixture can show that.
The gap is named in the README under Limitations, and closing it needs a
labelled set and an eval harness, not more unit tests.
"""

from __future__ import annotations

import json

import pytest

from src.llm import MockAnthropic, MockFixtureMissing, scripted_tool_response

# --- the factory -----------------------------------------------------------


def test_mock_mode_needs_no_api_key(monkeypatch):
    from src.llm import get_client
    monkeypatch.setenv("ANTHROPIC_MOCK", "1")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert isinstance(get_client(purpose="test"), MockAnthropic)


def test_missing_key_without_mock_says_what_wanted_it(monkeypatch, tmp_path):
    from src import llm
    monkeypatch.delenv("ANTHROPIC_MOCK", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # Point .env resolution at an empty dir so a real local .env cannot leak in.
    monkeypatch.setattr(llm, "ROOT", tmp_path)
    with pytest.raises(RuntimeError) as exc:
        llm.get_client(purpose="job triage")
    assert "job triage" in str(exc.value)
    assert "ANTHROPIC_MOCK" in str(exc.value)


def test_unknown_tool_fails_loudly_rather_than_inventing_a_reply(tmp_path):
    client = MockAnthropic(fixtures=tmp_path)
    with pytest.raises(MockFixtureMissing):
        client.messages.create(model="m", max_tokens=10,
                               tools=[{"name": "nope", "input_schema": {}}],
                               messages=[{"role": "user", "content": "hi"}])


# --- triage ----------------------------------------------------------------


def test_triage_parses_score_and_lists(config):
    from src.agent.triage import JobTriager
    result = JobTriager(config=config).triage(
        title="Data Engineer", company="Sample Energy",
        location="Musterstadt", description="Python, SQL, Airflow.")
    assert 0 <= result.score <= 10
    assert isinstance(result.strengths, list) and result.strengths
    assert isinstance(result.gaps, list) and result.gaps
    assert result.cost_usd > 0


def test_triage_caches_the_rules_prompt_and_the_cv(config):
    """Both are large and unchanging across a run — the whole point of caching."""
    from src.agent.triage import JobTriager
    agent = JobTriager(config=config)
    agent.triage(title="T", company="C", location="L", description="D")
    cached = agent.client.cached_system_blocks()
    assert len(cached) == 2


def test_job_specific_text_stays_out_of_the_cached_blocks(config):
    """Caching is a prefix match: anything per-job in a system block would
    invalidate the cache on every single posting. The job belongs in the user
    message. A distinctive marker is used here because ordinary role titles also
    appear in the CV, which legitimately *is* in a system block."""
    from src.agent.triage import JobTriager
    agent = JobTriager(config=config)
    marker = "Zzyzx Quantum Beekeeper"
    agent.triage(title=marker, company="Qqqq Corp", location="Nowhere", description="unique-desc-42")
    call = agent.client.last_call
    system_text = " ".join(b.get("text", "") for b in call["system"])
    assert marker not in system_text
    assert "unique-desc-42" not in system_text
    assert marker in str(call["messages"])


def test_triage_forces_its_tool(config):
    """Forcing the tool is what keeps the output parseable."""
    from src.agent.triage import JobTriager
    agent = JobTriager(config=config)
    agent.triage(title="T", company="C", location="L", description="D")
    assert agent.client.last_call["tool_choice"] == {"type": "tool", "name": "submit_match_score"}


def test_triage_repairs_xml_wrapped_lists(config):
    """Real Haiku output sometimes ignores the array schema. Parse it back."""
    from src.agent.triage import JobTriager
    agent = JobTriager(config=config)
    agent.client = MockAnthropic(scripted=[json.loads(
        (pytest.importorskip("pathlib").Path("fixtures/llm/submit_match_score_xml_quirk.json"))
        .read_text(encoding="utf-8"))])
    result = agent.triage(title="T", company="C", location="L", description="D")
    assert result.strengths == ["Domain is a match", "Language requirements are met"]
    assert result.gaps == ["Posting asks for eight years"]


# --- smart filter ----------------------------------------------------------


def test_smart_filter_returns_only_schema_keys(config):
    from src.agent.smart_filter import SmartFilter
    result = SmartFilter(config=config).interpret(
        "junior data roles near Musterstadt, remote ok",
        available_sources=["jobs_ch", "career_page"])
    assert result.location == "Musterstadt"
    assert result.remote == "yes"
    assert result.exp_band == "1-3"


def test_smart_filter_schema_enumerates_valid_values(config):
    """The schema is the halluzination guard — it has to list the real options."""
    from src.agent.smart_filter import VALID_EXP_BANDS, VALID_SORTS, SmartFilter
    agent = SmartFilter(config=config)
    agent.interpret("anything", available_sources=["jobs_ch"])
    schema = agent.client.custom_tools()[0]["input_schema"]["properties"]
    assert schema["exp_band"]["enum"] == VALID_EXP_BANDS
    assert schema["sort"]["enum"] == VALID_SORTS


def test_smart_filter_passes_the_available_sources_through(config):
    """A source the database does not have must not be offerable."""
    from src.agent.smart_filter import SmartFilter
    agent = SmartFilter(config=config)
    agent.interpret("anything", available_sources=["jobs_ch", "career_page"])
    schema = agent.client.custom_tools()[0]["input_schema"]["properties"]
    assert schema["sources"]["items"]["enum"] == ["jobs_ch", "career_page"]


# --- experience extraction -------------------------------------------------


def test_experience_llm_returns_a_number_when_explicit(config):
    from src.agent.experience_llm import ExperienceLLMExtractor
    result = ExperienceLLMExtractor().extract(
        title="Data Engineer", description="At least three years required.")
    assert result.min_years == 3
    assert result.confidence == "explicit"


def test_experience_llm_passes_through_no_signal_as_none():
    """"I cannot tell" is a first-class answer. Guessing here poisons the score."""
    from pathlib import Path

    from src.agent.experience_llm import ExperienceLLMExtractor
    agent = ExperienceLLMExtractor()
    agent.client = MockAnthropic(scripted=[json.loads(
        Path("fixtures/llm/submit_experience_unclear.json").read_text(encoding="utf-8"))])
    result = agent.extract(title="Data Engineer", description="Vague posting.")
    assert result.min_years is None
    assert result.confidence == "unclear"


# --- company classification ----------------------------------------------


def test_classifier_batches_and_answers_per_company(db):
    """One call per batch, one answer per company — the cost argument depends on it."""
    from datetime import datetime

    from src.agent.company_classifier import BATCH_SIZE, classify_companies
    from src.models import SourcePortal
    from tests.conftest import make_job

    for i in range(BATCH_SIZE + 3):
        db.upsert_job(make_job(company=f"Company {i}", url=f"https://example.com/{i}",
                               source=SourcePortal.CAREER_PAGE,
                               date_scraped=datetime(2026, 9, 1)))
    summary = classify_companies(db, limit=100)
    assert summary.classified == BATCH_SIZE + 3
    # Two batches for 23 companies at a batch size of 20 — not 23 calls.
    assert summary.api_calls == 2


def test_classification_is_cached_per_company(db):
    """A second run must not pay for companies already classified."""
    from datetime import datetime

    from src.agent.company_classifier import classify_companies
    from src.models import SourcePortal
    from tests.conftest import make_job

    db.upsert_job(make_job(company="Sample Energy", source=SourcePortal.CAREER_PAGE,
                           date_scraped=datetime(2026, 9, 1)))
    first = classify_companies(db, limit=100)
    second = classify_companies(db, limit=100)
    assert first.classified == 1
    assert second.api_calls == 0


# --- briefing --------------------------------------------------------------


def test_briefing_parses_the_three_summaries(config):
    from src.agent.briefing import BriefingGenerator
    result = BriefingGenerator(config=config).generate(
        job_title="Data Engineer", company="Sample Energy",
        location="Musterstadt", description="Smart grid analytics.")
    assert result.company_summary and result.role_summary and result.profile_summary


# --- apply method ----------------------------------------------------------


def test_apply_method_counts_searches_and_reads_the_channel(config):
    from src.agent.apply_method import ApplyMethodDetector
    result = ApplyMethodDetector().detect(
        title="Data Engineer", company="Sample Energy", location="Musterstadt",
        description="Apply through our portal.", url="https://example.com/1")
    assert result.primary_channel == "portal"
    assert "cv" in result.required_documents
    assert result.web_searches == 1


def test_apply_method_result_is_json_serialisable(config):
    """It is stored as a JSON blob in jobs.apply_method."""
    from src.agent.apply_method import ApplyMethodDetector
    result = ApplyMethodDetector().detect(
        title="T", company="C", location="L", description="D", url="u")
    assert json.loads(json.dumps(result.to_dict()))


# --- chat / letter review -------------------------------------------------


def test_letter_review_proposes_a_revision(config):
    from src.agent.chat import JobChatAgent
    turn = JobChatAgent(config=config).reply(
        mode="letter_review",
        job={"id": 1, "title": "Data Engineer", "company": "Sample Energy", "description": "x"},
        history=[], user_message="Tighten the opening.",
        cover_letter="Musterstadt, 4 May 2026\n\nDear Hiring Team,\n\nI am applying.\n\nKind regards,\nAlex Muster")
    assert turn.proposed_edit
    assert turn.reply_text


def test_letter_review_is_told_to_refuse_without_a_draft(config):
    """The prompt has to say it, or the model writes a letter from scratch."""
    from src.agent.chat import JobChatAgent
    agent = JobChatAgent(config=config)
    agent.reply(mode="letter_review",
                job={"id": 1, "title": "T", "company": "C", "description": "d"},
                history=[], user_message="Help me", cover_letter=None)
    system = agent.client.last_call["system"]
    text = " ".join(b.get("text", "") for b in system) if not isinstance(system, str) else system
    assert "kein Entwurf" in text


def test_only_interview_prep_gets_web_search(config):
    """Research belongs to interview prep; the review mode must not browse."""
    from src.agent.chat import JobChatAgent
    job = {"id": 1, "title": "T", "company": "C", "description": "d"}

    def tool_types(mode, **kw):
        agent = JobChatAgent(config=config)
        agent.reply(mode=mode, job=job, history=[], user_message="hi", **kw)
        return {t.get("type") for t in (agent.client.last_call.get("tools") or [])}

    assert any("web_search" in str(t) for t in tool_types("interview_prep"))
    assert not any("web_search" in str(t) for t in
                   tool_types("letter_review", cover_letter="A draft."))


def test_unknown_chat_mode_is_rejected(config):
    from src.agent.chat import JobChatAgent
    with pytest.raises(ValueError):
        JobChatAgent(config=config).reply(
            mode="write_my_application", job={"id": 1}, history=[], user_message="hi")


# --- cost arithmetic ------------------------------------------------------


def test_cached_reads_cost_less_than_fresh_input(config):
    """If this inverts, the cost display is telling the operator the wrong story."""
    from src.agent.triage import (
        PRICE_CACHE_READ_PER_MTOK,
        PRICE_INPUT_PER_MTOK,
        JobTriager,
    )
    assert PRICE_CACHE_READ_PER_MTOK < PRICE_INPUT_PER_MTOK

    agent = JobTriager(config=config)
    agent.client = MockAnthropic(scripted=[
        scripted_tool_response("submit_match_score",
                               {"score": 5, "reason": "r", "strengths": [], "gaps": []},
                               input_tokens=1000, cache_read_input_tokens=0),
    ])
    uncached = agent.triage(title="T", company="C", location="L", description="D").cost_usd

    agent.client = MockAnthropic(scripted=[
        scripted_tool_response("submit_match_score",
                               {"score": 5, "reason": "r", "strengths": [], "gaps": []},
                               input_tokens=0, cache_read_input_tokens=1000),
    ])
    cached = agent.triage(title="T", company="C", location="L", description="D").cost_usd
    assert cached < uncached
