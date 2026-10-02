"""Fill the example profile's database with invented postings for a demo.

No network, no API key: every posting below is made up, scored by the real
`JobScorer`, and the LLM steps (triage, briefing) run through the dashboard's
own routes in mock mode, so what the dashboard shows afterwards is exactly what
those code paths produce from the canned responses in `fixtures/llm/`.

The README screenshots were taken from a database built with this script.

Usage:
    python scripts/seed_demo.py            # refuses to touch a non-empty DB
    python scripts/seed_demo.py --reset    # delete data/jobs_example.db first
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Make `src` and `dashboard` importable when running as a script.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Before anything imports the LLM client: canned replies only, never a real key.
os.environ["ANTHROPIC_MOCK"] = "1"
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ["JOBFINDER_PROFILE"] = "example"

from src.config import load_config  # noqa: E402
from src.database import JobDatabase  # noqa: E402
from src.models import Job, SourcePortal  # noqa: E402
from src.scoring import JobScorer  # noqa: E402

NOW = datetime.now().replace(microsecond=0)

# (title, company, location, source, workload, days ago, description)
# Every company is invented; most come from the example profile's tier lists so
# the company-tier dimension has something to do.
POSTINGS = [
    ("Data Engineer", "Sample Energy", "Musterstadt, Switzerland", SourcePortal.CAREER_PAGE, 80, 1,
     "Build and operate the ingestion pipelines behind our smart-grid analytics. "
     "Python, SQL, Airflow, dbt. You own source connectors through to the warehouse "
     "models. Some Kafka streaming. Mentoring and career development. Hybrid working, "
     "80-100%. Junior and graduate applicants welcome."),
    ("Applied AI Engineer", "Example Robotics", "Musterstadt, Switzerland", SourcePortal.CAREER_PAGE, 100, 2,
     "Ship LLM features into our fleet-management product: retrieval (RAG), embeddings, "
     "evaluation harnesses and the review UI around them. Python, FastAPI, Postgres. "
     "Mentoring from senior engineers, hybrid, remote days possible."),
    ("Analytics Engineer", "Muster Analytics", "Beispielstadt, Switzerland", SourcePortal.JOBS_CH, 80, 3,
     "Model our customer data in dbt and SQL, maintain KPI dashboards in Power BI, and "
     "work with product on data analysis. 80%. Entry level, mentoring programme."),
    ("Solutions Engineer, Data Platform", "Demo Logistics", "Musterstadt, Switzerland", SourcePortal.SWISSDEVJOBS, 100, 4,
     "Help logistics customers integrate our data platform via API. Python and SQL, "
     "customer workshops, fleet management domain. Hybrid."),
    ("Technology Consultant AI & Data", "Example Corp", "Beispielstadt, Switzerland", SourcePortal.JOBS_CH, 100, 6,
     "Advise clients in real estate and building automation on data strategy and LLM "
     "use cases. Analytics background, German and English. Graduate programme."),
    ("Machine Learning Engineer", "Placeholder Health", "Remote, Switzerland", SourcePortal.STARTUPTICKER, 100, 5,
     "Train and deploy models for clinical scheduling. Python, Docker, Kubernetes. "
     "Fully remote within Switzerland."),
    ("Senior Data Engineer", "Acme Industries", "Musterstadt, Switzerland", SourcePortal.JOBS_CH, 100, 8,
     "Lead our data platform team. 8+ years of experience with Spark and Airflow required, "
     "people leadership expected."),
    ("Frontend Engineer", "Sample Software", "Musterstadt, Switzerland", SourcePortal.SWISSDEVJOBS, 100, 9,
     "React and TypeScript for our customer portal. Design systems, accessibility."),
    ("Ingeniero de Datos", "Ejemplo Movilidad", "Barcelona, Spain", SourcePortal.TECNOEMPLEO, 100, 7,
     "Pipelines de datos para movilidad urbana y transporte público. Python, SQL, Airflow. "
     "Español e inglés. Spanish required."),
]

# A draft with deliberate flaws, so the deterministic letter review has findings
# to show. German, because the review rules report in German. The flaws: a
# finished CV station in the present tense, a sentence opening with "Und", a
# summary sentence, a station named twice, no workload, and far too short.
DRAFT = (
    "Musterstadt, 2. Oktober 2026\n\n"
    "**Bewerbung als Data Engineer**\n\n"
    "Sehr geehrte Damen und Herren\n\n"
    "Ihre Ausschreibung beschreibt genau die Arbeit, die ich bei Muster Analytics jeden Tag "
    "mache: Ingestion-Pipelines bauen und betreiben, von der Quelle bis zum Warehouse-Modell.\n\n"
    "Bei Muster Analytics betreibe ich Pipelines für Dokumentarchive von Kunden und verantworte "
    "das Evaluations-Harness eines Retrieval-Dienstes. Und genau diese Verbindung von Datenfluss "
    "und Messbarkeit suche ich bei Ihnen.\n\n"
    "Bei Beispiel Energie entwickle ich derzeit einen Klassifikator, der eingehende Service-Mails "
    "der richtigen Queue zuweist, samt einer deterministischen Prüfschicht davor.\n\n"
    "Beide Erfahrungen spiegeln die Anforderungen dieser Stelle wider.\n\n"
    "Über eine Einladung zu einem Gespräch freue ich mich.\n\n"
    "Freundliche Grüsse\nAlex Muster\n"
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--reset", action="store_true", help="delete the example database first")
    args = parser.parse_args()

    config = load_config(profile="example")
    db_path = Path(config["output"]["database_path"])
    if args.reset:
        for suffix in ("", "-wal", "-shm"):
            Path(f"{db_path}{suffix}").unlink(missing_ok=True)

    db = JobDatabase(db_path)
    db.init_schema()
    if db.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]:
        print(f"{db_path} already holds jobs; rerun with --reset to replace them.")
        return 1

    scorer = JobScorer(config)
    for i, (title, company, location, source, workload, days, description) in enumerate(POSTINGS, 1):
        job = Job(
            title=title, company=company, location=location, source=source,
            url=f"https://example.com/jobs/{i}", description=description,
            workload_percent=workload,
            date_posted=NOW - timedelta(days=days), date_scraped=NOW - timedelta(days=days),
        )
        job.relevance_score = scorer.score(job)
        db.upsert_job(job)
    db.close()

    # The LLM steps go through the dashboard routes, so the stored results are
    # what a click in the UI would have stored.
    from fastapi.testclient import TestClient

    from dashboard.app import app

    with TestClient(app) as client:
        ids = {row["company"]: row["id"]
               for row in JobDatabase(db_path).get_jobs(limit=len(POSTINGS))}
        target = ids["Sample Energy"]
        for step in ("triage", "briefing/generate"):
            client.post(f"/job/{target}/{step}").raise_for_status()
        client.post(f"/job/{target}/cover-letter/save", data={"cover_letter": DRAFT}).raise_for_status()
        client.post(f"/job/{target}/status", data={"status": "bookmarked"}).raise_for_status()
        client.post(f"/job/{ids['Example Robotics']}/status", data={"status": "applied"}).raise_for_status()
        client.post(f"/job/{ids['Acme Industries']}/status", data={"status": "ignored"}).raise_for_status()

    print(f"Seeded {len(POSTINGS)} invented postings into {db_path}.")
    print("Start the dashboard with:  ANTHROPIC_MOCK=1 uvicorn dashboard.app:app")
    return 0


if __name__ == "__main__":
    sys.exit(main())
