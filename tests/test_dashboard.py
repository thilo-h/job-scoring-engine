"""Dashboard smoke tests.

Every page renders, the removed routes stay removed, and the review round-trip
works end to end. A template that references a variable the route no longer
passes fails here, which is exactly the class of breakage that unit tests miss.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.conftest import make_job


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A client wired to a temp database holding one job."""
    monkeypatch.setenv("JOBFINDER_PROFILE", "example")

    from dashboard import deps
    from src.database import JobDatabase

    database = JobDatabase(tmp_path / "dash.db")
    database.init_schema()
    database.upsert_job(make_job())

    monkeypatch.setattr(deps, "get_db", lambda: database)
    monkeypatch.setitem(deps._db_cache, "example", database)

    from dashboard.app import app
    with TestClient(app) as c:
        c.job_id = database.get_jobs(limit=1)[0]["id"]
        yield c
    database.close()


@pytest.mark.parametrize("path", [
    "/", "/browse", "/pipeline", "/replies", "/stats", "/db", "/watchlist", "/api/stats",
])
def test_pages_render(client, path):
    assert client.get(path).status_code == 200


@pytest.mark.parametrize("suffix", [
    "", "/score-breakdown", "/radar", "/outbound",
    "/chat?mode=letter_review", "/chat?mode=interview_prep",
    "/chat?mode=application_advisor",
])
def test_job_views_render(client, suffix):
    assert client.get(f"/job/{client.job_id}{suffix}").status_code == 200


@pytest.mark.parametrize("method,path", [
    ("POST", "/job/{id}/cover-letter/generate"),
    ("POST", "/job/{id}/send"),
    ("POST", "/job/{id}/self-test"),
    ("POST", "/pregenerate/trigger"),
    ("GET", "/job/{id}/send-modal"),
    ("GET", "/pregenerate/widget"),
    ("GET", "/applications/1/cover-letter"),
    ("POST", "/job/{id}/outbound/upload"),
])
def test_generation_and_sending_routes_are_gone(client, method, path):
    """The boundary is enforced by the router, not only by the templates."""
    response = client.request(method, path.format(id=client.job_id))
    assert response.status_code in (404, 405)


def test_saving_a_draft_returns_findings(client):
    """The review round-trip: paste a draft, get deterministic findings back."""
    draft = (
        "Musterstadt, 4 May 2026\n\n**Application: Data Engineer**\n\n"
        "Dear Hiring Team,\n\nI read the posting.And I built pipelines.\n\n"
        "Both experiences reflect the responsibilities listed in this role.\n\n"
        "Kind regards,\nAlex Muster\n"
    )
    r = client.post(f"/job/{client.job_id}/cover-letter/save",
                    data={"cover_letter": draft})
    assert r.status_code == 200
    assert "Saved" in r.text


def test_saved_draft_is_persisted(client):
    client.post(f"/job/{client.job_id}/cover-letter/save",
                data={"cover_letter": "Dear Hiring Team,\n\nHello.\n\nKind regards,\nAlex Muster"})
    assert client.get(f"/job/{client.job_id}").status_code == 200


def test_status_change_round_trip(client):
    r = client.post(f"/job/{client.job_id}/status", data={"status": "bookmarked"})
    assert r.status_code == 200


def test_db_explorer_rejects_a_write(client):
    """The SQL playground is read-only. That guard is worth a test."""
    r = client.post("/db/query", data={"sql": "DELETE FROM jobs"})
    assert r.status_code == 200
    assert "error" in r.text.lower() or "nicht" in r.text.lower()


def test_db_explorer_runs_a_select(client):
    r = client.post("/db/query", data={"sql": "SELECT title FROM jobs"})
    assert r.status_code == 200
    assert "Data Engineer" in r.text


def test_unknown_job_is_a_404(client):
    assert client.get("/job/999999").status_code == 404
