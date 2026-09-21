"""The model choices, and the constraints that come attached to them.

None of this is exercised by a normal run — a wrong model id or a stale price
constant fails silently, or fails in production with a 400. These tests pin the
pairings that the migration to Sonnet 5 depends on.
"""

from __future__ import annotations

import importlib
import pkgutil

import pytest

import src.agent as agent_pkg

# The current generation. Haiku 4.5 is the current Haiku — there is no Haiku 5 —
# so it is not a leftover.
SONNET = "claude-sonnet-5"
HAIKU = "claude-haiku-4-5"
CURRENT = {SONNET, HAIKU}

# Model ids are complete as written; a date suffix is not the documented form.
PREVIOUS_GENERATION = {"claude-sonnet-4-6", "claude-haiku-4-5-20251001", "claude-3-5-sonnet"}


def agent_modules():
    for info in pkgutil.iter_modules(agent_pkg.__path__):
        yield importlib.import_module(f"src.agent.{info.name}")


def model_of(module):
    return getattr(module, "MODEL", None) or getattr(module, "HAIKU_MODEL", None)


@pytest.mark.parametrize("module", list(agent_modules()), ids=lambda m: m.__name__.split(".")[-1])
def test_every_agent_pins_a_current_model(module):
    model = model_of(module)
    if model is None:
        pytest.skip("module makes no model call")
    assert model in CURRENT, f"{model} is not a current model id"


@pytest.mark.parametrize("module", list(agent_modules()), ids=lambda m: m.__name__.split(".")[-1])
def test_no_previous_generation_id_lingers(module):
    model = model_of(module)
    if model is None:
        pytest.skip("module makes no model call")
    assert model not in PREVIOUS_GENERATION


@pytest.mark.parametrize("module", list(agent_modules()), ids=lambda m: m.__name__.split(".")[-1])
def test_model_ids_carry_no_date_suffix(module):
    """`claude-haiku-4-5`, not `claude-haiku-4-5-20251001`."""
    model = model_of(module)
    if model is None:
        pytest.skip("module makes no model call")
    assert not model[-8:].isdigit(), f"{model} looks date-suffixed"


# --- prices have to match the model ---------------------------------------

PRICES = {
    SONNET: {"input": 2.00, "output": 10.00, "cache_read": 0.20, "cache_write": 2.50},
    HAIKU: {"input": 1.00, "output": 5.00, "cache_read": 0.10, "cache_write": 1.25},
}


@pytest.mark.parametrize("module", list(agent_modules()), ids=lambda m: m.__name__.split(".")[-1])
def test_price_constants_match_the_pinned_model(module):
    """A stale price constant makes the cost display quietly wrong, which is worse
    than no display at all — the operator trusts it."""
    model = model_of(module)
    if model is None or not hasattr(module, "PRICE_INPUT_PER_MTOK"):
        pytest.skip("module has no price constants")
    expected = PRICES[model]
    assert module.PRICE_INPUT_PER_MTOK == expected["input"]
    assert module.PRICE_OUTPUT_PER_MTOK == expected["output"]
    for attr, key in (("PRICE_CACHE_READ_PER_MTOK", "cache_read"),
                      ("PRICE_CACHE_WRITE_PER_MTOK", "cache_write")):
        if hasattr(module, attr):
            assert getattr(module, attr) == expected[key], attr


def test_cache_reads_are_a_tenth_of_input_everywhere():
    """The whole caching argument rests on this ratio."""
    for module in agent_modules():
        if hasattr(module, "PRICE_CACHE_READ_PER_MTOK"):
            assert module.PRICE_CACHE_READ_PER_MTOK == pytest.approx(
                module.PRICE_INPUT_PER_MTOK * 0.1)


# --- adaptive thinking shares the max_tokens budget -----------------------


def test_sonnet_callers_declare_their_thinking_mode(config):
    """Adaptive thinking is on by default on Sonnet 5 and counts against
    max_tokens. Leaving it implicit is how a request silently starts truncating,
    so both Sonnet callers state it."""
    from src.agent.chat import JobChatAgent
    from src.agent.smart_filter import SmartFilter

    filt = SmartFilter(config=config)
    filt.interpret("anything", available_sources=["jobs_ch"])
    assert filt.client.last_call["thinking"] == {"type": "adaptive"}

    chat = JobChatAgent(config=config)
    chat.reply(mode="letter_review", job={"id": 1, "title": "T", "company": "C",
                                          "description": "d"},
               history=[], user_message="hi", cover_letter="A draft.")
    assert chat.client.last_call["thinking"] == {"type": "adaptive"}


