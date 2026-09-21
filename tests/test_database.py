"""Schema, migrations and the queries the dashboard depends on.

The first test here is the one that matters most: a fresh clone has to be able
to create its own database. That path was broken before this suite existed.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from src.database import JobDatabase
from src.models import ApplicationStatus
from tests.conftest import make_job


def test_fresh_database_initialises(tmp_path):
    """A brand-new clone must get a working schema on first run."""
    db = JobDatabase(tmp_path / "fresh.db")
    db.init_schema()
    tables = {r[0] for r in db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"jobs", "applications", "replies", "audit_log"} <= tables
    db.close()


def test_init_schema_is_idempotent(tmp_path):
    """The dashboard calls it on every boot; the nightly script calls it again."""
    path = tmp_path / "twice.db"
    for _ in range(3):
        db = JobDatabase(path)
        db.init_schema()
        db.close()


def test_migrations_run_on_a_populated_database(tmp_path):
    """Re-running migrations over existing rows must not lose them."""
    path = tmp_path / "populated.db"
    db = JobDatabase(path)
    db.init_schema()
    db.upsert_job(make_job())
    db.close()

    db = JobDatabase(path)
    db.init_schema()
    assert len(db.get_jobs(limit=10)) == 1
    db.close()


# --- upsert and dedup ------------------------------------------------------


def test_upsert_reports_new_then_not_new(db):
    job = make_job()
    assert db.upsert_job(job) is True
    assert db.upsert_job(job) is False


def test_upsert_does_not_duplicate_the_same_posting(db):
    db.upsert_job(make_job())
    db.upsert_job(make_job())
    assert len(db.get_jobs(limit=10)) == 1


def test_different_postings_are_kept_apart(db):
    db.upsert_job(make_job(url="https://example.com/a"))
    db.upsert_job(make_job(title="ML Engineer", url="https://example.com/b"))
    assert len(db.get_jobs(limit=10)) == 2


def test_upsert_refreshes_last_seen(db):
    job = make_job()
    db.upsert_job(job)
    first = db.get_jobs(limit=1)[0]["last_seen_at"]
    db.upsert_job(make_job(date_scraped=datetime(2026, 9, 5, 12, 0)))
    assert db.get_jobs(limit=1)[0]["last_seen_at"] >= first


# --- status and notes -----------------------------------------------------


def test_status_change_is_persisted_and_audited(db):
    db.upsert_job(make_job())
    job_id = db.get_jobs(limit=1)[0]["id"]
    db.update_status(job_id, ApplicationStatus.BOOKMARKED.value)
    assert db.get_job(job_id)["application_status"] == "bookmarked"
    trail = db.conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE job_id = ?", (job_id,)).fetchone()[0]
    assert trail >= 1


def test_notes_survive_a_round_trip(db):
    db.upsert_job(make_job())
    job_id = db.get_jobs(limit=1)[0]["id"]
    db.update_notes(job_id, "Recruiter said they would call back.")
    assert "call back" in db.get_job(job_id)["notes"]


# --- letters --------------------------------------------------------------


def test_saving_a_letter_stores_text_language_and_checks(db):
    db.upsert_job(make_job())
    job_id = db.get_jobs(limit=1)[0]["id"]
    db.save_cover_letter(job_id, markdown="Dear Hiring Team,\n\nHello.",
                         language="en", checks='[{"code": "word_count"}]')
    row = db.get_job(job_id)
    assert row["cover_letter"].startswith("Dear Hiring Team")
    assert row["cover_letter_lang"] == "en"
    assert "word_count" in row["cover_letter_checks"]


def test_saving_a_letter_twice_keeps_the_newer_text(db):
    db.upsert_job(make_job())
    job_id = db.get_jobs(limit=1)[0]["id"]
    db.save_cover_letter(job_id, markdown="First draft.")
    db.save_cover_letter(job_id, markdown="Second draft.")
    assert db.get_job(job_id)["cover_letter"] == "Second draft."


# --- applications and reply correlation -----------------------------------


def test_marking_sent_records_the_recipient_domain(db):
    """The reply tracker correlates on this; without it replies cannot be matched."""
    db.upsert_job(make_job())
    job_id = db.get_jobs(limit=1)[0]["id"]
    db.mark_sent(job_id, eml_or_smtp="manual", message_id="<abc@example.com>",
                 recipient_email="jobs@example.com", recipient_domain="example.com")
    found = db.find_jobs_by_recipient_domain("example.com")
    assert [j["id"] for j in found] == [job_id]


def test_application_advances_through_the_pipeline(db):
    db.upsert_job(make_job())
    job_id = db.get_jobs(limit=1)[0]["id"]
    app_id = db.create_application(company="Sample Energy", role="Data Engineer",
                                   channel="ats", job_id=job_id, status="drafted")
    db.advance_application(app_id, "sent")
    app = db.get_application(app_id)
    assert app["status"] == "sent"
    assert app["sent_at"] is not None
    assert app["followup_due"] is not None


def test_unknown_advance_action_is_rejected(db):
    db.upsert_job(make_job())
    job_id = db.get_jobs(limit=1)[0]["id"]
    app_id = db.create_application(company="X", role="Y", channel="ats",
                                   job_id=job_id, status="drafted")
    with pytest.raises(ValueError):
        db.advance_application(app_id, "teleported")


# --- retention ------------------------------------------------------------


def test_retention_archives_only_stale_low_scoring_untouched_jobs(db):
    old = datetime.now() - timedelta(days=120)
    db.upsert_job(make_job(title="Irrelevant Role", url="https://example.com/x",
                           relevance_score=0.1, date_scraped=old))
    db.upsert_job(make_job(title="Good Role", url="https://example.com/y",
                           relevance_score=0.9, date_scraped=old))
    result = db.run_retention()
    assert result["archived"] == 1
    rows = db.conn.execute("SELECT title, application_status FROM jobs").fetchall()
    statuses = {r["title"]: r["application_status"] for r in rows}
    assert statuses["Irrelevant Role"] == "archived"
    assert statuses["Good Role"] != "archived"


def test_retention_spares_a_job_with_notes(db):
    """A note means a human looked at it. Retention must not undo that."""
    old = datetime.now() - timedelta(days=120)
    db.upsert_job(make_job(title="Noted Role", relevance_score=0.1, date_scraped=old))
    job_id = db.get_jobs(limit=1)[0]["id"]
    db.update_notes(job_id, "Worth a second look.")
    assert db.run_retention()["archived"] == 0


# --- export ---------------------------------------------------------------


def test_csv_export_writes_a_row_per_job(db, tmp_path):
    db.upsert_job(make_job(url="https://example.com/a"))
    db.upsert_job(make_job(title="ML Engineer", url="https://example.com/b"))
    out = tmp_path / "export.csv"
    assert db.export_csv(out) == 2
    assert out.read_text(encoding="utf-8").count("\n") >= 3   # header + 2 rows


# --- the upgrade path -----------------------------------------------------


def test_migration_adds_the_pbl_column_to_an_old_database(tmp_path):
    """A database created before the column existed must gain it on next boot.

    This is the pair to having the column in CREATE TABLE. Both paths are
    needed, and the bug they guard against is subtle: the migration reads
    PRAGMA table_info(applications) before the CREATE TABLE statement runs, so
    on a fresh database it sees an empty set and skips itself. Without the
    column in the DDL, every create_application() then failed — but only on a
    fresh clone, never on a long-lived database that had been migrated.

    The "old" state is produced from the real schema with the one column
    dropped, rather than from a hand-written historical DDL, so the rest of the
    database stays exactly as the code expects it.
    """
    path = tmp_path / "legacy.db"
    db = JobDatabase(path)
    db.init_schema()
    db.conn.execute("ALTER TABLE applications DROP COLUMN counts_for_project_based_learning")
    db.conn.execute(
        "INSERT INTO applications (company, role, channel, status, created_at) "
        "VALUES ('Sample Energy', 'Data Engineer', 'ats', 'drafted', '2026-01-01T00:00:00')"
    )
    db.conn.commit()
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(applications)")}
    assert "counts_for_project_based_learning" not in cols      # precondition
    db.close()

    db = JobDatabase(path)
    db.init_schema()
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(applications)")}
    assert "counts_for_project_based_learning" in cols
    # The pre-existing row was backfilled rather than left NULL.
    value = db.conn.execute(
        "SELECT counts_for_project_based_learning FROM applications").fetchone()[0]
    assert value in ("true", "false", "unknown")
    db.close()
