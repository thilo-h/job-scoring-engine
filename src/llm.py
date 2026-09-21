"""Central Anthropic client factory, with an offline mock for tests and demos.

Every agent in :mod:`src.agent` gets its client from :func:`get_client` rather
than constructing one itself. That buys two things:

* One place resolves the API key and produces one clear error when it is
  missing, instead of nine copies of the same boilerplate.
* One place can hand back a **mock** client, so the LLM code paths can run —
  and be tested — without an API key and without network access.

Mock mode is on when ``ANTHROPIC_MOCK`` is truthy. In that mode nothing leaves
the machine: :class:`MockAnthropic` replays canned responses from
``fixtures/llm/`` (override with ``ANTHROPIC_MOCK_FIXTURES``), matched to the
tool the request asks for.

    ANTHROPIC_MOCK=1 python -m src.main --profile example top -n 10
    ANTHROPIC_MOCK=1 uvicorn dashboard.app:app

The fixtures are **hand-written, not recorded from real traffic**, and they say
so in their own ``_comment`` field. They are shaped like real responses so the
parsing code is genuinely exercised — tool-use blocks, usage counters,
server-tool blocks — but their content is invented. A green test against a
fixture proves the parsing, the schema handling and the cost arithmetic; it
proves nothing about model quality.

Response objects mimic the SDK's attribute surface closely enough for the code
under test: ``.content`` (blocks with ``.type``, ``.name``, ``.input``,
``.text``, ``.id``), ``.usage`` (the four token counters), ``.model`` and
``.stop_reason``. They are deliberately not SDK types — a mock that subclassed
the real ones would drift silently when the SDK changes.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FIXTURE_DIR = ROOT / "fixtures" / "llm"

_TRUTHY = ("1", "true", "yes", "on")


def mock_enabled() -> bool:
    """True when the client factory should hand back a mock."""
    return os.getenv("ANTHROPIC_MOCK", "").strip().lower() in _TRUTHY


def fixture_dir() -> Path:
    override = os.getenv("ANTHROPIC_MOCK_FIXTURES", "").strip()
    return Path(override) if override else DEFAULT_FIXTURE_DIR


def get_client(api_key: Optional[str] = None, *, purpose: str = ""):
    """Return an Anthropic client, or a mock when ``ANTHROPIC_MOCK`` is set.

    Args:
        api_key: explicit key, overriding the environment.
        purpose: short label naming the caller, used in the error message so a
                 missing key says which feature wanted it.
    """
    if mock_enabled():
        logger.info("ANTHROPIC_MOCK is set — %s uses canned responses", purpose or "client")
        return MockAnthropic()

    load_dotenv(ROOT / ".env", override=True)
    key = api_key or os.getenv("ANTHROPIC_API_KEY")
    if not key:
        what = f" for {purpose}" if purpose else ""
        raise RuntimeError(
            f"ANTHROPIC_API_KEY not set{what}. Copy .env.example to .env and add "
            "your key, or set ANTHROPIC_MOCK=1 to run against canned responses."
        )

    import anthropic

    return anthropic.Anthropic(api_key=key)


# ---------------------------------------------------------------------------
# Mock response objects — shaped like the SDK's, deliberately not derived
# ---------------------------------------------------------------------------


class _Block:
    """One content block. Attributes present depend on ``type``."""

    def __init__(self, data: dict):
        self.type: str = data.get("type", "text")
        self.text: str = data.get("text", "")
        self.name: str = data.get("name", "")
        self.input: dict = data.get("input", {})
        self.id: str = data.get("id", f"mock_{self.type}_1")
        # web_search_tool_result carries a nested content list
        raw = data.get("content")
        if isinstance(raw, list):
            self.content = [_SearchResult(r) for r in raw]

    def __repr__(self) -> str:
        label = self.name or (self.text[:30] if self.text else "")
        return f"<_Block {self.type} {label!r}>"


class _SearchResult:
    def __init__(self, data: dict):
        self.type: str = data.get("type", "web_search_result")
        self.url: str = data.get("url", "")
        self.title: str = data.get("title", "")


class _Usage:
    def __init__(self, data: dict):
        self.input_tokens: int = int(data.get("input_tokens", 0))
        self.output_tokens: int = int(data.get("output_tokens", 0))
        self.cache_read_input_tokens: int = int(data.get("cache_read_input_tokens", 0))
        self.cache_creation_input_tokens: int = int(data.get("cache_creation_input_tokens", 0))


class MockResponse:
    def __init__(self, data: dict):
        self.content = [_Block(b) for b in data.get("content", [])]
        self.usage = _Usage(data.get("usage", {}))
        self.model: str = data.get("model", "mock-model")
        self.stop_reason: str = data.get("stop_reason", "end_turn")
        self.stop_details = data.get("stop_details")
        self.id: str = data.get("id", "msg_mock")


# ---------------------------------------------------------------------------
# Mock client
# ---------------------------------------------------------------------------


class MockFixtureMissing(RuntimeError):
    """No canned response matches the request — say so loudly.

    Failing here rather than returning something plausible is deliberate: a
    silent stand-in would make a test pass against a request nobody wrote a
    fixture for.
    """


class _Messages:
    def __init__(self, owner: "MockAnthropic"):
        self._owner = owner

    def create(self, **kwargs) -> MockResponse:
        return self._owner._respond(kwargs)


class MockAnthropic:
    """Stand-in for ``anthropic.Anthropic`` that never touches the network.

    Two ways to drive it:

    * **Scripted** — pass ``scripted=[dict, ...]`` and the responses come back
      in order, the last one repeating. Handy in a test that needs a specific
      payload without a fixture file.
    * **Fixtures** (the default) — the tool the request asks for names a file
      in the fixture directory. ``tool_choice={"type": "tool", "name": "x"}``
      matches ``x.json``; otherwise the first custom tool in ``tools`` does.
      With no custom tool at all, ``text_reply.json`` is used.

    Every call is appended to :attr:`calls`, so a test can assert on what was
    actually sent — that the system blocks were marked for caching, that the
    schema enumerated the right values, that a batch was really batched.
    """

    def __init__(self, scripted: Optional[list[dict]] = None,
                 fixtures: Optional[Path] = None,
                 handlers: Optional[dict] = None):
        self.messages = _Messages(self)
        self.calls: list[dict] = []
        self._scripted = list(scripted) if scripted else None
        self._fixtures = fixtures or fixture_dir()
        self._replay_index: dict[str, int] = {}
        # A handler computes the response from the request, for the cases where
        # a static file cannot work — a batch endpoint has to answer about the
        # items it was actually given.
        self._handlers = {**DEFAULT_HANDLERS, **(handlers or {})}

    # -- introspection helpers for tests ---------------------------------

    @property
    def last_call(self) -> dict:
        if not self.calls:
            raise AssertionError("no request was made")
        return self.calls[-1]

    def cached_system_blocks(self, call: Optional[dict] = None) -> list[dict]:
        """System blocks in a call that ask to be cached."""
        system = (call or self.last_call).get("system") or []
        if isinstance(system, str):
            return []
        return [b for b in system if isinstance(b, dict) and b.get("cache_control")]

    def custom_tools(self, call: Optional[dict] = None) -> list[dict]:
        """Tools the caller defined, excluding Anthropic-hosted server tools."""
        return [t for t in ((call or self.last_call).get("tools") or [])
                if "input_schema" in t]

    # -- response construction -------------------------------------------

    def _respond(self, kwargs: dict) -> MockResponse:
        self.calls.append(kwargs)

        if self._scripted is not None:
            data = self._scripted.pop(0) if len(self._scripted) > 1 else self._scripted[0]
            return MockResponse(data)

        name = self._wanted_tool(kwargs)
        handler = self._handlers.get(name)
        if handler is not None:
            return MockResponse(handler(kwargs))
        return MockResponse(self._load(name))

    @staticmethod
    def _wanted_tool(kwargs: dict) -> str:
        choice = kwargs.get("tool_choice") or {}
        if isinstance(choice, dict) and choice.get("name"):
            return str(choice["name"])
        for tool in kwargs.get("tools") or []:
            if "input_schema" in tool and tool.get("name"):
                return str(tool["name"])
        return "text_reply"

    def _load(self, name: str) -> dict:
        path = self._fixtures / f"{name}.json"
        if not path.exists():
            raise MockFixtureMissing(
                f"No fixture for tool {name!r} at {path}. Add it, or pass "
                "scripted=[...] to MockAnthropic."
            )
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            # A list replays in order across calls, then repeats the last entry.
            i = self._replay_index.get(name, 0)
            self._replay_index[name] = min(i + 1, len(payload) - 1)
            return payload[i]
        return payload


# ---------------------------------------------------------------------------
# Request-dependent handlers
# ---------------------------------------------------------------------------


def _classify_companies(kwargs: dict) -> dict:
    """Answer about the companies the request actually asked about.

    The classifier sends a batch and matches the answer back by name, so a
    static fixture would only ever fit one batch size. This walks the prompt,
    pulls the names out, and assigns a category deterministically from the
    name — nonsense as a classification, correct as an exercise of the batching
    and name-matching code.
    """
    text = ""
    for msg in kwargs.get("messages") or []:
        content = msg.get("content")
        if isinstance(content, str):
            text += content + "\n"

    names: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("- ") and "|" in line:
            names.append(line[2:].split("|", 1)[0].strip())

    categories = ["startup", "scaleup", "sme", "enterprise"]
    industries = ["ai", "energy", "saas", "other"]
    classifications = [
        {
            "company": name,
            "category": categories[len(name) % len(categories)],
            "industry": industries[sum(map(ord, name)) % len(industries)],
            "confidence": 0.5,
            "reasoning": "mock classification, derived from the name",
        }
        for name in names
    ]
    return {
        "stop_reason": "tool_use",
        "content": [{"type": "tool_use", "name": "classify_companies",
                     "input": {"classifications": classifications}}],
        "usage": {"input_tokens": 40 * max(len(names), 1), "output_tokens": 25 * max(len(names), 1)},
    }


DEFAULT_HANDLERS: dict[str, Any] = {
    "classify_companies": _classify_companies,
}


def scripted_tool_response(tool_name: str, payload: dict[str, Any], **usage) -> dict:
    """Build one scripted tool-use response — convenience for tests."""
    return {
        "stop_reason": "tool_use",
        "content": [{"type": "tool_use", "name": tool_name, "input": payload}],
        "usage": {"input_tokens": 100, "output_tokens": 50, **usage},
    }
