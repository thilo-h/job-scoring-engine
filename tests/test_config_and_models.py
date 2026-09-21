"""Profile loading, validation, and the Job dataclass."""

from __future__ import annotations

from datetime import datetime

import pytest
import yaml

from src.config import DEFAULT_PROFILE, config_path_for_profile, load_config, resolve_profile
from src.models import ApplicationStatus, Job, SourcePortal, job_from_row
from tests.conftest import make_job

# --- profile resolution ----------------------------------------------------


def test_explicit_profile_wins_over_env(monkeypatch):
    monkeypatch.setenv("JOBFINDER_PROFILE", "from_env")
    assert resolve_profile("explicit") == "explicit"


def test_env_is_used_when_nothing_explicit(monkeypatch):
    monkeypatch.setenv("JOBFINDER_PROFILE", "from_env")
    assert resolve_profile() == "from_env"


def test_falls_back_to_the_default(monkeypatch):
    monkeypatch.delenv("JOBFINDER_PROFILE", raising=False)
    assert resolve_profile() == DEFAULT_PROFILE


def test_the_shipped_default_profile_exists():
    """A clone must find a profile without the user creating one first."""
    assert config_path_for_profile(DEFAULT_PROFILE).exists()


# --- validation ------------------------------------------------------------


def test_example_profile_loads_and_validates():
    cfg = load_config(profile="example")
    assert cfg["_meta"]["profile"] == "example"
    for section in ("profile", "search", "preferences", "scrapers", "output"):
        assert section in cfg


def test_missing_profile_raises():
    with pytest.raises(FileNotFoundError):
        load_config(profile="no_such_profile_exists")


@pytest.mark.parametrize("drop", ["profile", "search", "preferences", "scrapers", "output"])
def test_each_required_section_is_enforced(tmp_path, drop):
    cfg = yaml.safe_load(config_path_for_profile("example").read_text(encoding="utf-8"))
    cfg.pop(drop)
    path = tmp_path / "broken.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    with pytest.raises(ValueError, match=drop):
        load_config(path=path)


def test_keywords_are_required(tmp_path):
    cfg = yaml.safe_load(config_path_for_profile("example").read_text(encoding="utf-8"))
    cfg["search"]["keywords"] = []
    path = tmp_path / "nokeywords.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    with pytest.raises(ValueError, match="keyword"):
        load_config(path=path)


def test_database_path_is_required(tmp_path):
    cfg = yaml.safe_load(config_path_for_profile("example").read_text(encoding="utf-8"))
    cfg["output"].pop("database_path")
    path = tmp_path / "nodb.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    with pytest.raises(ValueError, match="database_path"):
        load_config(path=path)


# --- example profile sanity ------------------------------------------------


def test_example_profile_points_at_files_that_exist():
    """A broken asset path turns into a confusing runtime error much later."""
    from pathlib import Path
    cfg = load_config(profile="example")
    root = Path(__file__).resolve().parent.parent
    for key in ("cv_md", "letter_style_guide"):
        assert (root / cfg["assets"][key]).exists(), key


def test_every_enabled_scraper_is_implemented():
    from src.main import SCRAPER_REGISTRY
    cfg = load_config(profile="example")
    unknown = set(cfg["scrapers"]["enabled"]) - set(SCRAPER_REGISTRY)
    assert not unknown, f"enabled but not implemented: {unknown}"


def test_example_profile_contains_no_real_contact_details():
    """The shipped profile is a dummy. A real address here would ship by accident."""
    raw = config_path_for_profile("example").read_text(encoding="utf-8").lower()
    for leak in ("@gmail.", "@bluewin.", "@hotmail.", "@outlook."):
        assert leak not in raw


# --- Job dataclass ---------------------------------------------------------


def test_dedup_key_ignores_the_url():
    """The same posting on two boards has two URLs but one identity."""
    a = make_job(url="https://a.example.com/1")
    b = make_job(url="https://b.example.com/2")
    assert a.dedup_key() == b.dedup_key()


def test_dedup_key_separates_different_roles():
    assert make_job().dedup_key() != make_job(title="ML Engineer").dedup_key()


def test_dedup_key_is_case_and_space_insensitive():
    a = make_job(company="Sample Energy", title="Data Engineer")
    b = make_job(company="  sample energy ", title="DATA ENGINEER")
    assert a.dedup_key() == b.dedup_key()


def test_job_round_trips_through_a_database_row(db):
    db.upsert_job(make_job())
    row = db.get_jobs(limit=1)[0]
    restored = job_from_row(row)
    assert restored.title == "Data Engineer"
    assert restored.company == "Sample Energy"
    assert isinstance(restored.source, SourcePortal)


def test_new_job_starts_as_new():
    assert make_job().application_status == ApplicationStatus.NEW


def test_job_accepts_missing_optional_fields():
    """Scrapers legitimately return postings without workload or description."""
    j = Job(title="T", company="C", url="u", source=SourcePortal.MANUAL,
            date_scraped=datetime(2026, 9, 1))
    assert j.workload_percent is None
    assert j.min_years_experience is None


# --- first run in a fresh clone -------------------------------------------


@pytest.mark.parametrize("command", ["stats", "top", "export", "rescore"])
def test_read_commands_work_before_any_scrape(tmp_path, command, monkeypatch):
    """The first command anyone runs in a fresh clone must not hit a bare
    "no such table: jobs" from SQLite. Every command opens the database through
    `open_db`, which creates the schema; init_schema is idempotent, so the
    read-only commands can call it too.
    """
    import src.main as main

    cfg = load_config(profile="example")
    cfg["output"]["database_path"] = str(tmp_path / "fresh.db")
    cfg["output"]["csv_export_path"] = str(tmp_path / "out.csv")

    runners = {
        "stats": lambda: main.cmd_stats(cfg),
        "top": lambda: main.cmd_top(cfg, 5),
        "export": lambda: main.cmd_export(cfg),
        "rescore": lambda: main.cmd_rescore(cfg),
    }
    runners[command]()          # must not raise


def test_open_db_creates_the_schema(tmp_path):
    import src.main as main

    cfg = load_config(profile="example")
    cfg["output"]["database_path"] = str(tmp_path / "created.db")
    db = main.open_db(cfg)
    tables = {r[0] for r in db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "jobs" in tables
    db.close()


def test_readme_quotes_one_consistent_test_count():
    """The README names the suite size in three places. Updating one and
    forgetting the others is the realistic mistake, and it is the kind of small
    wrongness a reviewer notices and generalises from.

    Only internal consistency is asserted, not the absolute number: a test that
    compared against the collected count would fail whenever someone runs a
    single file, which is a normal thing to do.
    """
    import re
    from pathlib import Path

    claimed = set(re.findall(r"(\d{2,4}) tests", Path("README.md").read_text(encoding="utf-8")))
    assert len(claimed) <= 1, f"README quotes conflicting test counts: {sorted(claimed)}"
