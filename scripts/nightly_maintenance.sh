#!/bin/bash
# Nightly maintenance — runs via launchd (com.jobfinder.nightly), see
# ~/Library/LaunchAgents/com.jobfinder.nightly.plist. launchd catches up
# missed runs after sleep (unlike cron), so a closed MacBook at 02:00 just
# runs this on next wake.
#
# Steps (each idempotent, each only touches what's new since the last run):
#   1. scrape --if-due-days — Breitensuche WÖCHENTLICH:
#                             läuft nur, wenn der letzte Lauf > 6.5 Tage her ist
#   2. classify-companies   — company type + industry for newly scraped companies
#   3. experience backfill  — Haiku fills min_years_experience for fresh,
#                             relevant jobs where the regex found nothing
#   4. rescore              — fold new experience data into relevance_score (free)
#   5. retention            — Score < 0.5 und 60 Tage unangetastet → archived
#   6. watchlist-poll       — TÄGLICH: Karriereseiten, Breitensuche-Abgleich,
#                             Keyword-Treffer, Sofort-Alert
#
# Der Wochendigest läuft NICHT hier, sondern als eigener launchd-Job montags
# 11:00 (com.jobfinder.digest → scripts/weekly_digest.sh).
#
# Mails (Watchlist-Alert) gehen nur raus mit NOTIFY_DRY_RUN=false in .env —
# sonst landen sie als .eml in data/outbox/notifications/.
#
# Manual run:  ./scripts/nightly_maintenance.sh
# Logs:        data/nightly.log

set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG_FILE="${PROJECT_DIR}/data/nightly.log"
PROFILE="${JOBFINDER_PROFILE:-example}"
PY="${PROJECT_DIR}/.venv/bin/python"

cd "${PROJECT_DIR}"

{
  echo
  echo "═════ $(date '+%Y-%m-%d %H:%M:%S') · nightly maintenance · profile=${PROFILE} ═════"

  echo "--- [1/6] Breitensuche (wöchentlich, nur wenn fällig) ---"
  "${PY}" -m src.main --profile "${PROFILE}" scrape --if-due-days 6.5 \
    || echo "WARN: scrape failed (exit $?)"

  echo "--- [2/6] classify-companies (neue Firmen) ---"
  "${PY}" -m src.main --profile "${PROFILE}" classify-companies -n 300 \
    || echo "WARN: classify-companies failed (exit $?)"

  echo "--- [3/6] experience backfill (frisch + relevant) ---"
  "${PY}" scripts/backfill_experience_llm.py --min-score 0.35 --fresh-days 30 \
    || echo "WARN: experience backfill failed (exit $?)"

  echo "--- [4/6] rescore ---"
  "${PY}" -m src.main --profile "${PROFILE}" rescore \
    || echo "WARN: rescore failed (exit $?)"

  echo "--- [5/6] retention (nach dem Rescore, damit aktuelle Scores zählen) ---"
  "${PY}" -m src.main --profile "${PROFILE}" retention \
    || echo "WARN: retention failed (exit $?)"

  echo "--- [6/6] watchlist-poll (täglich) ---"
  "${PY}" -m src.main --profile "${PROFILE}" watchlist-poll \
    || echo "WARN: watchlist-poll failed (exit $?)"

  echo "═════ done $(date '+%H:%M:%S') ═════"
} >> "${LOG_FILE}" 2>&1
