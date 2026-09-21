"""Shared fixtures.

Every test in this suite runs offline: no network, no API key, no dependency on
a database that happens to be lying around. `no_network` is autouse, so a test
that accidentally reaches for the network fails loudly instead of quietly
passing on someone's machine and failing in CI.
"""

from __future__ import annotations

import socket
from datetime import datetime
from pathlib import Path

import pytest

from src.config import load_config
from src.database import JobDatabase
from src.models import Job, SourcePortal

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Fail any test that opens a socket, rather than let it reach the network."""

    def blocked(*args, **kwargs):
        raise AssertionError(
            "This test tried to open a network connection. Use the mock client "
            "(src.llm.MockAnthropic) or a local fixture."
        )

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


@pytest.fixture(autouse=True)
def mock_llm(monkeypatch):
    """Default every test into mock mode, and make sure no real key leaks in."""
    monkeypatch.setenv("ANTHROPIC_MOCK", "1")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


@pytest.fixture
def config() -> dict:
    """The example profile — the same one the repo ships."""
    return load_config(profile="example")


@pytest.fixture
def db(tmp_path) -> JobDatabase:
    """A fresh database per test, on disk in a temp dir."""
    database = JobDatabase(tmp_path / "jobs_test.db")
    database.init_schema()
    yield database
    database.close()


@pytest.fixture
def cv_markdown() -> str:
    return (ROOT / "assets" / "example" / "cv_example.md").read_text(encoding="utf-8")


def make_job(**overrides) -> Job:
    """A plausible Job, overridable field by field.

    Defaults describe a good match for the example profile, so a test that
    wants a bad one only has to say what makes it bad.
    """
    fields = {
        "title": "Data Engineer",
        "company": "Sample Energy",
        "location": "Musterstadt, Switzerland",
        "url": "https://example.com/jobs/1",
        "source": SourcePortal.CAREER_PAGE,
        "description": (
            "Build and operate the ingestion pipelines behind our smart-grid "
            "analytics. Python, SQL, Airflow, dbt. Mentoring and career "
            "development. Hybrid working."
        ),
        "workload_percent": 80,
        "date_scraped": datetime(2026, 9, 1, 12, 0, 0),
    }
    fields.update(overrides)
    return Job(**fields)


@pytest.fixture
def job() -> Job:
    return make_job()
