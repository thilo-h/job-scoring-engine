"""Backfill min_years_experience for existing rows in jobs.db.

Run once after adding the column. Idempotent — only touches rows where
`min_years_experience IS NULL`, so it can be re-run safely if scraping
changed any descriptions.

Usage:
    python scripts/backfill_experience.py [--dry-run]
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from collections import Counter
from pathlib import Path

# Make `src` importable when running as a script.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import load_config
from src.experience_extractor import extract_for_job

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("backfill")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print stats but don't write to DB",
    )
    args = parser.parse_args()

    config = load_config()
    db_path = config["output"]["database_path"]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT id, title, description FROM jobs WHERE min_years_experience IS NULL"
    ).fetchall()
    logger.info("Found %d jobs without min_years_experience", len(rows))

    distribution: Counter[int | None] = Counter()
    updates: list[tuple[int, int]] = []

    for row in rows:
        years = extract_for_job(title=row["title"], description=row["description"])
        distribution[years] += 1
        if years is not None:
            updates.append((years, row["id"]))

    logger.info(
        "Extraction result: %d hit / %d miss",
        sum(v for k, v in distribution.items() if k is not None),
        distribution.get(None, 0),
    )
    # Pretty distribution
    sorted_dist = sorted(
        ((k if k is not None else -1, v) for k, v in distribution.items()),
    )
    print("\nDistribution by extracted min_years:")
    for k, v in sorted_dist:
        label = "NULL (unknown)" if k == -1 else f"{k} years"
        bar = "█" * min(50, v // 5)
        print(f"  {label:>15s}  {v:5d}  {bar}")

    if args.dry_run:
        logger.info("--dry-run: no DB writes")
        return 0

    logger.info("Writing %d updates ...", len(updates))
    conn.executemany(
        "UPDATE jobs SET min_years_experience = ? WHERE id = ?",
        updates,
    )
    conn.commit()
    conn.close()
    logger.info("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
