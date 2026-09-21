#!/bin/bash
# Wochendigest — läuft via launchd (com.jobfinder.digest) montags 11:00, siehe
# ~/Library/LaunchAgents/com.jobfinder.digest.plist.
#
# --if-due: höchstens ein Digest pro Kalenderwoche, auch bei mehrfachem Aufruf.
# Verschickt wird nur mit NOTIFY_DRY_RUN=false in .env, sonst .eml in
# data/outbox/notifications/.
#
# launchd holt einen verpassten Termin nach, wenn der Mac um 11:00 schlief —
# nicht aber, wenn er den ganzen Montag aus war. Dann von Hand:
#   ./scripts/weekly_digest.sh
#
# Logs: data/digest.log

set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG_FILE="${PROJECT_DIR}/data/digest.log"
PROFILE="${JOBFINDER_PROFILE:-example}"
PY="${PROJECT_DIR}/.venv/bin/python"

cd "${PROJECT_DIR}"

{
  echo
  echo "═════ $(date '+%Y-%m-%d %H:%M:%S') · Wochendigest · profile=${PROFILE} ═════"
  "${PY}" -m src.main --profile "${PROFILE}" digest --if-due \
    || echo "WARN: digest failed (exit $?)"
} >> "${LOG_FILE}" 2>&1
