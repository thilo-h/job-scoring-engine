#!/usr/bin/env bash
# Back up the local SQLite databases.
#
# Why this exists:
#   A single SQLite file is a single point of failure — if it gets corrupted
#   (rare but catastrophic), all bookmarks, notes and triage decisions are
#   gone. This script uses the SQLite `.backup` command, which produces a
#   consistent copy even while the DB is being written to, unlike `cp`.
#
# What it does:
#   1. Backs up every data/jobs_*.db to data/backups/
#   2. Verifies each backup with PRAGMA integrity_check
#   3. Rotates: keeps the most recent N backups per database, deletes older
#
# Usage:
#   ./scripts/backup_databases.sh
#
# Cron suggestion (daily 03:00), run from the project root:
#   0 3 * * * cd /path/to/repo && ./scripts/backup_databases.sh >> data/backups/backup.log 2>&1

set -euo pipefail

# --- Config ---------------------------------------------------------------
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKUP_DIR="$PROJECT_ROOT/data/backups"
KEEP_LAST=14                              # Rotation: keep N most recent per DB
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"

mkdir -p "$BACKUP_DIR"

# --- Helpers --------------------------------------------------------------
backup_local_db() {
  # $1 = source DB path, $2 = label (e.g. "jobs_example")
  local src="$1" label="$2"
  local dst="$BACKUP_DIR/${label}_${TIMESTAMP}.db"

  if [ ! -f "$src" ]; then
    echo "  ⚠ Source not found: $src — skipping"
    return 0
  fi

  echo "  → $label: backing up $(du -h "$src" | cut -f1)…"
  # SQLite .backup is the safe way: it handles WAL, locks, in-flight writes
  sqlite3 "$src" ".backup '$dst'"

  # Verify
  local check
  check="$(sqlite3 "$dst" "PRAGMA integrity_check;" | head -1)"
  if [ "$check" != "ok" ]; then
    echo "  ✗ Integrity check FAILED for $label: $check"
    rm -f "$dst"
    return 1
  fi

  local jobs
  jobs="$(sqlite3 "$dst" "SELECT COUNT(*) FROM jobs;")"
  echo "  ✓ $label: $jobs jobs, integrity OK → $(basename "$dst")"
}

rotate_backups() {
  # $1 = label prefix (e.g. "jobs_example")
  local prefix="$1"
  local files
  # ls -t sorts newest first; tail +$((KEEP_LAST + 1)) gives everything beyond N
  files="$(ls -t "$BACKUP_DIR/${prefix}_"*.db 2>/dev/null | tail -n +$((KEEP_LAST + 1)) || true)"
  if [ -n "$files" ]; then
    echo "  → rotating ${prefix}: deleting $(echo "$files" | wc -l | tr -d ' ') old backup(s)"
    echo "$files" | xargs rm -f
  fi
}

# --- Run ------------------------------------------------------------------
echo "================================================================"
echo "DB backup — $(date '+%Y-%m-%d %H:%M:%S')"
echo "Target: $BACKUP_DIR (keeping last $KEEP_LAST per database)"
echo "================================================================"

FAILURES=0
LABELS=()

shopt -s nullglob
for db in "$PROJECT_ROOT"/data/jobs_*.db; do
  label="$(basename "$db" .db)"
  LABELS+=("$label")
  backup_local_db "$db" "$label" || FAILURES=$((FAILURES + 1))
done
shopt -u nullglob

if [ "${#LABELS[@]}" -eq 0 ]; then
  echo "  ⚠ No data/jobs_*.db found — nothing to back up"
fi

echo "[rotation]"
for label in "${LABELS[@]:-}"; do
  [ -n "$label" ] && rotate_backups "$label"
done

echo "================================================================"
if [ "$FAILURES" -gt 0 ]; then
  echo "✗ Backup completed with $FAILURES failure(s)"
  exit 1
fi
echo "✓ Backup completed successfully"
echo "  Total backups on disk: $(ls "$BACKUP_DIR"/*.db 2>/dev/null | wc -l | tr -d ' ')"
echo "  Disk used: $(du -sh "$BACKUP_DIR" | cut -f1)"
