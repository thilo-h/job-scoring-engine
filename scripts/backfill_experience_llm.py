"""Haiku-Fallback: füllt min_years_experience für Jobs wo Regex nichts fand.

Workflow:
  1. SELECT alle jobs WHERE min_years_experience IS NULL
  2. Pro Job: Haiku-Call mit Titel + Description
  3. Wenn Haiku eine Zahl liefert → UPDATE DB
  4. Wenn Haiku auch null sagt → DB bleibt NULL (= ehrlich "unbekannt")

Idempotent: skipt Jobs die bereits eine Zahl haben. Re-Run nach unterbrochener
Session ist safe — fängt da an wo es aufgehört hat.

Usage:
    # Sample auf 10 Jobs (Kosten-Check):
    python scripts/backfill_experience_llm.py --limit 10
    # Vollrun:
    python scripts/backfill_experience_llm.py
    # Dry-run (kein DB-Write, nur Vorschau):
    python scripts/backfill_experience_llm.py --limit 10 --dry-run
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agent.experience_llm import ExperienceLLMExtractor
from src.config import load_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("backfill_llm")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap on jobs to process (omit = all)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print results but don't write to DB")
    parser.add_argument("--min-desc-len", type=int, default=50,
                        help="Skip jobs with description shorter than this "
                             "(no point asking LLM about empty postings)")
    parser.add_argument("--min-score", type=float, default=None,
                        help="Only jobs with relevance_score >= this "
                             "(target the jobs you actually browse)")
    parser.add_argument("--fresh-days", type=int, default=None,
                        help="Only jobs last seen within N days (skip stale listings)")
    args = parser.parse_args()

    config = load_config()
    db_path = config["output"]["database_path"]

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    query = (
        "SELECT id, title, company, description FROM jobs "
        "WHERE min_years_experience IS NULL "
        "AND description IS NOT NULL AND LENGTH(description) >= ? "
    )
    params: list = [args.min_desc_len]
    if args.min_score is not None:
        query += "AND relevance_score >= ? "
        params.append(args.min_score)
    if args.fresh_days is not None:
        query += f"AND last_seen_at >= date('now', '-{int(args.fresh_days)} days') "
    query += "ORDER BY relevance_score DESC NULLS LAST"
    if args.limit:
        query += " LIMIT ?"
        params.append(args.limit)

    rows = conn.execute(query, params).fetchall()
    logger.info("%d jobs to process (limit=%s, dry-run=%s)",
                len(rows), args.limit, args.dry_run)
    if not rows:
        return 0

    extractor = ExperienceLLMExtractor()
    distribution: Counter = Counter()
    confidence_counts: Counter[str] = Counter()
    total_cost = 0.0
    written = 0  # successful UPDATE writes (incremental)
    errors = 0

    # Commit every N rows so a mid-run crash doesn't lose all progress.
    COMMIT_BATCH = 50

    started = time.time()
    for i, row in enumerate(rows, 1):
        try:
            result = extractor.extract(
                title=row["title"] or "",
                company=row["company"] or "",
                description=row["description"] or "",
            )
        except Exception as e:
            errors += 1
            logger.warning("Job %d failed: %s", row["id"], e)
            continue

        total_cost += result.cost_usd
        confidence_counts[result.confidence] += 1
        distribution[result.min_years] += 1

        if result.min_years is not None and not args.dry_run:
            conn.execute(
                "UPDATE jobs SET min_years_experience = ? WHERE id = ?",
                (result.min_years, row["id"]),
            )
            written += 1
            if written % COMMIT_BATCH == 0:
                conn.commit()

        if i % 20 == 0 or i == len(rows):
            elapsed = time.time() - started
            rate = i / elapsed if elapsed > 0 else 0
            eta = (len(rows) - i) / rate if rate > 0 else 0
            logger.info(
                "  [%d/%d] cost=$%.3f written=%d rate=%.1f/s ETA=%.0fs",
                i, len(rows), total_cost, written, rate, eta,
            )

    # Final commit catches the tail (< COMMIT_BATCH remaining writes).
    if not args.dry_run:
        conn.commit()

    print("\n=== Distribution ===")
    # Defensive: tolerate non-int keys that slipped through (shouldn't happen
    # after the coercion in ExperienceLLMResult, but harmless to guard).
    int_keys = []
    for k in distribution:
        if k is None:
            continue
        try:
            int_keys.append(int(k))
        except (TypeError, ValueError):
            pass
    for k in sorted(set(int_keys)) + [None]:
        v = distribution.get(k, 0)
        if v == 0:
            continue
        label = "NULL (unclear)" if k is None else f"{k} years"
        bar = "█" * min(50, v // 5 + 1)
        print(f"  {label:>16s}  {v:5d}  {bar}")

    print(f"\nConfidence: {dict(confidence_counts)}")
    print(f"Errors: {errors}")
    print(f"Total cost: ${total_cost:.4f}")
    print(f"Writes committed: {written}")

    if args.dry_run:
        logger.info("--dry-run: no DB writes")
    conn.close()
    logger.info("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