def test_sonnet_callers_leave_room_for_thinking(config):
    """max_tokens caps thinking plus response together. The pre-migration values
    (600 and 2048) were sized for a thinking-off model."""
    from src.agent import chat, smart_filter
    assert smart_filter.MAX_TOKENS >= 2000
    assert chat.MAX_TOKENS >= 4000


def test_the_latency_sensitive_path_uses_low_effort(config):
    """The filter is a schema-constrained extraction behind a button, not a
    reasoning task — the schema already enumerates every legal value."""
    from src.agent.smart_filter import SmartFilter
    agent = SmartFilter(config=config)
    agent.interpret("anything", available_sources=["jobs_ch"])
    assert agent.client.last_call["output_config"] == {"effort": "low"}


def test_no_sampling_parameters_are_sent(config):
    """temperature, top_p and top_k are rejected on the current models."""
    from src.agent.chat import JobChatAgent
    from src.agent.smart_filter import SmartFilter
    from src.agent.triage import JobTriager

    calls = []
    filt = SmartFilter(config=config)
    filt.interpret("x", available_sources=["jobs_ch"])
    calls.append(filt.client.last_call)
    trg = JobTriager(config=config)
    trg.triage(title="T", company="C", location="L", description="D")
    calls.append(trg.client.last_call)
    cht = JobChatAgent(config=config)
    cht.reply(mode="interview_prep", job={"id": 1, "title": "T", "company": "C",
                                          "description": "d"},
              history=[], user_message="hi")
    calls.append(cht.client.last_call)

    for call in calls:
        for banned in ("temperature", "top_p", "top_k", "budget_tokens"):
            assert banned not in call


# --- server tool versions are tier-dependent ------------------------------


def test_web_search_version_matches_the_model_tier(config):
    """The dynamic-filtering variant is available on Sonnet, not on the Haiku
    tier. Sending the wrong one is a 400, and only in the branch that searches."""
    from src.agent.apply_method import ApplyMethodDetector
    from src.agent.chat import JobChatAgent

    chat = JobChatAgent(config=config)
    chat.reply(mode="interview_prep", job={"id": 1, "title": "T", "company": "C",
                                           "description": "d"},
               history=[], user_message="hi")
    sonnet_tools = {t.get("type") for t in chat.client.last_call["tools"]}
    assert "web_search_20260209" in sonnet_tools

    detector = ApplyMethodDetector()
    detector.detect(title="T", company="C", location="L", description="D", url="u")
    haiku_tools = {t.get("type") for t in detector.client.last_call["tools"]}
    assert "web_search_20250305" in haiku_tools


def test_dynamic_filtering_search_does_not_also_declare_code_execution(config):
    """The newer web_search runs code execution internally; declaring a second
    execution environment alongside it confuses the model."""
    from src.agent.chat import JobChatAgent
    chat = JobChatAgent(config=config)
    chat.reply(mode="interview_prep", job={"id": 1, "title": "T", "company": "C",
                                           "description": "d"},
               history=[], user_message="hi")
    types = {t.get("type", "") for t in chat.client.last_call["tools"]}
    assert not any("code_execution" in t for t in types)


# --- fixtures must not claim a model the code no longer uses ---------------


def test_fixture_model_fields_are_current():
    """Nothing reads a fixture's `model` field, which is exactly why it goes
    stale. A fixture that names a retired model misleads the next reader about
    what the code actually calls."""
    import json
    from pathlib import Path

    for path in sorted(Path("fixtures/llm").glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        for entry in (payload if isinstance(payload, list) else [payload]):
            model = entry.get("model")
            if model:
                assert model in CURRENT, f"{path.name} names {model}"


def test_every_fixture_says_it_is_hand_written():
    """The `_comment` is the honesty marker: these are invented, not recorded,
    and a reader has to be able to tell without asking."""
    import json
    from pathlib import Path

    for path in sorted(Path("fixtures/llm").glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        for entry in (payload if isinstance(payload, list) else [payload]):
            assert entry.get("_comment"), f"{path.name} has no _comment"
